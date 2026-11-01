# Adopting fleet

How an app calls fleet's reusable workflows, configures `[tool.fleet]` in its `pyproject.toml` and syncs fleet's content. Filled in as the pieces land; complete with the v1.0.0 release (`FLEET-RELEASE`).

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
