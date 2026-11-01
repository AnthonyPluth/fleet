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
