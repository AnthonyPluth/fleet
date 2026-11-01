# Adopting fleet

How an app calls fleet's reusable workflows, configures `[tool.fleet]` in its `pyproject.toml` and syncs fleet's content. Filled in as the pieces land; complete with the v1.0.0 release (`FLEET-RELEASE`).

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
    uses: AnthonyPluth/fleet/.github/workflows/security.yml@<commit>   # vX.Y.Z
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
| `sarif-upload` | `true` | Upload findings to the Security tab; `false` fails the job on findings instead |
| `scorecard` | `true` | Run OpenSSF Scorecard (public repositories only) |
| `runs-on` | `"ubuntu-latest"` | The runner for every job but Scorecard's, as JSON |
| `fleet-ref` | the workflow's `FLEET_REF` | The fleet ref to fetch scripts from (fleet's self-test only) |

## Deep-scan reminder

`.github/workflows/deep-scan-due.yml` (`workflow_call`) opens one issue, "Deep security scan due" (labels `manual`, `security`), unless one is open, with the link to fleet's [deep-scan runbook](deep-scan.md) at the pinned ref, the revision of the last scan (read from the last closed issue with that title) and the number of commits on the default branch since. The caller schedules it; `issues: write` is all it needs, and no secrets. Both labels must exist in the repository.

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
      issues: write
    uses: AnthonyPluth/fleet/.github/workflows/deep-scan-due.yml@<commit>   # vX.Y.Z
```

Inputs: `dry-run` (default `false`; print the issue instead of opening it) and `fleet-ref`.


## The `fleet` command

`fleet_tools` is standard library only (Python 3.11 or newer), so a runner's system `python3` runs it from a checkout of fleet with nothing installed:

```sh
python3 -E -s -m fleet_tools.cli checks --root /path/to/app --commits origin/main..HEAD
```

Each command finds the app's root as the nearest `pyproject.toml` up from the working directory, unless `--root` names it.

| Command | What it does | Exit codes |
| --- | --- | --- |
| `fleet checks [--commits BASE..HEAD]` | Every check the app's `[tool.fleet]` configures. With `--commits` (a pull request's range; BASE is merged with HEAD to find where the branch left), also the commit trailers, removed or skipped tests and committed images. The one entry point for `make fleet-checks` and CI. | 0 all passed (the line lists what ran), 1 problems (one per line on stderr, `::error::` on GitHub Actions), 2 a config error or a failing `git` |
| `fleet no-comments` | No comments or docstrings in tracked Python, CSS and Svelte. Opt-in: does nothing without `[tool.fleet.no-comments]`. | 0, 1, 2 |
| `fleet coverage-floor [coverage.json]` | coverage.py's JSON report against `[tool.fleet.coverage]`. | 0, 1, 2 (no floors, or no report) |
| `fleet next-version [--sha] [--output]` | The next release version from the commits since the last tag (`feat:` a minor; `!:`, `BREAKING CHANGE:` or a trailing `#major` a major; else a patch; a trailing `#minor` a minor). Appends `newest`, then `version` and `new`, or `skip`, to `$GITHUB_OUTPUT`. Replaces `.github/scripts/next-version.sh`. | 0, 1 a failing `git` |
| `fleet test-timings DIR [--top N]` | Sums up the per-test timing files a timing test result wrote. | 0 |

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
| `[[tool.fleet.generated]]` | skills | A generated file: `path`, the `command` that prints what it should hold (run from the app's root), and optionally `fix`, the command that rewrites it. |
| `feature-maps` | skills | Python files that define `OUT` (the map's path), `generate()` (its text and any problems) and `check(committed, generated)` (the problems). |
| `extra-checks` | the app's own | `module:NAME`, a list of callables in the app's tree (`tools/app_checks.py`, or a package's `__init__.py`). Each takes the check context (`.root`, `.config`, `.commits`) and returns a list of problem strings, reported like fleet's own. A missing module or name, a failed import or a check that raises is a problem, never a silent pass. |
| `[tool.fleet.no-comments]` | `fleet no-comments` | `paths`: what to read (default: every tracked file); `skip`: substrings of paths to leave out (default `.semgrep/examples/`, `node_modules/`). |
| `[tool.fleet.coverage]` | `fleet coverage-floor` | `overall`: the floor for the whole report; `modules`: path prefix to floor, for each module with statements (the longest matching prefix wins). |

The workflow conventions check runs wherever `.github/workflows/` or `.github/actions/` exists: bash as each workflow's default shell, every action pinned by its commit SHA with its version in a comment, `gh api ... per_page=` with `--paginate`, no `runner`, `steps`, `job` or `env` expression in a workflow's or job's `env:`, and every `runs-on:` either fleet's `vars.RUNS_ON` line (`fleet_tools/checks/workflows.py`) or `runs-on: ubuntu-latest   # hosted: <why it needs GitHub's runners>`.

## Merge gate

`.github/workflows/merge-gate.yml` (`workflow_call`) sets one commit status, "Merge gate", on a pull request's head: passing once the newest run of every workflow the pull request started has succeeded (and the required ones ran at all), every other app's check run and every commit status has passed (and the required statuses are set), pending while anything is still running, failing otherwise. Require that one status in the ruleset on `main`. It merges Dependabot's pull requests once it passes, except one that changes an `AnthonyPluth/fleet/` reference, which it leaves for the owner's Code Owner review. The rules are in `.github/scripts/merge-gate.sh`, which the workflow fetches from fleet at its pinned ref.

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
    uses: AnthonyPluth/fleet/.github/workflows/merge-gate.yml@<commit>   # vX.Y.Z
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
| `required-workflows` | `Security scans`, `CodeQL` | Workflows that must have run, one per line. CodeQL isn't required on Dependabot's pull requests (its default setup skips them) |
| `ignored-workflows` | none | Workflows whose runs don't count, as a regex alternation (`Preview\|Screenshot diff`). `Merge gate` and `Cancel runs on close` never count |
| `required-statuses` | `Agent review`, `Second review` | Commit statuses that must be set on the head, one per line. None are required on Dependabot's pull requests. An empty string requires none |
| `ignored-apps` | `codecov\|github-actions` | Apps whose check runs don't count (Codecov's are informational; Actions' runs are counted as workflows) |
| `poll-seconds`, `poll-attempts` | 30, 20 | How long to look again for CodeQL, whose finishing starts no workflow |
| `automerge-dependabot` | `true` | Merge Dependabot's pull requests once the gate passes (every commit Dependabot's own and verified, same repository, not a draft, no fleet reference changed) |
| `after-merge-workflows` | `docker.yml security.yml` | Workflow files to start on `main` after such a merge, space-separated; a merge made with the token starts none by itself |
| `dry-run` | `false` | Print the status posts, the merge and the workflow starts instead of doing them |
| `fleet-ref` | the workflow's `FLEET_REF` | Where to fetch the script from; the self-test passes its own commit |

What each app passes today: Waypoint `required-workflows: Docker image, Security scans, CodeQL` and `required-statuses: ""` (its review is off); Runway the same workflows, `required-statuses: Agent review` and `ignored-workflows: Preview|Screenshot diff`. Both move to the default statuses when they adopt the reviews.

## AGENTS.md blocks

fleet ships the parts of `AGENTS.md` the apps share as blocks, one file each in `agents-md/sections/` (`starting-work`, `working-in-a-container`, `mistakes`, `git-and-prs`), with front matter naming the `## ` heading it lives under. In the app's `AGENTS.md` a block sits between markers:

```md
## Mistakes that keep coming back

<!-- fleet:begin mistakes v0.1.0 -->
…fleet's text, verbatim…
<!-- fleet:end mistakes -->

- **An app item** (human-judged: why no check can express it). …
```

Inside the markers is fleet's file at the marker's version, byte for byte; everything outside them is the app's. App items go after a block, under the same heading ("Mistakes that keep coming back" opens with fleet's rule and shared items; the app's own follow).

| Command | What it does | Exit codes |
| --- | --- | --- |
| `fleet sync --to vX.Y.Z` | Run from fleet at that version (the sections ship with the package; a different `--to` is refused). Replaces each marked block, inserts a missing one at the end of its heading (creating the heading at the end of the file if there is none), never changes a byte outside the markers, and writes `.fleet/version`. Running it twice changes nothing. | 0, 2 a marker error (an unclosed, nested, repeated or unknown block) or another version |
| `fleet drift` | Every block byte-equal to fleet's at the version in its marker (read from this fleet, or from its git history for an older tag), every block fleet ships present, the markers and `.fleet/version` at one version, and that version not newer than any `uses: AnthonyPluth/fleet/…@<sha>   # vX.Y.Z` pin in `.github/workflows/` and in the same major (it may trail the pins while a sync pull request is open). With no fleet pin, there is no pin check. | 0, 1 drift (one problem per line on stderr, `::error::` on GitHub Actions), 2 no app root |

To adopt: delete the app's copies of the shared bullets, put an empty pair of markers (`<!-- fleet:begin <section> v0.0.0 -->` and `<!-- fleet:end <section> -->` on the next line) where each block should go, above the app's own items under that heading, and run `fleet sync --to vX.Y.Z`. A section without markers is inserted at the end of its heading instead. A line that mentions a marker anywhere else is a marker error, so write about them without the `<!--` form. Commit `AGENTS.md` and `.fleet/version` together.
