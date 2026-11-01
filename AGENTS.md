# Agent guide

Instructions for AI coding agents working in fleet, the shared agent build safeguards of Darkroom, Waypoint and Runway: reusable workflows, the `fleet` command (`fleet_tools/`), the Claude Code plugin and the shared `AGENTS.md` blocks. The apps call it by pinned reference. Human contributors can use this guide too. The text between the `fleet:begin` and `fleet:end` markers is fleet's shared `AGENTS.md` blocks, the same the apps carry: edit it in `agents-md/sections/`, then `poetry run fleet sync --to v$(cat VERSION)`, never between the markers.

## Layout

- `fleet_tools/`: the `fleet` command. `cli.py` holds the subcommand registry (`COMMANDS`), `config.py` reads a consumer's `[tool.fleet]` table, `self_check.py` checks fleet's own conventions. `checks/` holds `fleet checks`, one module per concern, and `runner.py` runs them; `no_comments.py`, `coverage_floor.py`, `next_version.py`, `test_timings.py`, `agent_review.py` and `review_mine.py` are the other commands.
- `.github/workflows/`: `ci.yml` ("fleet CI", `make check`), `self-test.yml` ("Self-test", calls each reusable workflow by local path), and the reusable workflows beside them.
- `tests/`: the `unittest` suite. `fixtures/consumer-waypoint/` and `fixtures/consumer-runway/` are minimal trees with each app's layout and `[tool.fleet]`; `fixtures/workflows/` holds workflows for `fleet self-check`'s tests; `fakes/` holds fakes of outside services.
- `agents-md/sections/`: the shared `AGENTS.md` blocks, one file per block, with front matter naming the heading each lives under; `fleet sync` writes them into a consumer and `fleet drift` checks them (`fleet_tools/agents_md.py`).
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

<!-- fleet:begin starting-work v0.1.0 -->
- **One issue, one session, one pull request.** Work starts from an issue written as an agent task: its scope, acceptance criteria, dependencies and the model to use (see [CLAUDE.md](CLAUDE.md)). Start a fresh session for each one, on that model, rather than running many pieces of work through one long session.
- **Decisions belong in the issue.** If the issue doesn't settle something a user would notice, ask instead of choosing. Otherwise keep existing behaviour exactly; a change that isn't asked for is a regression.
- **Done means checked.** An issue says how its result is checked (`make check`, a test that fails before the change, `make verify` for anything on screen where the repository has it); the pull request shows that output, not a description of it.
- **Verify in proportion.** Run the tests your change touches while working, and `make check` once before pushing.
- **A correction becomes a check.** When a mistake is pointed out, fix it and add what would have caught it: a lint rule, a type, a test, or a check `fleet checks` runs ("Mistakes that keep coming back" says how). A sentence in this file is the last resort, for what no check can express.
- **Parallel work stays apart.** Agents working at once take separate areas. Two pull requests that change the same files, or both add a migration, land one at a time: the second merges `main` in and re-runs its checks.
<!-- fleet:end starting-work -->
- **In fleet, done means `fleet CI` and `Self-test` too.** The pull request shows `make check`'s output and their results; a fleet check gets its rule in `fleet self-check` or a test.

## Working in a container

<!-- fleet:begin working-in-a-container v0.1.0 -->
- **Start the session in the repository.** The session-start hook (in `.claude/hooks/`) sets up the toolchain, Poetry's environment and the web app's packages, but only for a session whose project directory is this repository. A session started one level up has none of it: the system `python3` lacks the dependencies, and the tests fail on their imports.
- **Run Python through `poetry run` or `make`** (`make test`, or `poetry run python -m unittest tests.test_x`), never the bare `python3`. `make setup` installs the dependencies by hand.
- **Subagents make their own worktree** under `.claude/worktrees/` (ignored by `.gitignore`), from the session's checkout, and push from there. A session whose working directory isn't a git repository can't create one for them.
<!-- fleet:end working-in-a-container -->
- fleet has no session-start hook of its own yet (`FLEET-PLUGIN` adds it): run `make setup` once in a fresh container.

## Mistakes that keep coming back

<!-- fleet:begin mistakes v0.1.0 -->
**A mistake that repeats gets a check.** When review flags the same kind of mistake on a second pull request, the fix adds a check that would have caught it: a lint rule (ESLint, or Semgrep in `.semgrep/` with a failing and a passing example), a type (strict mypy, a `NewType` or `Literal`), or a test. The item below then names that check in its parenthesis (`checked: waypoint-trip-visibility`, `checked: tests/test_offline.py`). An item stays *human-judged* only when no check can express it, and says why. Every month the "Repeated review findings" issue lists the kinds that came back; a row there is closed by a check, not by a new sentence here.

Review findings on agents' pull requests fall into the same few kinds: check your change for each before you push. The independent review ("Agent review", and "Second review" on a risky pull request) reads every agent's pull request for the *human-judged* ones, and a blocking finding fails the merge gate.

- **Failure paths** (human-judged: what a person should see when something fails is a product decision, which no rule can see). For every new error, rejection or early return, know what the user sees, and test it. Don't leave stale state that says things are fine: a scan or sync that half-fails must not clear the last warning, a failed reload must not leave old data on screen looking current, and a job that couldn't start isn't a failed run.
- **Access that outlives the person** (human-judged: each new kind of credential is new code that no existing check knows; its own test holds it once written). A new kind of session, grant, token, key or connection must end when its owner can no longer sign in, like browser sessions do; test that it does.
- **Undo and restore** (human-judged: which values are awkward depends on the data; the round-trip test holds each case once written). Putting something back restores exactly what was there, without normalizing it on the way; test the round trip with awkward values.
- **GitHub Actions** (partly checked: `fleet checks` checks bash by default, SHA pins with a version comment, paginated `gh api` lists, step-only contexts in a workflow's or job's `env:` and every `runs-on:`; zizmor checks `persist-credentials`, template injection and pinning; concurrency groups and agents' allowed commands are human-judged, because the right group and the commands an agent needs depend on what the workflow does). Workflows run bash with `-eo pipefail` (each file's `defaults`); keep it. On a pull request's `closed` event `github.ref` is main's, so build concurrency groups from `github.event.pull_request.number`. Check out with `persist-credentials: false`, pin actions by SHA with the version in a comment, pass event values to scripts as environment variables, paginate `gh api` lists, and allow an agent only the exact commands it needs. Run zizmor on a workflow you change.
- **Layout** (human-judged: whether a page reads well at a width is a judgement, which no rule can make; the screenshots at both widths show it). A layout change is checked at phone and desktop widths, not only the one it was written at.
<!-- fleet:end mistakes -->

## Git and PRs

<!-- fleet:begin git-and-prs v0.1.0 -->
- Work on the branch you were given; never push to `main`.
- Keep commits focused, with a clear message. An agent's commit ends with a `Co-Authored-By: Claude <Model> <version> <noreply@anthropic.com>` trailer naming the model that wrote it (checked on every pull request).
- Before pushing to a pull request, run `make check`.
- Open a pull request for every change, once it's pushed, and summarize what changed and why. Keep the description short and professional: a few bullets on the change and how it was tested, not an essay. Don't say who asked for it or reported it.
- A pull request's title decides the next version when it's merged: `feat: …` releases a minor version, `fix: …` or anything else a patch, and `feat!: …` (or a `BREAKING CHANGE:` line) a major one, for a change that breaks an existing setup.
- A pull request is ready when its "Merge gate" check passes: every check green, including, on an agent's pull request, the independent "Agent review" with no blocking finding and, on a risky one (labelled `safety-critical` or `security`, or touching the paths the review lists), the "Second review". Dependabot's updates merge themselves through it.
- Don't hard-wrap lines in pull request descriptions, comments, issues or the body of a commit message: write each paragraph or list item as one line, and let GitHub wrap it to the screen. Only code and repo files keep their own wrapping. Keep the commit subject to one short line.
<!-- fleet:end git-and-prs -->
- Use `.github/pull_request_template.md`, and add a line to `CHANGELOG.md`'s Unreleased section.
- fleet itself releases when a release pull request bumps `VERSION` ([docs/release.md](docs/release.md)), not on each merge: its title still says what kind of change it is.
