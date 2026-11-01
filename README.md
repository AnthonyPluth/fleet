# fleet

Shared agent build safeguards for Darkroom, Waypoint and Runway.

Waypoint and Runway each carry their own copy of the same automation: the independent agent review, the merge gate, the security scans, the fleet checks, the session-start hook and the shared parts of `AGENTS.md`. fleet holds one copy of it. The apps use it by pinned reference only: they call fleet's reusable workflows pinned by commit SHA with a `# vX.Y.Z` comment, and run its tools at the same tag.

## The pieces

- `fleet_tools/`: the `fleet` command. Standard library only (Python 3.11 or newer), so a runner's or a cloud container's system `python3` runs it without Poetry (`fleet screenshot-diff` alone needs Pillow, the optional extra `fleet[images]`). A consumer configures it in a `[tool.fleet]` table in its own `pyproject.toml`.
- `.github/workflows/`: fleet's own CI (`ci.yml`, "fleet CI"), the self-test of the reusable workflows (`self-test.yml`, "Self-test"), and the reusable workflows the apps call.
- `.github/agent-review/`, `.github/scripts/`: the review prompts and the scripts the reusable workflows run.
- `.claude-plugin/`, `plugins/fleet/`: the Claude Code plugin (skills, the reviewer agent, the session-start hook), vendored into each app.
- `agents-md/sections/`: the `AGENTS.md` blocks the apps share.
- `docs/`: [adopting fleet](docs/adopting.md), [where the apps differed](docs/drift.md), [the agent review](docs/agent-review.md), [review mining](docs/review-mining.md), the owner's [deep-scan runbook](docs/deep-scan.md) and [releasing](docs/release.md).

Most of these arrive in later issues; this repository starts with the scaffold.

## The FLEET_REF convention

A called workflow can't learn its own ref: `github.workflow_ref` and `github.workflow_sha` describe the caller. So every reusable workflow (`on: workflow_call`) carries a top-level `env: FLEET_REF: <tag>` and an optional `fleet-ref` input, and a workflow that runs fleet's own files checks out `AnthonyPluth/fleet` at `inputs.fleet-ref || env.FLEET_REF` into `.fleet/` (sparse, `persist-credentials: false`) before the first step that uses one, after the caller's own checkout (which would delete `.fleet/`); that is where its scripts, prompts and tools come from, and a workflow that runs none has no such step. The release stamps the tag; until v1.0.0 the value is `main`. `fleet self-check` holds it: it fails when a reusable workflow has no `FLEET_REF`, when the value isn't `v` + `VERSION` (or `main` while `VERSION` is 0.x), and when `self-test.yml` calls a reusable workflow without `fleet-ref: ${{ github.sha }}`.

## Working on fleet

```sh
make setup   # Poetry's environment and the pinned actionlint
make check   # ruff, mypy, the tests, fleet self-check, actionlint and zizmor
```

Read [AGENTS.md](AGENTS.md) first. Adopting fleet in an app: [docs/adopting.md](docs/adopting.md).

## License

AGPL-3.0, see [LICENSE](LICENSE).
