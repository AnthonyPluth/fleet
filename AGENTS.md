# Agent guide

Instructions for AI coding agents working in fleet, the shared agent build safeguards of Darkroom, Waypoint and Runway: reusable workflows, the `fleet` command (`fleet_tools/`), the Claude Code plugin and the shared `AGENTS.md` blocks. The apps call it by pinned reference. Human contributors can use this guide too. Parts of it will be replaced by fleet's own synced blocks (`agents-md/sections/`).

## Layout

- `fleet_tools/`: the `fleet` command. `cli.py` holds the subcommand registry (`COMMANDS`), `config.py` reads a consumer's `[tool.fleet]` table, `self_check.py` checks fleet's own conventions. `checks/` holds `fleet checks`, one module per concern, and `runner.py` runs them; `no_comments.py`, `coverage_floor.py`, `next_version.py` and `test_timings.py` are the other commands.
- `.github/workflows/`: `ci.yml` ("fleet CI", `make check`), `self-test.yml` ("Self-test", calls each reusable workflow by local path), and the reusable workflows beside them.
- `tests/`: the `unittest` suite. `fixtures/consumer-waypoint/` and `fixtures/consumer-runway/` are minimal trees with each app's layout and `[tool.fleet]`; `fixtures/workflows/` holds workflows for `fleet self-check`'s tests; `fakes/` holds fakes of outside services.
- `docs/`: adopting fleet, the drift between the apps and how fleet resolved it, and the release process.

## Commands

- `make setup`: Poetry's environment (the dev tools, pinned in `pyproject.toml`) and the pinned actionlint binary in `.tools/`.
- `make check`: ruff, mypy (strict), the tests, `fleet self-check`, actionlint and zizmor over `.github/workflows/`. Run it before pushing; CI runs the same.
- `make fix`: ruff's safe fixes; review the diff afterwards.

## Rules

- **Nothing personal in the repository, its issues or its pull requests.** No household member's name, no host or domain of anyone's own, no account, email address, token or key, and nothing from a real instance of an app. Fixtures and examples use made-up values. The review of each pull request checks it.
- **Every change is a change to three repositories' CI.** Each one adds a line to `CHANGELOG.md`'s Unreleased section.
- **A change to an input, a status name or an AGENTS block is a breaking change** unless it keeps the old behaviour by default. Prefer adding an input with the old behaviour as its default over changing what an existing one does.
- **fleet follows the stricter convention where the apps differ:** no comments or docstrings in Python; every action pinned by full commit SHA with its version in a comment; `bash` (which runs each step with `-eo pipefail`) as each workflow's default shell; `persist-credentials: false` on every checkout; `permissions: {}` at the top of a workflow and the least each job needs.
- **The FLEET_REF convention.** Every reusable workflow has a top-level `env: FLEET_REF`, an optional `fleet-ref` input, and, in any workflow that runs fleet's own files (scripts, prompts, tools), a step that checks out `AnthonyPluth/fleet` at `inputs.fleet-ref || env.FLEET_REF` into `.fleet/` (sparse, `persist-credentials: false`), after the caller's own checkout (which cleans the workspace and would delete `.fleet/`) and before the first step that uses a fleet file. A workflow that runs none of fleet's files has no such step. Until v1.0.0 the value is `main`; the release stamps `v` + `VERSION`. `self-test.yml` calls each one with `fleet-ref: ${{ github.sha }}`. `fleet self-check` fails when any of this drifts.
- **`fleet_tools` stays standard library only**, on Python 3.11 or newer, so a runner's system `python3` runs it without Poetry. The one exception is `fleet screenshot-diff`, whose Pillow is the optional extra `images` (`pip install "fleet[images]"`) that only `screenshot-diff.yml` installs.
- **A correction becomes a check.** When a mistake is pointed out, fix it and add the test or `fleet self-check` rule that would have caught it.

## Starting work

- **One issue, one session, one pull request.** Work starts from an issue made with the "Agent task" template (`.github/ISSUE_TEMPLATE/agent-task.md`), on the model it names ([CLAUDE.md](CLAUDE.md)).
- **Decisions belong in the issue.** If it doesn't settle something a consumer would notice, ask instead of choosing.
- **Done means checked.** The pull request shows `make check`'s output and the `fleet CI` and `Self-test` results, not a description of them.

## Git and PRs

- Never push to `main`. Open a pull request for every change, from a branch, using `.github/pull_request_template.md`.
- Small, logical commits.
