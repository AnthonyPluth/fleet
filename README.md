# fleet

Shared agent build safeguards for Darkroom, Waypoint and Runway.

Waypoint and Runway each carry their own copy of the same automation: the independent agent review, the merge gate, the security scans, the fleet checks, the session-start hook and the shared parts of `AGENTS.md`. fleet holds one copy of it. The apps use it by pinned reference only: they call fleet's reusable workflows pinned by commit SHA with a `# vX.Y.Z` comment, and run its tools at the same tag.

## The pieces

- `fleet_tools/`: the `fleet` command. Standard library only (Python 3.11 or newer), so a runner's or a cloud container's system `python3` runs it without Poetry (`fleet screenshot-diff` alone needs Pillow, the optional extra `fleet[images]`). A consumer configures it in a `[tool.fleet]` table in its own `pyproject.toml`.
- `.github/workflows/`: fleet's own CI (`ci.yml`, "fleet CI"), the self-test of the reusable workflows (`self-test.yml`, "Self-test"), and the reusable workflows the apps call.
- `.github/agent-review/`, `.github/scripts/`: the review prompts and the scripts the reusable workflows run.
- `.claude-plugin/`, `plugins/fleet/`: the Claude Code plugin (skills, the reviewer agent, the session-start hook), vendored into each app by `fleet sync` with `shim/fleet` as the app's `tools/fleet` ([adopting](docs/adopting.md#claude-code-plugin)).
- `agents-md/sections/`: the `AGENTS.md` blocks the apps share.
- `docs/`: [adopting fleet](docs/adopting.md), [where the apps differed](docs/drift.md), [the agent review](docs/agent-review.md), [review mining](docs/review-mining.md), [loop stats](docs/loop-stats.md), [where skills live](docs/skills.md), the owner's [deep-scan runbook](docs/deep-scan.md) and [releasing](docs/release.md).

Most of these arrive in later issues; this repository starts with the scaffold.

## The FLEET_REF convention

A called workflow learns its own commit from `job.workflow_sha`: the commit of the reusable workflow's own file, which is the commit the caller pins. `github.workflow_ref` and `github.workflow_sha` describe the caller. Every reusable workflow (`on: workflow_call`) has an optional `fleet-ref` input. A workflow that runs fleet's own files checks out `AnthonyPluth/fleet` at `inputs.fleet-ref || job.workflow_sha` into `.fleet/` (sparse, `persist-credentials: false`). The checkout comes after the caller's own checkout (which would delete `.fleet/`) and before the first step that uses a fleet file, and after a `Check fleet's commit` step that fails rather than let an empty value check out fleet's default branch. That checkout is where the workflow's scripts, prompts and tools come from; a workflow that runs none has no such step. Each reusable workflow also still carries a top-level `env: FLEET_REF: <tag>`, which the release stamps and nothing reads; it goes when fleet releases on every merge (#112). `fleet self-check` holds all of this. It fails when a reusable workflow has no `FLEET_REF` or its value isn't `v` + `VERSION` (or `main` while `VERSION` is 0.x). It also fails when a workflow reads `FLEET_REF`, checks out `AnthonyPluth/fleet` at any other ref or without the guard step, and when `self-test.yml` calls a reusable workflow without `fleet-ref: ${{ github.sha }}` (all but `checks-own-commit`, which runs the default).

## Working on fleet

```sh
make setup   # Poetry's environment, the pinned actionlint and the locked Claude Code
make check   # ruff, mypy, the tests, fleet self-check, actionlint, zizmor and claude plugin validate
```

Read [AGENTS.md](AGENTS.md) first. Adopting fleet in an app: [docs/adopting.md](docs/adopting.md).

## License

AGPL-3.0, see [LICENSE](LICENSE).
