# Adopting fleet

How an app calls fleet's reusable workflows, configures `[tool.fleet]` in its `pyproject.toml` and syncs fleet's content. The steps below take an app from nothing to every piece; the sections after them hold each piece's caller and inputs.

The snippets name the release as `vX.Y.Z` until fleet's first release, and each release stamps them with its own version (`fleet release prepare`, [release.md](release.md)); `fleet self-check` fails when a snippet's version and `VERSION` disagree. `<commit>` is the full SHA the tag points at (`git ls-remote https://github.com/AnthonyPluth/fleet vX.Y.Z`); Dependabot moves it from then on. What a version number promises is in [release.md](release.md#what-a-version-number-promises).

## Adopting, step by step

1. **Pick a release** and pin every fleet workflow at its commit, with the tag in the comment (`uses: AnthonyPluth/fleet/.github/workflows/<name>.yml@<commit>   # vX.Y.Z`). All the pins move together: `sync.yml` fails when they disagree.
2. **Write the callers** you want, one file each in `.github/workflows/`, from the sections below: [Security scans](#security-scans), [Deep-scan reminder](#deep-scan-reminder), [Agent review](#agent-review), [Review mining](#review-mining) and [its tasks caller](#ticked-rows-become-agent-tasks), [Merge gate](#merge-gate) and [its threads caller](#open-review-threads), [Checks](#checks), [Content sync](#content-sync-two-pins-two-bots), [Verify](#verify-control-smoke-verify-flows), [Screenshot diff](#screenshot-diff), [Cancel runs on close](#cancel-runs-on-close) and [Tidy releases](#tidy-releases). A caller passes each secret by name (`secrets: inherit` would hand over every secret the repository has) and grants exactly the permissions its section lists.
3. **Set `[tool.fleet]`** in `pyproject.toml` ([`[tool.fleet]`](#toolfleet)), key by key: a check runs only where its key is set. Add `[tool.fleet.verification]` (with `guides` once the app has [verification guides](#verification-guides)) when the pull requests carry a [verification record](#verification-records).
4. **Sync fleet's content** once by hand, from fleet at the pins' commit: `fleet sync` writes the `AGENTS.md` blocks, the vendored `.claude/` files, the shim `tools/fleet` and `.fleet/version` (the release on line 1, and on line 2 the commit the pins name, which `tools/fleet` runs fleet at, so a moved tag can't change what runs). Put the empty markers in `AGENTS.md` first ([AGENTS.md blocks](#agentsmd-blocks)); from then on `sync.yml` opens the sync pull requests.

   ```sh
   uvx --from "git+https://github.com/AnthonyPluth/fleet@<commit>" fleet sync --to v2.3.0
   tools/fleet drift
   git add .fleet/version tools/fleet AGENTS.md .claude/
   ```

5. **Point `.claude/settings.json`** at the vendored hook and declare fleet's marketplace at the same tag ([Claude Code plugin](#claude-code-plugin)), and add `.claude/hooks/fleet.conf`. Add `.github/agent-review/context.md` (and `invariants.md` with `second-review-paths`) for the reviews.
6. **Create the labels** the workflows use: `safety-critical` and `security` (they make a pull request risky for the second review; `security` also marks the "Deep security scan due" issue), `manual` (the "Deep security scan due" issue) and `review-mining` (the "Repeated review findings" issue). An issue opened with a label that doesn't exist fails.
7. **Add the secrets and variables**: `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` (the reviews), the variable `FLEET_BOT_APP_ID` and the secret `FLEET_BOT_PRIVATE_KEY` (the content sync's App), and optionally the variables `RUNS_ON`, `AGENT_REVIEW_BUDGET_USD` and `AGENT_REVIEW_MAX_TURNS`.
8. **Protect `main` with a ruleset** (Settings, Rules, Rulesets, a branch ruleset targeting the default branch): require a pull request before merging, block force pushes and deletions, and require one status check, **Merge gate**, and nothing else. The gate already waits for every workflow, check run and status the pull request started, and for the required ones; requiring them again in the ruleset would keep a pull request whose path filters skip a workflow from ever merging. Don't allow bypass for bots: Dependabot's merges go through the gate.
9. **Let Dependabot bump the pins**, in one group so every fleet workflow moves at once, a few days after a release:

   ```yaml
   - package-ecosystem: github-actions
     directory: /
     schedule: { interval: daily }
     cooldown: { default-days: 3 }
     groups:
       fleet:
         patterns: ["AnthonyPluth/fleet", "AnthonyPluth/fleet/*"]
   ```

   Dependabot takes one entry per ecosystem and directory: an app that already has a `github-actions` entry for `/` adds the `fleet` group to it, first in its `groups` (a dependency joins the first group whose patterns match), makes that entry daily with a 3-day cooldown for every action, fleet's pins included, and lists both fleet names in the group's `patterns` (Dependabot may name a reusable workflow by its repository alone, which `AnthonyPluth/fleet/*` doesn't match). The cooldown counts from the release date, so a daily entry opens the bump on its first run once the release is 3 days old: a Tuesday release is bumped on the Friday. A weekly Monday entry waits for the first Monday after that, up to about 10 days. The cooldown stays for fleet too because the bump pull request's CI runs the new fleet code as soon as it opens, and it leaves a few days to catch a bad release. By default the merge gate passes a fleet bump but leaves it for the owner to merge, and merging it by hand starts `sync.yml`, which opens the content's pull request. With `automerge-fleet-bumps: true` ([Merge gate](#merge-gate)) the gate merges it once it passes, like any other Dependabot pull request (the tests and the security scans still run on it); that merge, made with the workflow's token, starts no `push` workflow, so also list the content sync's caller in `after-merge-workflows` (`docker.yml security.yml fleet-sync.yml`), or the content's pull request waits for the sync's weekly run. Step 8 doesn't ask for a Code Owner review; a ruleset that adds one (optional, with a `CODEOWNERS` entry covering `.github/`) keeps every action bump, fleet's included, waiting for that review whatever the gate's inputs, since the gate's merge is refused until it is given.

## Security scans

`.github/workflows/security.yml` (`workflow_call`) runs Semgrep (the registry packs `p/python`, `p/security-audit`, `p/owasp-top-ten` and `p/javascript`, plus every rules file in the app's `.semgrep/`, after `python3 .semgrep/check_examples.py pipx run semgrep==<pinned>` when that file exists), `npm audit` in each web directory, Trivy over the files (always) and over the image, zizmor over the workflows, and OpenSSF Scorecard on pushes and scheduled runs. A pull request, and a push to `main`, runs only the scans its change can affect (`.github/scripts/security-changes.sh`); a scheduled or manual run scans everything; a change under `.github/` runs everything. The scripts come from fleet at the pinned ref.

The caller keeps the triggers and the concurrency group. It grants the permissions the called jobs ask for, `security-events: write` included even with `sarif-upload: false` (a called job can't ask for more than its caller has).

```yaml
name: Security scans

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  schedule:
    - cron: "17 5 * * 1"
  workflow_dispatch:

permissions: {}

concurrency:
  group: security-${{ github.ref }}
  cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}

jobs:
  scans:
    permissions:
      contents: read
      pull-requests: read      # the pull request's changed files
      security-events: write   # the SARIF uploads
      actions: read            # Scorecard
      id-token: write          # Scorecard publishes its results
    uses: AnthonyPluth/fleet/.github/workflows/security.yml@<commit>   # v2.3.0
    with:
      code-paths: ^(waypoint|frontend/src|\.semgrep)/
      semgrep-paths: waypoint run.py frontend/src
      web-dirs: frontend
      image-paths: ^(Dockerfile|\.dockerignore|pyproject\.toml|poetry\.lock)$
      runs-on: ${{ vars.RUNS_ON != '' && github.actor != 'dependabot[bot]' && (!github.event.pull_request || github.event.pull_request.head.repo.full_name == github.repository && github.event.pull_request.user.login != 'dependabot[bot]') && vars.RUNS_ON || '"ubuntu-latest"' }}
```

A private repository on a personal account has no code scanning: pass `sarif-upload: false` (Semgrep, Trivy and zizmor then fail their job on findings, which the merge gate sees) and `scorecard: false`, and don't require CodeQL in its merge gate.

| Input | Default | What |
| --- | --- | --- |
| `code-paths` | required | Regex of the paths whose change runs Semgrep |
| `semgrep-paths` | `.` | What Semgrep scans, space-separated |
| `semgrep-examples` | run `.semgrep/check_examples.py` when present | The command that checks the app's own rules against their examples |
| `web-dirs` | none | Directories with a `package-lock.json` to `npm audit`, one per line; a change to their `package.json` or `package-lock.json` runs it |
| `image-paths` | none | Regex of the paths whose change rebuilds and scans the image (none: no image scan) |
| `dockerfile` | `Dockerfile` | The image's Dockerfile |
| `zizmor-paths` | `.` | What zizmor audits (space-separated paths); the whole tree by default |
| `sarif-upload` | `true` | Upload findings to the Security tab; `false` fails the job on findings instead |
| `scorecard` | `true` | Run OpenSSF Scorecard (public repositories only) |
| `runs-on` | `"ubuntu-latest"` | The runner for every job but Scorecard's, as JSON |
| `fleet-ref` | the workflow's `FLEET_REF` | The fleet ref to fetch scripts from (fleet's self-test only) |

### Trivy's version

On Linux X64 and ARM64 runners, both Trivy jobs install Trivy themselves (`.github/scripts/security-trivy-install.sh`) and pass `skip-setup-trivy: true` to `aquasecurity/trivy-action`, whose own install (`aquasecurity/setup-trivy`) downloads from `get.trivy.dev`, which stopped resolving. The script downloads the Linux release tarball for the runner from `https://github.com/aquasecurity/trivy/releases/download/`, checks it against the SHA-256 that `security.yml` pins, and puts `trivy` on the `PATH`; the tarball is cached outside the workspace (`actions/cache`, keyed on the version, the platform and the checksum) and checked again on every run, cached or not, so a cache that doesn't match is downloaded again. The file scan saves the cache; the image job only restores it. On a pinned platform, a failed download or a checksum that doesn't match fails the job; it never falls back. A runner with no pinned checksum (macOS, Windows, 32-bit Linux) gets a warning and keeps trivy-action's own install, at the same `TRIVY_VERSION`, as before, so it still depends on `get.trivy.dev`.

Dependabot bumps `trivy-action`'s pin but can't bump Trivy itself, whose version and checksums are three values at the top of `security.yml`: `TRIVY_VERSION`, `TRIVY_SHA256_X64` and `TRIVY_SHA256_ARM64`. To move to a new Trivy, a fleet pull request changes all three together, taking the checksums of `trivy_<version>_Linux-64bit.tar.gz` and `trivy_<version>_Linux-ARM64.tar.gz` from the release's `trivy_<version>_checksums.txt`, and confirms them independently: `sha256sum` over the downloaded tarballs, and their Sigstore bundles (`<tarball>.sigstore.json` on the release) with `sigstore verify github --bundle <tarball>.sigstore.json --repository aquasecurity/trivy --ref refs/tags/<version> <tarball>`. A `trivy-action` bump whose default `version` moves is the reminder to do it. The apps change nothing but the pin.

## Deep-scan reminder

`.github/workflows/deep-scan-due.yml` (`workflow_call`) opens one issue, "Deep security scan due" (labels `manual`, `security`), unless one is open, with the link to fleet's [deep-scan runbook](deep-scan.md) at the pinned ref, the revision of the last scan (read from the last closed issue with that title) and the number of commits on the default branch since. The caller schedules it and grants `issues: write` (the issues it reads and the one it opens) and `contents: read` (the comparison of the last scan's revision with the default branch, which counts the commits); no secrets. A called job can't ask for more than its caller grants, so a caller with `issues: write` alone fails to start. Both labels must exist in the repository.

```yaml
name: Deep scan due

on:
  schedule:
    - cron: "23 6 1 1,4,7,10 *"   # quarterly
  workflow_dispatch:

permissions: {}

jobs:
  remind:
    permissions:
      contents: read   # count the commits since the last scan
      issues: write    # read the earlier issues and open the reminder
    uses: AnthonyPluth/fleet/.github/workflows/deep-scan-due.yml@<commit>   # v2.3.0
```

Inputs: `dry-run` (default `false`; print the issue instead of opening it) and `fleet-ref`.

The app's own deep-scan settings live in its repository, not in an open issue: add a `## Deep security scans` section to `AGENTS.md`, outside the `fleet:begin`/`fleet:end` blocks, with the four things the runbook's [Per-app settings](deep-scan.md#per-app-settings) lists (when it scans, where findings go, the model, where the first scan is tracked), and open one first-run issue for the first scan, which closes once the scan record is written in it. Retitle it to exactly "Deep security scan due" before closing it (or open and close one with that title carrying the record): the reminder reads the last scan's revision only from a closed issue with that title. The reminder issues take over from there.


## Agent review

`.github/workflows/agent-review.yml` (`workflow_call`) runs fleet's Claude reviews. By default (the owner's policy: Codex reviews every pull request, the Claude review only security-sensitive ones) it runs only the second review; with `first-review: true` it also runs the independent first review of every agent's pull request: a fresh Claude Code session on Sonnet reads the change against main's `AGENTS.md` and the app's `.github/agent-review/context.md`, posts one comment and sets the "Agent review" status, which the merge gate then requires when the app lists it in `required-statuses`. The second review runs on `claude-opus-5-5` (temporarily, while Fable is rate-limited; normally `claude-fable-5-1`), on risky pull requests (labelled `safety-critical` or `security`, or changing `.github/**`, `.claude/**` or the paths in `second-review-paths`), as its own comment and "Second review" status; a pull request that isn't risky gets a passing "Second review" saying so, so the gate can require it on every pull request. When a push and a trigger label both start a second review of the same head, the run that started last decides its "Second review" and its comment, whichever finishes first, and a complete review's blocking verdict stands over a later pass; this lives in the reusable workflow (which needs the caller's `actions: write`, already granted, to look the runs up), so the caller keeps its concurrency groups and only moves its pin. The design, the re-review, the carried-forward verdict, the second review and the reviewer's isolation are in [agent-review.md](agent-review.md).

The caller keeps the triggers and passes the secrets by name (`secrets: inherit` would hand over every secret). It runs from main's copy (`pull_request_target`), so a pull request can't change how it is reviewed, and grants the permissions the called jobs ask for.

```yaml
name: Agent review

on:  # zizmor: ignore[dangerous-triggers] -- runs main's workflow; the pull request's code is only read, never run
  pull_request_target:
    branches: [main]
    types: [opened, synchronize, reopened, ready_for_review, labeled, unlabeled]

permissions: {}

jobs:
  review:
    permissions:
      contents: read           # main's instructions and the pull request's code
      pull-requests: write     # the comment
      statuses: write          # the "Second review" status ("Agent review" too, with first-review: true)
      actions: write           # start the merge gate, and look up the runs that set "Second review"
    uses: AnthonyPluth/fleet/.github/workflows/agent-review.yml@<commit>   # v2.3.0
    with:
      generated: |
        docs/openapi.json
        frontend/src/lib/api-types.ts
        docs/feature-map.json
        docs/src/content/docs/contributing/feature-map.md
      budget-usd: ${{ vars.AGENT_REVIEW_BUDGET_USD || '3' }}
      max-turns: ${{ vars.AGENT_REVIEW_MAX_TURNS || '60' }}
      second-review-paths: |
        app/photos/**
        app/library/**
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

An app that sets `second-review-paths` also adds `.github/agent-review/invariants.md`: a numbered list of the app's invariants, each with the test or rule that holds it (fleet's own is the example). Without it, a risky pull request's second review is an `error` status naming the file. The labels `safety-critical` and `security` must exist in the repository.

Every finding carries an id (`` `f:1a2b3c4d` `` after its title), and the comment records what became of each: fixed by a later push, or dismissed by the repository owner with a conversation comment `/fleet dismiss f:<id> <reason>` (ten characters of reason at least; anyone else's is ignored). A dismissal is a record for improving the reviewer, not an override: it doesn't pass the review or change the merge gate ([agent-review.md](agent-review.md)).

The app adds `.github/agent-review/context.md` (what the app is, what private data looks like in its repository and where it would land, which files are generated and what checks cover them, which `AGENTS.md` sections matter most; without it every review is an `error` status naming the file) and drops its own `agent-review.yml` body, `.github/agent-review/`, `.github/scripts/agent-review-run.sh`, `.github/scripts/agent_review.py`, `tests/test_agent_review.py` and the Dependabot entry for `/.github/agent-review`: fleet locks the Claude Code version for everyone. Its `AGENTS.md` "Mistakes that keep coming back" items keep bold titles, which findings cite as their `kind`.

| Input | Default | What |
| --- | --- | --- |
| `generated` | none | Paths left out of `diff.patch` (still listed for the reviewer), one per line. An entry wins over the rule that fixtures, demo and sample data are always read (Runway lists `tests/fixtures/forecast_golden.json`) |
| `first-review` | `false` | `true` turns the independent first review back on: the "Agent review" status and comment on every agent's pull request (list `Agent review` in the merge gate's `required-statuses` too). `false`, the default, runs no first review and posts no status or comment for it; the second review runs either way |
| `verification` | `false` | `true` gives the reviews the description's [verification record](#verification-records) as `verification.md`, to judge against the change: every changed behaviour a dimension, every `verified` row's evidence real, every gap explained ([agent-review.md](agent-review.md#the-verification-record)). An app that sets `[tool.fleet.verification]` passes it |
| `budget-usd` | `3` | The most a review may spend, in US dollars as Claude Code estimates it |
| `max-turns` | `60` | The most round trips to the model a review may take |
| `second-review-labels` | `safety-critical,security` | Labels that make a pull request risky, comma-separated |
| `second-review-paths` | none | Changed paths that make a pull request risky, one glob per line (`**` spans directories); `.github/**` and `.claude/**` always count. Setting it requires `.github/agent-review/invariants.md` |
| `second-review-always` | `.github/**` and `.claude/**` | Changed paths that always make a pull request risky, one glob per line. A caller that wants the second review on fewer paths lists them; fleet's own caller lists every reusable workflow, `release.yml`, the scripts and agent-review files, CODEOWNERS, `.claude/**`, `plugins/**` and `shim/**` (a test keeps new reusable workflows in the list) |
| `second-review-budget-usd` | `10` | The most the second review may spend, in US dollars |
| `second-review-max-turns` | `120` | The most round trips to the model the second review may take |
| `merge-gate-workflow` | `merge-gate.yml` | The caller's merge gate workflow file to start once the status is set; empty starts none |
| `dry-run` | `false` | Self-test only: the report jobs print the comment and the status instead of posting them |
| `fake-review` | none | Self-test only: the answer fleet's stub reviewer gives in place of Claude Code (`pass`, `advisory`, `blocking`, `verification`, `malformed`) |
| `fake-event` | none | Self-test only, with `fake-review`: the event the second review decides on (`action=… sender=… label=… labels=a,b files=x,y`) |
| `fleet-ref` | the workflow's `FLEET_REF` | The fleet ref to fetch the prompt, scripts and tools from (fleet's self-test only) |

Secrets, both optional and passed by name: `ANTHROPIC_API_KEY` (an API key, which wins when both are set) or `CLAUDE_CODE_OAUTH_TOKEN` (a Claude Pro or Max subscription's token from `claude setup-token`); both reviews use them, each in its own job. Outputs: `verdict` (`pass`, `blocking`, `error`, `skip`) and `description` for the first review, `second-verdict` and `second-description` for the second. The models are `sonnet` (first) and `claude-opus-5-5` (second; temporarily, while Fable is rate-limited) and not inputs: changing either is a fleet pull request.

## Review mining

`.github/workflows/review-mining.yml` (`workflow_call`) keeps one issue per repository, "Repeated review findings" (label `review-mining`, which must exist), rewritten each run: the kinds of review finding that came back on two or more pull requests in the window, with the check each should become, read from the reviews' hidden findings records and `AGENTS.md`'s "Mistakes that keep coming back" states; the kinds mostly dismissed, the findings open at merge and, where `[tool.fleet.verification]` is set, the areas that keep going unverified; at its top, "Loop health", whether review, verification and the report are improving the harness, beside last run's numbers, also in the job summary and the `loop-stats.json` artifact ([loop-stats.md](loop-stats.md)); no model, no secrets. Each row ends with a checkbox, "becomes an Agent task", that keeps its tick across rewrites. The report and its rules are in [review-mining.md](review-mining.md). The caller schedules it monthly, or weekly (`"17 6 * * 1"`): the issue is rewritten either way.

```yaml
name: Review mining

on:
  schedule:
    - cron: "17 6 1 * *"   # monthly; weekly instead: "17 6 * * 1"
  workflow_dispatch:

permissions: {}

jobs:
  mine:
    permissions:
      contents: read           # AGENTS.md, and the default branch's commits for the loop stats
      issues: write            # the one issue
      pull-requests: read      # the pull requests in the window
    uses: AnthonyPluth/fleet/.github/workflows/review-mining.yml@<commit>   # v2.3.0
```

| Input | Default | What |
| --- | --- | --- |
| `days` | `90` | The window, in days before the run, a pull request must have been updated in |
| `loop-stats-days` | `30` | The loop stats' window, in days before the run |
| `dry-run` | `false` | Print the issue instead of opening or rewriting it (the job summary and the loop-stats artifact are still produced) |
| `fleet-ref` | the workflow's `FLEET_REF` | The fleet ref to fetch the tool from (fleet's self-test only) |

### Ticked rows become Agent tasks

`.github/workflows/mining-tasks.yml` (`workflow_call`) opens one Agent task issue, once, per row of the report that is ticked and links no issue yet, on each edit of the report by the owner, and writes ` → #N` back on the row; nothing runs for anyone else's edit, or for any other issue. No secrets, `issues: write` only. The rules are in [review-mining.md](review-mining.md#ticked-rows-become-agent-tasks).

```yaml
name: Mining tasks

on:
  issues:
    types: [edited]

permissions: {}

jobs:
  tasks:
    permissions:
      issues: write            # open the task issues and link the report's rows
    uses: AnthonyPluth/fleet/.github/workflows/mining-tasks.yml@<commit>   # v2.3.0
```

| Input | Default | What |
| --- | --- | --- |
| `dry-run` | `false` | Print the issues and links instead of writing them |
| `fixture` | none | Self-test only: an issues event payload in fleet's checkout, read in place of the real event; implies `dry-run` |
| `fleet-ref` | the workflow's `FLEET_REF` | The fleet ref to fetch the tool from (fleet's self-test only) |

## The `fleet` command

`fleet_tools` is standard library only (Python 3.11 or newer), so a runner's system `python3` runs it from a checkout of fleet with nothing installed:

```sh
python3 -E -s -m fleet_tools.cli checks --root /path/to/app --commits origin/main..HEAD
```

Each command finds the app's root as the nearest `pyproject.toml` up from the working directory, unless `--root` names it.

| Command | What it does | Exit codes |
| --- | --- | --- |
| `fleet checks [--commits BASE..HEAD] [--pr-event PATH]` | Every check the app's `[tool.fleet]` configures. With `--commits` (a pull request's range; BASE is merged with HEAD to find where the branch left), also the commit trailers, removed or skipped tests and committed images. With `--pr-event` (a pull request event payload, `$GITHUB_EVENT_PATH` on Actions) and `[tool.fleet.verification]`, also the description's [verification record](#verification-records). The one entry point for `make fleet-checks` and CI. | 0 all passed (the line lists what ran; a warning is a `warning:` line, `::warning::` on GitHub Actions, and still 0), 1 problems (one per line on stderr, `::error::` on GitHub Actions), 2 a config error or a failing `git` |
| `fleet no-comments` | No comments or docstrings in tracked Python, CSS and Svelte. Opt-in: does nothing without `[tool.fleet.no-comments]`. | 0, 1, 2 |
| `fleet coverage-floor [coverage.json]` | coverage.py's JSON report against `[tool.fleet.coverage]`. | 0, 1, 2 (no floors, or no report) |
| `fleet next-version [--sha] [--output]` | The next release version from the commits since the last tag (`feat:` a minor; `!:`, `BREAKING CHANGE:` or a trailing `#major` a major; else a patch; a trailing `#minor` a minor). Appends `newest`, then `version` and `new`, or `skip`, to `$GITHUB_OUTPUT`. Replaces `.github/scripts/next-version.sh`. | 0, 1 a failing `git` |
| `fleet test-timings DIR [--top N]` | Sums up the per-test timing files a timing test result wrote. | 0 |
| `fleet review-outcomes --repo OWNER/NAME --pr N [--json] [--second]` | What became of each of a pull request's review findings: `open`, `resolved` by a later push, or `dismissed` by the owner with `/fleet dismiss f:<id> <reason>` ([agent-review.md](agent-review.md)). Reads the review comment and the pull request's comments with `gh`. | 0, 1 a failing `gh` |
| `fleet agent-review [second] detect\|wanted\|previous\|plan\|risky\|report` | The agent reviews' decisions (`second` for the second review's marker, status and rules), run by `agent-review.yml` as `python3 -I .fleet/fleet_tools/agent_review.py` ([agent-review.md](agent-review.md)). | 0, 2 usage |
| `fleet review-mine --repo OWNER/NAME [--days 90] [--agents-md AGENTS.md] [--app-root DIR] [--summary FILE] [--stats LOOP-STATS.json] [--dry-run]` | The "Repeated review findings" issue, from the reviews' findings and outcomes records on the pull requests updated in the window (repeats, often dismissed, open at merge) and, where `DIR`'s `pyproject.toml` sets `[tool.fleet.verification]`, the verification gaps of the pull requests merged in it, run by `review-mining.yml` ([review-mining.md](review-mining.md)); with `--stats`, a "Loop health" section at the top beside last run's numbers. | 0, 1 a failing `gh api`, 2 usage |
| `fleet loop-stats --repo OWNER/NAME --json OUT.json --markdown OUT.md [--days 30] [--app-root DIR]` | Loop health over the window: the merged pull requests' reviews and what became of their findings, their verification records and the paths no guide covers (where `DIR` sets `[tool.fleet.verification]`), the report's rows and the issues opened from them, the harness changes on the default branch, `.fleet/version` and an open `fleet/sync` pull request, as schema 1 JSON and Markdown; a part it can't read is `null` and named, never zero ([loop-stats.md](loop-stats.md)). | 0 (also with parts not read), 1 the files can't be written, 2 usage |
| `fleet mining-tasks --event PATH [--repo OWNER/NAME] [--dry-run]` | From an `issues` `edited` event of the "Repeated review findings" report by the owner, one Agent task issue per row of the current report that is ticked and not yet linked (at most 20 per run) (none when a `mined-from` marker for the row exists), and the ` → #N` written back on the row, run by `mining-tasks.yml` ([review-mining.md](review-mining.md#ticked-rows-become-agent-tasks)). | 0 (also when there is nothing to do), 1 a failing `gh api`, 2 an unreadable event or no repository |
| `fleet verification show (--pr N \| --body-file PATH) [--json]` | The verification record in a pull request's description ([Verification records](#verification-records)): from pull request N in the current repository (`gh api repos/{owner}/{repo}/pulls/N --jq .body`) or a file (`-` is stdin). Text has mentions and HTML comments defused; `--json` prints `{"pr": N or null, "dimensions": [{"dimension", "status", "how", "evidence"}]}` with the cells as data. | 0, 1 a record that doesn't parse (the line and the rule), a failing `gh` or an unreadable file, 2 usage |
| `fleet sync-plan [--root APP] [--changelog CHANGELOG.md --body FILE]` | The content sync's plan from the workflow pins and `.fleet/version` (`action=none\|sync\|migration`, `pin`, `title`, …), and the pull request's or issue's body from fleet's CHANGELOG at the pin, run by `sync.yml` ([Content sync](#content-sync-two-pins-two-bots)). | 0, 1 pins that disagree or another state it can't act on, 2 no app root |

## `[tool.fleet]`

A check runs only where its key is set, so an app opts in key by key. Paths are relative to the app's root. Waypoint's values:

```toml
[tool.fleet]
migrations = "waypoint/storage/migrations/versions"
migration-tests = "tests/test_migrations.py"
tested-from = "0001"
image-homes = ["docs/public/", "waypoint/static/", "waypoint/storage/logos/"]
image-files = ["docs/src/assets/logo.png"]
skills = ".claude/skills"
feature-maps = ["tools/user_feature_map.py"]
extra-checks = "tools.app_checks:CHECKS"

[tool.fleet.screenshots]
dir = "docs/src/assets/screenshots/"
names = "pages"
verify = "frontend/verify/verify.mjs"
flows = "frontend/verify/flows"

[[tool.fleet.generated]]
path = ".claude/skills/verify-waypoint/references/cli.md"
command = "make --no-print-directory -s verify-skill-print"
fix = "make verify-skill"

[tool.fleet.no-comments]
paths = ["waypoint", "tests", "tools", "frontend/src"]

[tool.fleet.verification]
required = false
guides = "docs/verification"

[tool.fleet.coverage]
overall = 85
modules = { "waypoint/domain/" = 90, "waypoint/providers/" = 90, "waypoint/server/api/" = 90 }
```

| Key | Check | Meaning |
| --- | --- | --- |
| `migrations` | migrations | The Alembic versions directory: one head, revisions unique, four digits and the start of their file's name. |
| `migration-tests` | migrations | The file holding a `test_<revision>_...` method per migration (default `tests/test_migrations.py`). |
| `tested-from` | migrations | The first revision that needs its own test (default `0001`); the older ones predate the rule. Never lower it. |
| `test-paths` | tests removed (`--commits`) | Where tests live (default `["tests/", "frontend/"]`): `tests/**/test_*.py` and `*.test.ts`/`.mjs`/`.js`. A removed test needs a `Removes-Test: <name> — <why>` trailer, an added skip a `Skips-Test: <name> — <why>` one. `[]` turns the check off. |
| `image-homes` | images (`--commits`) | Directories where any image belongs. |
| `image-files` | images (`--commits`) | Single image files that belong, matched exactly. |
| `[tool.fleet.screenshots]` | images (`--commits`) | `dir`: where `make verify` writes its screenshots; `names`: `"pages"` (`<page>-<width>.png` and `<page>-<width>-hero.png` for each of `verify.mjs`'s `PAGES`) or `"pages-and-flows"` (`<page>-<width>.png`, `<page>-<TOP>-<width>.png`, and `flow-<name>-<width>.png` and `flow-<name>-<step>-<width>.png` for each flow and its `screenshot` steps); `verify` (default `frontend/verify/verify.mjs`) and `flows` (default `frontend/verify/flows`) are read at the branch's head. Widths are `verify.mjs`'s `DEFAULT_VIEWPORTS`, else `phone` and `desktop`. |
| `skills` | skills | The skills directory: each `*/SKILL.md` has frontmatter with `name` (its folder's) and a `description` (at most 1024 characters, no `<` or `>`), a body, and relative links that lead somewhere. |
| `skill-homes-ignore` | skill-homes | Globs (`*` matches across `/`) of `SKILL.md` paths allowed outside the two homes, besides `node_modules/`, `.fleet/` and `tests/fixtures/` (default none). Where `skills` is set, `skill-homes` checks every skill sits at `<skills>/<name>/SKILL.md`, a `fleet-*` one is vendored, and no app skill takes a fleet skill's name or copies its body ([skills](skills.md)). |
| `[[tool.fleet.generated]]` | skills | A generated file: `path`, the `command` that prints what it should hold (run from the app's root), and optionally `fix`, the command that rewrites it. |
| `feature-maps` | skills | Python files that define `OUT` (the map's path), `generate()` (its text and any problems) and `check(committed, generated)` (the problems). |
| `extra-checks` | the app's own | `module:NAME`, a list of callables in the app's tree (`tools/app_checks.py`, or a package's `__init__.py`). Each takes the check context (`.root`, `.config`, `.commits`) and returns a list of problem strings, reported like fleet's own. A missing module or name, a failed import or a check that raises is a problem, never a silent pass. |
| `[tool.fleet.no-comments]` | `fleet no-comments` | `paths`: what to read (default: every tracked file); `skip`: substrings of paths to leave out (default `.semgrep/examples/`, `node_modules/`). |
| `[tool.fleet.verification]` | verification (`--pr-event`) | `required` (default `false`): a missing or malformed record in the pull request's description is a problem on a ready pull request, else a warning; `exempt-authors` (default `["dependabot[bot]", "fleet-bot[bot]"]`): pull requests by these authors are skipped. The table's presence turns the check on ([Verification records](#verification-records)). `guides` (no default): the directory of [verification guides](#verification-guides), relative to the repository root and below it, which turns on the `verification-guides` check. |
| `[tool.fleet.coverage]` | `fleet coverage-floor` | `overall`: the floor for the whole report; `modules`: path prefix to floor, for each module with statements (the longest matching prefix wins). |

The workflow conventions check runs wherever `.github/workflows/` or `.github/actions/` exists: bash as each workflow's default shell, except in a caller whose every job calls a reusable workflow (`uses:` and no `steps:`), which runs no shell of its own, every action pinned by its commit SHA with its version in a comment, `gh api ... per_page=` with `--paginate`, no `runner`, `steps`, `job` or `env` expression in a workflow's or job's `env:`, every event under `on:` one GitHub Actions has (an `on:` the check can't read is reported), and every job's `runs-on:` either fleet's `vars.RUNS_ON` line (`fleet_tools/checks/workflows.py`) or `runs-on: ubuntu-latest   # hosted: <why it needs GitHub's runners>`; a job that only macOS can build (an iPhone app's Xcode build) may instead use a pinned `runs-on: macos-<number>   # hosted: <why>`, never `macos-latest` or a larger runner. GitHub's standard macOS runners are free for a public repository and cost ten times a Linux minute in a private one. A `runs-on` under `with:` is a reusable workflow's input, which the check leaves alone, so the callers below pass as written.

## Merge gate

`.github/workflows/merge-gate.yml` (`workflow_call`) sets one commit status, "Merge gate", on a pull request's head: passing once the newest run of every workflow the pull request started has succeeded (and the required ones ran at all), every other app's check run and every commit status has passed (and the required statuses are set), pending while anything is still running, failing otherwise. Require that one status in the ruleset on `main`. It merges Dependabot's pull requests once it passes, except one that changes an `AnthonyPluth/fleet/` reference, which it leaves for the owner unless `automerge-fleet-bumps` is on. The rules are in `.github/scripts/merge-gate.sh`, which the workflow fetches from fleet at its pinned ref.

The triggers stay in the app's caller, because a called workflow can't declare them. The `workflow_run` list names the app's own pull request workflows (the last one to finish decides); `check_suite` covers other apps' checks; `workflow_dispatch` decides a pull request by number (the agent review starts it this way once it has set its status). The job's `if:` keeps it to pull requests. The caller runs from `main`'s copy with a write token and checks out nothing of the pull request.

```yaml
name: Merge gate

on:  # zizmor: ignore[dangerous-triggers]
  workflow_run:
    workflows: [Docker image, Security scans, Docs]   # the app's own pull request workflows
    types: [completed]
  check_suite:
    types: [completed]
  workflow_dispatch:
    inputs:
      pr:
        description: "Pull request number"
        required: true

permissions: {}

concurrency:
  group: merge-gate-${{ github.event.workflow_run.head_sha || github.event.check_suite.head_sha || inputs.pr }}
  cancel-in-progress: true

jobs:
  gate:
    if: >-
      github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'workflow_run' && github.event.workflow_run.event == 'pull_request') ||
      (github.event_name == 'check_suite' && github.event.check_suite.app.slug != 'github-actions')
    permissions:
      contents: write          # merge Dependabot's pull requests
      pull-requests: write     # merge them, and read their commits and files
      statuses: write          # the "Merge gate" status
      actions: write           # read the pull request's workflow runs, and start main's release after a merge
      checks: read             # other apps' check runs
    uses: AnthonyPluth/fleet/.github/workflows/merge-gate.yml@<commit>   # v2.3.0
    with:
      sha: ${{ github.event.workflow_run.head_sha || github.event.check_suite.head_sha }}
      pr: ${{ inputs.pr }}
      required-workflows: |
        Docker image
        Security scans
        CodeQL
```

Inputs, each with the default fleet's own gate uses:

| Input | Default | What |
| --- | --- | --- |
| `sha`, `pr` | | The head commit, or a pull request number; one of the two |
| `required-workflows` | `Security scans`, `CodeQL` | Workflows that must have run, one per line. CodeQL isn't required on Dependabot's pull requests or on a pull request from a fork (its default setup skips both); the gate reads the pull request's head repository itself, so it decides the same on every trigger, and every other required workflow stays required. A CodeQL run that does come still counts. List `CodeQL` plainly: a caller that swaps it for another name on a fork's `workflow_run` isn't needed, and its swap didn't hold on `check_suite` or `workflow_dispatch`, where `github.event.workflow_run` is empty |
| `ignored-workflows` | none | Workflows whose runs don't count, as a regex alternation (`Preview\|Screenshot diff`). `Merge gate` and `Cancel runs on close` never count |
| `required-statuses` | `Second review` | Commit statuses that must be set on the head, one per line. None are required on Dependabot's pull requests. An empty string requires none. An app that turns the first review on passes `Agent review` and `Second review`. Any status that fails fails the gate, required or not |
| `ignored-apps` | `codecov\|github-actions` | Apps whose check runs don't count (Codecov's are informational; Actions' runs are counted as workflows) |
| `poll-seconds`, `poll-attempts` | 30, 20 | How long to look again for CodeQL, whose finishing starts no workflow |
| `blocking-thread-badges` | `P1\|P2` | Badges on a review thread's first comment that fail the gate while the thread is open (Codex marks its findings `P1 Badge`, `P2 Badge`), as a regex alternation of letters and digits. An empty string turns the check off. An outdated thread counts until it is resolved. If the review threads can't be read, the gate stays pending |
| `blocking-thread-authors` | `chatgpt-codex-connector` | Whose first comment counts toward `blocking-thread-badges`, as a regex alternation of letters, digits and `-` (a bot's login without `[bot]`). Anyone else's badge text is ignored; an empty string lets any author's count |
| `require-codex-review` | `false` | `true` keeps the gate pending ("Codex review of this commit") until Codex (`chatgpt-codex-connector[bot]`) has reviewed the head commit: a pull request review on that commit, or Codex's comment whose "Reviewed commit" or review state names it (a review that finds nothing is a comment). A review of an older commit doesn't count; Dependabot's pull requests are exempt. Add `issue_comment` to the threads caller (below), since a comment starts no workflow the gate listens to |
| `automerge-dependabot` | `true` | Merge Dependabot's pull requests once the gate passes (every commit Dependabot's own and verified, same repository, not a draft, no fleet reference changed unless `automerge-fleet-bumps`) |
| `automerge-fleet-bumps` | `false` | `true` also merges a Dependabot pull request that changes a fleet reference, once the gate passes; `false` leaves it for the owner. List the content sync's caller in `after-merge-workflows` with it, since the gate's merge starts no `push` workflow |
| `after-merge-workflows` | `docker.yml security.yml` | Workflow files to start on `main` after such a merge, space-separated; a merge made with the token starts none by itself. With `automerge-fleet-bumps`, add the content sync's caller (`docker.yml security.yml fleet-sync.yml`) so it follows a merged fleet bump |
| `dry-run` | `false` | Print the status posts, the merge and the workflow starts instead of doing them |
| `fleet-ref` | the workflow's `FLEET_REF` | Where to fetch the script from; the self-test passes its own commit |

### Open review threads

A review thread whose first comment is by one of the `blocking-thread-authors` (Codex, by default) and carries one of the `blocking-thread-badges` fails the gate until someone resolves it. GitHub Actions has no event for resolving or reopening a thread, so nothing runs then: **after resolving a thread, re-run the "Merge gate" by hand** (its latest run's "Re-run jobs", or `gh workflow run merge-gate.yml --ref main -f pr=<number>`), or push, which runs it anyway. An agent that resolves a thread on a pull request it owns starts the gate itself this way, once after the last thread, with the access it already has, naming the workflow file its agent review caller's `merge-gate-workflow` names (`merge-gate.yml` by default); it never does for a pull request it doesn't own (the `fleet-review-guidance` skill's "After resolving a thread"). A new review or review comment starts no workflow the gate listens to either, so the caller adds a second small workflow, `merge-gate-threads.yml` (`workflow_call`; inputs `pr`, `merge-gate-workflow` (default `merge-gate.yml`), `dry-run`, `fleet-ref`; `actions: write` only), which starts the caller's own merge gate workflow on `main` with the pull request's number. It checks out nothing and runs nothing of the pull request's. The caller keeps the triggers, and skips a pull request from a fork, whose token is read-only there (that pull request's gate looks again on its next push or check, or by hand):

```yaml
name: Merge gate threads

on:
  pull_request_review:
    types: [submitted]
  pull_request_review_comment:
    types: [created, edited, deleted]

permissions: {}

jobs:
  decide:
    if: github.event.pull_request.head.repo.full_name == github.repository
    permissions:
      actions: write   # start the merge gate
    uses: AnthonyPluth/fleet/.github/workflows/merge-gate-threads.yml@<commit>   # v2.3.0
    with:
      pr: ${{ github.event.pull_request.number }}
```

Upgrading changes behaviour: an app that adopts this version, or whose pin moves to it, fails its gate on an open P1 or P2 thread. Pass `blocking-thread-badges: ""` to keep the old behaviour.

With `require-codex-review: true` the gate also waits for Codex's review of the head, and a review that finds nothing arrives as a conversation comment, which none of the triggers above sees. The caller then also runs on `issue_comment`, with a second job for it (a comment's workflow runs with the base repository's token, so there is no fork to skip; only Codex's own comments on a pull request start it):

```yaml
name: Merge gate threads

on:
  pull_request_review:
    types: [submitted]
  pull_request_review_comment:
    types: [created, edited, deleted]
  issue_comment:   # zizmor: ignore[dangerous-triggers] only Codex's own comment on a pull request starts a job, which checks out nothing and passes main's merge gate only the number
    types: [created, edited]

permissions: {}

jobs:
  decide:
    if: github.event_name != 'issue_comment' && github.event.pull_request.head.repo.full_name == github.repository
    permissions:
      actions: write   # start the merge gate
    uses: AnthonyPluth/fleet/.github/workflows/merge-gate-threads.yml@<commit>   # v2.3.0
    with:
      pr: ${{ github.event.pull_request.number }}
  codex-comment:
    if: github.event_name == 'issue_comment' && github.event.issue.pull_request && github.event.comment.user.login == 'chatgpt-codex-connector[bot]'
    permissions:
      actions: write   # start the merge gate
    uses: AnthonyPluth/fleet/.github/workflows/merge-gate-threads.yml@<commit>   # v2.3.0
    with:
      pr: ${{ github.event.issue.number }}
```

The caller has no concurrency group, neither the workflow's nor a job's. Codex posts a review and a comment per finding in one burst, and every run of a review event belongs to the same head commit: with a group and `cancel-in-progress`, each newer run cancelled the one before it, and a cancelled job leaves a check run on the pull request's head that GitHub lists as failing ("Merge gate" green, the checks red, the merge state "unstable"). Cancelling them saves nothing, since each run only starts the gate, and `merge-gate.yml`'s own group (`merge-gate-<pull request number>` for a `workflow_dispatch`, with `cancel-in-progress`) already replaces an older evaluation of the same pull request with the newer one; those runs belong to `main`, not to the pull request's head. A group keyed on `github.run_id` with `cancel-in-progress: false` would do the same as none, so there is none. A workflow-level group in particular would also be wrong: it is joined before any job's `if:` is read, and `issue_comment` fires on every comment on the pull request (a person's, the agent's own `@codex review`, another bot's), so each such run would cancel a gate restart still in flight (the `decide` run of Codex's review, or the `codex-comment` run of its comment) and then skip its own jobs, leaving the gate pending until someone re-runs it. `fleet checks`' conventions and zizmor (`tests/test_merge_gate_threads_caller.py` runs it over every documented caller) pass this caller; the `dangerous-triggers` ignore on `issue_comment` carries its reason, since the job runs only for `chatgpt-codex-connector[bot]`'s comment and hands main's merge gate nothing but the pull request number.

What each app passes today: Waypoint and Runway `required-workflows: Docker image, Security scans, CodeQL`, Runway also `ignored-workflows: Preview|Screenshot diff`, and each app `required-statuses: Second review` with `first-review: false` in its agent review caller (the owner's policy). Those two are the defaults now, so a caller drops them when its pin moves here; an app that wants the first review back passes `first-review: true` and `required-statuses` with both `Agent review` and `Second review`.

## AGENTS.md blocks

fleet ships the parts of `AGENTS.md` the apps share as blocks, one file each in `agents-md/sections/` (`starting-work`, `working-in-a-container`, `mistakes`, `git-and-prs`), with front matter naming the `## ` heading it lives under. In the app's `AGENTS.md` a block sits between markers:

```md
## Mistakes that keep coming back

<!-- fleet:begin mistakes v2.3.0 -->
…fleet's text, verbatim…
<!-- fleet:end mistakes -->

- **An app item** (human-judged: why no check can express it). …
```

Inside the markers is fleet's file at the marker's version, byte for byte; everything outside them is the app's. App items go after a block, under the same heading ("Mistakes that keep coming back" opens with fleet's rule and shared items; the app's own follow).

| Command | What it does | Exit codes |
| --- | --- | --- |
| `fleet sync --to vX.Y.Z` | Run from fleet at that version (the sections ship with the package; a different `--to` is refused). Replaces each marked block, inserts a missing one at the end of its heading (creating the heading at the end of the file if there is none), never changes a byte outside the markers, and writes `.fleet/version`: the version on line 1 and, on line 2, the commit its tag points at, which `tools/fleet` runs. The commit comes from the workflow pins at that version (`@<commit>   # vX.Y.Z`; pins at that version naming two commits are refused), else from `git ls-remote` of the tag, or from `--commit <sha>`, which must match the pins. Running it twice changes nothing. | 0, 2 a marker error (an unclosed, nested, repeated or unknown block), another version, or no commit for the tag |
| `fleet drift` | Every block byte-equal to fleet's at the version in its marker (read from this fleet, or from its git history for an older tag), every block fleet ships present, the markers and `.fleet/version` at one version, and that version not newer than any `uses: AnthonyPluth/fleet/…@<sha>   # vX.Y.Z` pin in `.github/workflows/` and in the same major (it may trail the pins while a sync pull request is open), and `.fleet/version`'s commit (line 2) the one the pins at that version name. A missing or malformed commit fails (from v1.1.0 on; v1.0.0 wrote none; spaces and tabs around it are read as `tools/fleet`'s `read -r` reads them, but a carriage return or other whitespace fails), and so do pins at one version that name two commits, and a pin at that version by a ref that can move (`@main`, a tag). With no fleet pin, there is no pin check. | 0, 1 drift (one problem per line on stderr, `::error::` on GitHub Actions), 2 no app root |

To adopt: delete the app's copies of the shared bullets, put an empty pair of markers (`<!-- fleet:begin <section> v0.0.0 -->` and `<!-- fleet:end <section> -->` on the next line) where each block should go, above the app's own items under that heading, and run `fleet sync --to vX.Y.Z`. A section without markers is inserted at the end of its heading instead. A line that mentions a marker anywhere else is a marker error, so write about them without the `<!--` form. Commit `AGENTS.md` and `.fleet/version` together.

`fleet sync` and `fleet drift` also cover the plugin files fleet vendors into the app and the `ref` of fleet's marketplace in `.claude/settings.json` ([Claude Code plugin](#claude-code-plugin)).

## Claude Code plugin

fleet's Claude Code plugin (`plugins/fleet/`, listed by the marketplace in `.claude-plugin/marketplace.json`) holds the skills every app shares (`fix-with-checks`, `design-signoff`, `agent-task`, `review-guidance`, `verify-change`, `verification-guide`), the `fleet-reviewer` agent (the first review's prompt run locally on `git diff origin/main...HEAD` with the app's `.github/agent-review/context.md`; read-only and advisory) and the session-start hook. A cloud session doesn't install the plugins a repository enables, but loads the repository's own `.claude/skills/`, `.claude/agents/` and `.claude/settings.json` hooks, and the agents run in cloud sessions. So `fleet sync --to vX.Y.Z` also vendors the plugin into the app at that version:

| Vendored file | From fleet's |
| --- | --- |
| `.claude/skills/fleet-<name>/` (prefixed, so the app's own skills never clash) | `plugins/fleet/skills/<name>/` |
| `.claude/agents/fleet-reviewer.md` | `plugins/fleet/agents/fleet-reviewer.md` |
| `.claude/hooks/fleet-session-start.sh` (executable) | `plugins/fleet/hooks/session-start.sh` |
| `tools/fleet` (executable) | `shim/fleet`: runs `uvx --from "git+https://github.com/AnthonyPluth/fleet@<commit>" fleet "$@"` with the commit on `.fleet/version`'s line 2, installing uv first when it's missing; with no commit there it fails, naming the `fleet sync` that writes it, and never falls back to the tag |

Each starts with a line (after the `#!` line or the front matter's opening `---`) saying it's vendored from fleet at that version and is edited in fleet. `fleet drift` fails when one is missing, differs by a byte from fleet's at `.fleet/version`, has lost its executable bit, or when a file under the `fleet-` names (`.claude/skills/fleet-*/`, `.claude/agents/fleet-*`, `.claude/hooks/fleet-*`) isn't one fleet ships; `fleet sync` replaces them and removes a vendored file a later fleet no longer ships, never the app's own. A fleet version from before the plugin vendors nothing. Commit the vendored files with `AGENTS.md` and `.fleet/version`.

The hook acts only in a cloud session (`CLAUDE_CODE_REMOTE=true`) and only on what `.claude/hooks/fleet.conf` asks for; with no `fleet.conf` it does nothing. `fleet.conf` is a shell file the hook sources:

| Key | What the hook does with it |
| --- | --- |
| `PYTHON_VERSION` | Installs uv and Poetry (pinned) when missing, finds that Python (or has uv fetch it), points Poetry's environment at it, remakes the environment with that Python's own venv when Poetry picked another, and runs `poetry install --no-root --no-interaction`. Empty: no Python step |
| `FRONTEND_DIRS` | In each, space-separated: `npm install --no-save --no-audit --no-fund` (`--no-save`: a session must not rewrite the committed lockfile) |
| `APT_PACKAGES` | Installs the ones `dpkg` doesn't have, with `apt-get`, then `sudo apt-get`; a failure only warns |
| `NODE_CHECK` | What a complete `node_modules` has, space-separated: a package name (it must import from the directory) or a file under `node_modules` (`esrap/types/public.d.ts`: it must exist). When one is missing, the directory is reinstalled with `npm ci` and checked again |

`.claude/hooks/session-start.local.sh`, when present, is sourced last with the configuration set, for the app's own steps (`npx playwright install`, say). What each app sets:

```sh
# Waypoint
PYTHON_VERSION=3.14
FRONTEND_DIRS="frontend"
APT_PACKAGES="libfaketime"

# Runway: the same, plus the completeness check its own hook ran
NODE_CHECK="esrap/types/public.d.ts eslint-plugin-svelte"
```

`.claude/settings.json` runs the vendored hook and declares fleet's marketplace pinned to the tag the workflow pins carry, with no `enabledPlugins` entry:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {"type": "command", "command": "\"$CLAUDE_PROJECT_DIR/.claude/hooks/fleet-session-start.sh\""}
        ]
      }
    ]
  },
  "extraKnownMarketplaces": {
    "fleet": {"source": {"source": "github", "repo": "AnthonyPluth/fleet", "ref": "v2.3.0"}}
  }
}
```

The plugin isn't enabled: the vendored copies already give every session the skills, the agent and the hook, and enabling the plugin as well would load each skill twice in a local session (once as `fleet-<name>`, once as `fleet:<name>`) and run the hook twice. Declaring the marketplace lets someone browse or install it by hand. The marketplace stays at the tag: Claude Code's marketplace `ref` is a branch or tag (it clones with `git clone --branch`), not a commit, and nothing a session runs comes from it. `fleet sync --to vX.Y.Z` moves that `ref` to `vX.Y.Z` (only the value; nothing else in the file changes) for an entry under `extraKnownMarketplaces` whose `source` is `github` and `repo` is `AnthonyPluth/fleet`, and `fleet drift` fails while it names another ref than `.fleet/version`'s version; settings that declare no such marketplace, or one without a `ref`, are left alone. A `ref` that isn't a string fails `fleet sync` (exit 2) before it writes anything. The app deletes its own `.claude/hooks/session-start.sh`; its own skills (`verify-waypoint`, say) stay.

## Checks

`.github/workflows/checks.yml` (`workflow_call`) replaces the app's own `fleet-checks` job: it checks out the app with its history (into `app/`, beside fleet's `.fleet/`, so the app's own `.fleet/version` is never in the fleet checkout's way), then runs `fleet checks` (with `--commits <base>..<head>` and `--pr-event "$GITHUB_EVENT_PATH"` on a pull request, plain on `main`; the description is read from the payload file, never from an expression) and `fleet drift`, both from fleet at the workflow's own pinned ref (the FLEET_REF convention, so a pull request can't choose which fleet checks it). `fleet drift` reads the blocks and vendored files of the version `.fleet/version` names from fleet's history, so the content may trail the workflow pins within one major while a sync pull request is open. `contents: read` only, no secrets. The caller keeps the triggers and the concurrency group and passes its runner.

```yaml
name: fleet checks

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
    types: [opened, synchronize, reopened, ready_for_review, edited]   # edited and ready_for_review: a fixed description, or a draft marked ready, re-runs the verification check

permissions: {}

concurrency:
  group: fleet-checks-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}

jobs:
  checks:
    permissions:
      contents: read
    uses: AnthonyPluth/fleet/.github/workflows/checks.yml@<commit>   # v2.3.0
    with:
      runs-on: ${{ vars.RUNS_ON != '' && github.actor != 'dependabot[bot]' && (!github.event.pull_request || github.event.pull_request.head.repo.full_name == github.repository && github.event.pull_request.user.login != 'dependabot[bot]') && vars.RUNS_ON || '"ubuntu-latest"' }}
```

| Input | Default | What |
| --- | --- | --- |
| `runs-on` | `"ubuntu-latest"` | The runner, as JSON |
| `working-directory` | `.` | The app's root within its checkout (fleet's self-test passes a fixture consumer) |
| `expect-drift` | `false` | Self-test only: the drift step passes only when `fleet drift` fails |
| `fleet-ref` | the workflow's `FLEET_REF` | The fleet ref to run the tools from (fleet's self-test only) |

## Verification records

"Done means checked" lives in the pull request's description, where no tool could read it. A verification record puts it in a table there, one dimension of the change per row, that `fleet checks` parses, the reviews read and the monthly report counts. It stays in the description: no per-pull-request file is committed. "Testing" keeps saying what was run (`make check`, specific tests); "Verification" says, per behaviour the change touches, whether it was verified and on what evidence, and names every gap. Agents write it with the `fleet-verify-change` skill (vendored by `fleet sync`) before marking the pull request ready; the `git-and-prs` block says so where the table below is set.

An app turns it on in `pyproject.toml`; the table's presence is the switch:

```toml
[tool.fleet.verification]
required = false                                        # true: a ready pull request without a record fails fleet checks
exempt-authors = ["dependabot[bot]", "fleet-bot[bot]"]  # the default; their pull requests are skipped
```

The record sits under a `## Verification` heading, between two marker lines, each alone on its line. HTML comments don't render on GitHub and survive edits in the web editor, so it reads as a plain table:

```md
## Verification

<!-- fleet-verification -->
| Dimension | Status | How | Evidence |
| --- | --- | --- | --- |
| Settings page shows the new toggle | verified | `make verify`, flow `settings-toggle` | `screenshots/flow-settings-toggle-desktop.png` |
| Toggle rejected without the permission | verified | `tests/test_settings.py` | `test_a_viewer_cannot_change_the_toggle` passed |
| Sync against a live account | not verified | no fake for the provider; a fake under `tests/fakes/` would make it testable | - |
<!-- /fleet-verification -->
```

The rules (`fleet_tools/verification.py`, which later tools reuse): the header is exactly `| Dimension | Status | How | Evidence |` and there is at least one row; Status is exactly `verified` or `not verified`; a `verified` row has a non-empty How (the command, flow, test or manual step) and Evidence (a test name, a flow name, a screenshot path, a command's output summary); a `not verified` row's How says why it couldn't be verified and what would make it verifiable; `-` and `—` count as empty; `\|` is a literal pipe in a cell; the markers start at the first column, and every table row (the header, the separator and each body row) starts with `|` at the first column; between the markers every line with an unescaped `|` is a row, so a line holding an unescaped `|` that doesn't start with `|` (indented, after a blockquote, list or heading marker or an HTML tag, or a row without its leading pipe) is rejected as ambiguous, and prose there escapes its pipes as `\|` or stays out; other lines between the markers, and everything outside them, are ignored; exactly one block per description; `\r\n` endings parse as `\n`. The only code fence the parser knows is a plain top-level one (three or more backticks or tildes at the start of a line, an info string allowed on the opener, the closer the same character at least as long with nothing else on its line), and the markers inside one don't count; anything fancier is rejected as ambiguous rather than guessed at (the parser doesn't emulate Markdown): a fence line that is indented, in a list item or blockquote or carries other text, a marker that is indented or prefixed, a row that doesn't start with `|`, a tab on a fence line, or an HTML comment that doesn't open and close on one line. This strictness is intentional and reaches the whole description, not only the record: a fenced example inside a list item or blockquote in the Testing section is rejected too, so write fences at the first column and outside lists; with `required = false` a rejection is only a warning. A description over 65,536 characters, a block over 100 rows or a cell over 1,000 characters is a parse error, never a silent cut. The description is untrusted: the parser is text processing only (nothing in a cell is run, fetched, followed or expanded), `fleet verification show` defuses mentions and HTML comments in its text output, and the check's messages name the line and the rule and never quote the description, escaped for workflow commands.

The check (`verification`) runs only where the table is set and only when `fleet checks` is given `--pr-event PATH`, which `checks.yml` passes on pull request events (`$GITHUB_EVENT_PATH`); on a push to `main` or a local run without it, it's silent. It reads `pull_request.body`, `pull_request.user.login` and `pull_request.draft` from the payload, skips an author in `exempt-authors`, and otherwise requires exactly one block that parses. With `required = false`, or on a draft pull request, each failure is a warning (`::warning::` on Actions, exit 0); with `required = true` on a ready pull request it's a problem like any other. Add `edited` and `ready_for_review` to the caller's `pull_request` types ([Checks](#checks)) so a fixed description re-runs it, and so a draft marked ready is checked as a ready pull request (with `required = true`, the warning becomes a problem there).

The app's pull request template keeps its "Testing" section and gains this one after it, with a comment that says what goes in each column and that every gap is listed as `not verified`:

```md
## Verification

<!-- One row per dimension of the change: each behaviour a user sees, each failure path, a migration, a permission, anything AGENTS.md calls an invariant. Status is `verified` or `not verified`. How: the command, flow, test or manual step you ran (for `not verified`: why it couldn't be, and what would make it verifiable). Evidence: a test name, a flow name, a screenshot path or a command's output summary; never a step you didn't run in this session. Every gap is a `not verified` row, never left out. Keep the two marker lines; `fleet checks` and the reviews read the table between them. -->
<!-- fleet-verification -->
| Dimension | Status | How | Evidence |
| --- | --- | --- | --- |
<!-- /fleet-verification -->
```

From advisory to required: set the table with `required = false` and `edited` and `ready_for_review` in the caller's types, read the warnings on a few agents' pull requests until the records are complete and honest, then flip `required = true`; a ready pull request without a parseable record then fails "fleet checks", which the merge gate sees, while drafts stay warnings. `fleet verification show --pr N` prints a record as the tools read it.

### Verification guides

A verification record is only as good as the agent's sense of how to verify that part of the app. A verification guide gives every session the same method for one area: which paths it covers, how to verify a change there, and what can't be verified yet and why. Guides are optional; the `guides` key under the same table turns them on, naming the guides directory (relative to the repository root and below it: `.`, `..` and an absolute path are refused; there is no default, and nothing about guides runs without it):

```toml
[tool.fleet.verification]
required = false
guides = "docs/verification"   # one guide per area: docs/verification/<area>.md
```

A guide is `<guides>/<area>.md`, the file name being the area's name (lowercase letters, digits and `-`):

```md
# Settings

The settings pages and the API behind them.

## Paths

- `src/app/settings/**`
- `tests/test_settings.py`

## How to verify

- `make verify`, flow `settings-toggle`: the page at phone and desktop widths.
- `tests/test_settings.py`: the API's validation and permission checks.

## Not verifiable yet

- The email sent on a change: no fake mail server; a fake under `tests/fakes/` would make it verifiable.
```

Line 1 is `# <Area>`, and prose may follow it; then `## Paths`, `## How to verify` and `## Not verifiable yet`, in that order, each once, with no other headings and no code fences, every line in a section a list item or an indented continuation of one. `## Paths` lists globs relative to the repository root, optionally in backticks (`*` and `?` within one directory, `**` a whole segment for any number of directories, a directory written `<dir>/**`; no spaces, brackets, braces, leading `/`, `./` or `!`). `## How to verify` lists steps, each naming a real command, flow, fake or test. `## Not verifiable yet` lists each gap with why and what would change it, or is the single item `Nothing`. The parser (`fleet_tools/verification_guides.py`) is text processing only: nothing in a guide is run, and globs are matched against path strings, the check's against `git ls-files`. Globs are matched segment by segment without regular expressions, in time linear in the pattern times the path, however the wildcards are arranged. A guide over 65,536 characters, a section over 200 items, or a glob over 1,000 characters or 64 segments is a parse error; a guide that is a link is refused.

| Command | What it does | Exit |
| --- | --- | --- |
| `fleet verification guides [--json]` | Lists each area (its name, title and guide) with its globs; `--json` adds the steps and the gaps (`not_verifiable_yet` is empty for `Nothing`) | 0, 1 a guide doesn't parse, 2 `guides` unset or invalid |
| `fleet verification areas [PATH...] [--stdin0] [--json]` | Names every area whose globs match one of the paths, with the paths each matched, then the paths no guide covers; pass both names of a renamed file. `--stdin0` also reads NUL-separated paths from stdin (up to 16 MiB), so `git diff -z --name-only --no-renames origin/main...HEAD \| fleet verification areas --json --stdin0` gives one result for the whole diff, however long; empty input is an empty result. Paths after `--` are paths even when they start with `-` | the same, and 2 for no paths at all or stdin over the limit |

The check `verification-guides` runs where `guides` is set, in a git checkout: each guide parses, and each glob matches at least one tracked file; a glob that matches nothing means the guide is stale, and is a problem. With `--commits`, the changed paths (`git diff -z --name-only --no-renames`, so both names of a renamed file) that no guide covers, outside the guides directory, are one warning (exit 0), never a problem. The `fleet-verify-change` skill reads the guides for the changed paths (`fleet verification areas --stdin0`) before it verifies anything: it follows each area's "How to verify", records a listed gap as `not verified` with the guide's reason, and a dimension verified some other way says why in its How column. The `fleet-verification-guide` skill writes or updates a guide, for an area the user names or for the paths the warning listed; every step it writes has been run once in that session, and every gap has a reason. It doesn't replace or edit the app's feature map.

## Content sync: two pins, two bots

An app carries two kinds of fleet pin. The **workflow pins** (`uses: AnthonyPluth/fleet/.github/workflows/<name>.yml@<sha>   # vX.Y.Z`) are bumped by Dependabot, in one `fleet` group so they move together; the merge gate passes such a pull request and leaves it for the owner to merge, or merges it itself with `automerge-fleet-bumps: true`. The **content** (the `AGENTS.md` blocks, the vendored `.claude/` files and `tools/fleet`, `.fleet/version`) is what `fleet sync` writes, and Dependabot can't edit it. `.github/workflows/sync.yml` (`workflow_call`) follows each bump: it reads every fleet pin in the app's workflows (they must agree; if they don't, it fails naming the files), compares with `.fleet/version` and, when the pins are newer in the same major, runs `fleet sync --to <pin>` from a plain clone of fleet at that tag (the planner validates the version and the pins' commit first, and the clone must be at that commit, so a moved tag fails the run; the one fleet checkout by the action stays at the workflow's pinned ref), which writes the pins' commit into `.fleet/version`, and opens, or updates, the pull request `chore: fleet content vX.Y.Z` from the branch `fleet/sync` (rewritten by each run until it merges), with the CHANGELOG entries in between in its body. Pins at a newer major open an issue instead, "fleet vN needs a migration: see CHANGELOG", with the entries to read and the `fleet sync` to run by hand. `fleet drift` lets the content trail the pins within one major, so the app's checks stay green while the sync pull request is open.

The second bot is the point: a pull request opened with a workflow's own `GITHUB_TOKEN` starts no workflows, so nothing would check it. The sync pushes and opens its pull request with the `fleet-bot` GitHub App's installation token instead (`FLEET-SETUP` created the App and installed it on the three repositories; the caller passes its `FLEET_BOT_APP_ID` variable as `app-id` and its `FLEET_BOT_PRIVATE_KEY` secret by name). The token is minted by `actions/create-github-app-token` for this repository alone, narrowed to contents and pull requests, used in one step after the content is written, and revoked when the job ends. The App has no Workflows permission, so it couldn't push a change under `.github/workflows/`; the workflow checks that the sync touched nothing there before it commits anyway. Nothing merges by itself: the merge gate and the owner decide. The migration issue uses the workflow's own token (`issues: write`). The sync always reads the default branch, whatever ref a manual run was started from. The caller runs it on a push to `main` that touches the workflows (a pin bump merged by hand), when the merge gate starts it after merging a bump (`automerge-fleet-bumps` with the caller in `after-merge-workflows`: a merge made with the gate's token starts no `push` workflow), weekly (a run that failed) and by hand.

```yaml
name: Sync fleet content

on:
  push:
    branches: [main]
    paths: [.github/workflows/**]
  schedule:
    - cron: "41 6 * * 1"
  workflow_dispatch:
    inputs:
      dry-run:
        description: "Print the plan and the pull request instead of opening it"
        type: boolean
        default: false

permissions: {}

concurrency:
  group: fleet-sync
  cancel-in-progress: false

jobs:
  sync:
    permissions:
      contents: read           # the checkout; the pull request is pushed with the App token
      issues: write            # the migration issue
    uses: AnthonyPluth/fleet/.github/workflows/sync.yml@<commit>   # v2.3.0
    with:
      app-id: ${{ vars.FLEET_BOT_APP_ID }}
      dry-run: ${{ inputs.dry-run || false }}
    secrets:
      FLEET_BOT_PRIVATE_KEY: ${{ secrets.FLEET_BOT_PRIVATE_KEY }}
```

| Input | Default | What |
| --- | --- | --- |
| `app-id` | none | The fleet-bot App's id (`vars.FLEET_BOT_APP_ID`); needed unless `dry-run` |
| `dry-run` | `false` | Print the plan, the diff and the pull request or issue; mint no token, push and open nothing |
| `fixture` | none | Self-test only: build a fixture consumer (`pins=v1.1.0 content=v1.0.0`) in place of the checkout; implies `dry-run` |
| `fleet-ref` | the workflow's `FLEET_REF` | Where to fetch the planner from (fleet's self-test only) |

Secret, passed by name: `FLEET_BOT_PRIVATE_KEY`. Outputs: `action` (`none`, `sync`, `migration`), `title`, `changed` (the sync commit's files, base64) and `body` (base64). `fleet sync-plan --root APP [--changelog CHANGELOG.md --body FILE]` is the planner (`fleet_tools/sync_workflow.py`): it prints the plan as `key=value` lines and exits 1 when the pins disagree, a pin has no version comment, the pins at one version name two commits or any of them is a ref that can move (`@main`, a tag), `.fleet/version` is missing or malformed, or the content is newer than the pins. The plan's `commit` is the pins' commit; pins at the content's version but another commit than `.fleet/version`'s plan a sync too, so the tag-versus-pin check runs.

## Verify (control smoke, verify flows)

`.github/workflows/verify.yml` (`workflow_call`) runs one `make` target of the app in a real browser against its demo server: Waypoint's `make control-smoke`, Runway's `make verify`. It sets up Python with Poetry's virtualenv (cached by `poetry.lock`), Node with the app's packages (cached by `package-lock.json`), the Playwright browser and libfaketime, then runs the target. `contents: read` only, no secrets. The caller keeps the workflow name (the status the merge gate lists), the triggers with their path filters and the concurrency group; its job name is what the status shows.

```yaml
name: Control smoke

on:
  push:
    branches: [main]
    paths: [frontend/verify/**, waypoint/verify.py, tools/control, poetry.lock, frontend/package-lock.json, .github/workflows/control-smoke.yml]
  pull_request:
    branches: [main]
    paths: [frontend/verify/**, waypoint/verify.py, tools/control, poetry.lock, frontend/package-lock.json, .github/workflows/control-smoke.yml]
  workflow_dispatch:

permissions: {}

concurrency:
  group: control-smoke-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}

jobs:
  smoke:
    name: Control smoke
    permissions:
      contents: read
    uses: AnthonyPluth/fleet/.github/workflows/verify.yml@<commit>   # v2.3.0
    with:
      make-target: control-smoke
      timeout-minutes: 20
      runs-on: ${{ vars.RUNS_ON != '' && github.actor != 'dependabot[bot]' && (!github.event.pull_request || github.event.pull_request.head.repo.full_name == github.repository && github.event.pull_request.user.login != 'dependabot[bot]') && vars.RUNS_ON || '"ubuntu-latest"' }}
```

Runway's "Verify flows" passes `make-target: verify` and `poetry-install-args: --only main`, and keeps its own paths.

| Input | Default | What |
| --- | --- | --- |
| `make-target` | required | The `make` target to run |
| `working-directory` | `.` | Where `make` and `poetry install` run (the self-test's fixture tree) |
| `needs-faketime` | `true` | Install libfaketime first |
| `timeout-minutes` | 15 | The job's limit |
| `runs-on` | `"ubuntu-latest"` | The runner, as JSON |
| `python-version` | `3.14` | The Python to set up; `""` sets up none |
| `poetry-version` | `2.5.1` | The Poetry to install |
| `poetry-install-args` | none | Extra arguments for `poetry install` (`--only main`) |
| `node-dir` | `frontend` | The npm project to `npm ci` in; `""` sets up no Node |
| `node-version-file` | `.nvmrc` | Where the Node version is read from |
| `browser` | `chromium` | The Playwright browser to install, with its system dependencies; `""` installs none |
| `fleet-ref` | the workflow's `FLEET_REF` | Kept for the convention (fleet's self-test only); nothing of fleet's runs here |

## Screenshot diff

`.github/workflows/screenshot-diff.yml` (`workflow_call`) reports which pixels of the committed screenshots a pull request changed: a self-contained `report.html` and `summary.json` as artifacts, and one sticky comment (the link, a line of counts and a note) found again by its marker. It is informational, never a verdict: list it in the merge gate's `ignored-workflows`. The tool (`fleet screenshot-diff`, `fleet_tools/screenshot_diff.py`) is installed from fleet's checkout at the pinned ref with its Pillow extra and run with `python -I`, so the pull request's files can't change it; the images are the only pull request content it reads. It stays on `pull_request` (a fork's read-only token skips the comment and keeps the artifact). The caller runs it on every pull request, not only ones that touch the screenshots, so a push that reverts a change updates the comment.

```yaml
name: Screenshot diff

on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions: {}

concurrency:
  group: screenshot-diff-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  diff:
    permissions:
      contents: read
      pull-requests: write
    uses: AnthonyPluth/fleet/.github/workflows/screenshot-diff.yml@<commit>   # v2.3.0
    with:
      dir: screenshots/
```

| Input | Default | What |
| --- | --- | --- |
| `dir` | `screenshots/` | Where the committed screenshots live (`docs/src/assets/screenshots/` for Waypoint) |
| `comment-marker` | `<!-- screenshot-diff -->` | The hidden first line the comment is found again by |
| `runs-on` | `"ubuntu-latest"` | The runner, as JSON |
| `dry-run` | `false` | Print the comment to the log instead of posting it |
| `fixture` | none | Two PNG directories to compare in a made-up repository (fleet's self-test only) |
| `fleet-ref` | the workflow's `FLEET_REF` | Where to install the tool from (fleet's self-test only) |

Locally: `python3 -m fleet_tools.cli screenshot-diff --base origin/main --head HEAD --dir screenshots/ --repo /path/to/app --dry-run`, with Pillow installed (`pip install "fleet[images]"` from a checkout of fleet).

## Cancel runs on close

`.github/workflows/cancel-on-close.yml` (`workflow_call`) stops a closed pull request's runs that are still going by joining their concurrency groups, one matrix job per group, with `cancel-in-progress`. The groups are built from the pull request's number, never from `github.ref` (on a merged pull request's closed event that is `main`'s). No permission is needed. The caller keeps the trigger.

```yaml
name: Cancel runs on close

on:
  pull_request:
    types: [closed]

permissions: {}

jobs:
  cancel:
    uses: AnthonyPluth/fleet/.github/workflows/cancel-on-close.yml@<commit>   # v2.3.0
    with:
      groups: '["docker-image-refs/pull/{0}/merge", "security-refs/pull/{0}/merge", "verify-{0}"]'
```

| Input | Default | What |
| --- | --- | --- |
| `groups` | required | A JSON array of group patterns; `{0}` is the pull request's number, and a pattern without it is a prefix the number is appended to |
| `runs-on` | `"ubuntu-latest"` | The runner, as JSON |
| `fleet-ref` | the workflow's `FLEET_REF` | Kept for the convention (fleet's self-test only) |

## Tidy releases

`.github/workflows/tidy-releases.yml` (`workflow_call`) takes back versions that shouldn't have been released: each one's GitHub release, its tag and its image in the GitHub Container Registry, after pointing floating image tags elsewhere if asked. It never deletes the newest version, nor an image still tagged `latest` or with a floating tag it wasn't asked to move. The caller keeps the `workflow_dispatch` inputs and the concurrency group it shares with its `docker.yml` on `main`, so it waits for a release in progress (check that no Docker image run is queued on `main` first, and don't push to `main` while it waits).

```yaml
name: Tidy releases

on:
  workflow_dispatch:
    inputs:
      versions:
        description: "Versions to delete, separated by spaces (v1.0.2 v2.0.4): the release, its tag and its image"
        required: true
      retag:
        description: "Floating image tags to point elsewhere first, as tag=version (2=2.3.21 1.0=1.0.1)"
        required: false
        default: ""
      confirm:
        description: 'Type "delete" to confirm'
        required: true

permissions: {}

concurrency:
  group: docker-image-refs/heads/main
  cancel-in-progress: false

jobs:
  tidy:
    permissions:
      contents: write
      packages: write
    uses: AnthonyPluth/fleet/.github/workflows/tidy-releases.yml@<commit>   # v2.3.0
    with:
      image: waypoint
      versions: ${{ inputs.versions }}
      retag: ${{ inputs.retag }}
      confirm: ${{ inputs.confirm }}
```

| Input | Default | What |
| --- | --- | --- |
| `image` | required | The container image in the owner's registry (`waypoint`, `runway`) |
| `versions`, `retag`, `confirm` | | The dispatch inputs, passed through; `confirm` must be `delete` |
| `runs-on` | `"ubuntu-latest"` | The runner, as JSON |
| `fleet-ref` | the workflow's `FLEET_REF` | Kept for the convention (fleet's self-test only) |

Runway's preview (`preview.yml`, a live copy of its own image behind a Cloudflare quick tunnel) stays in Runway: nothing for the others to reuse yet.
