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
