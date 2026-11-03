# Agent guide

Instructions for AI coding agents working in Runway, a self-hosted personal finance app (Python backend in `runway/`, Svelte web app in `frontend/`, browser extension in `extension/`). Human contributors can use it too.

## Layout

- `runway/`: the backend, in packages whose imports `make lint` checks (`server/`, `domain/`, `providers/`, `storage/`).
- `frontend/`: the web app (Svelte, Vite, Vitest, ESLint).
- `extension/`: the browser extension.

## Commands

- `make check`: each tool once. Run it before pushing.
- `make pre-push`: about a minute: the feature-map, API-contract and fleet checks, and the tests that read shared fixtures.

## Starting work

<!-- fleet:begin starting-work v1.1.0 -->
- **One issue, one session, one pull request.** Work starts from an issue written as an agent task: its scope, acceptance criteria, dependencies and the model to use (see [CLAUDE.md](CLAUDE.md)). Start a fresh session for each one, on that model, rather than running many pieces of work through one long session.
- **Decisions belong in the issue.** If the issue doesn't settle something a user would notice, ask instead of choosing. Otherwise keep existing behaviour exactly; a change that isn't asked for is a regression.
- **Done means checked.** An issue says how its result is checked (`make check`, a test that fails before the change, `make verify` for anything on screen where the repository has it); the pull request shows that output, not a description of it.
- **Verify in proportion.** Run the tests your change touches while working, and `make check` once before pushing.
- **A correction becomes a check.** When a mistake is pointed out, fix it and add what would have caught it: a lint rule, a type, a test, or a check `fleet checks` runs ("Mistakes that keep coming back" says how). A sentence in this file is the last resort, for what no check can express.
- **Parallel work stays apart.** Agents working at once take separate areas. Two pull requests that change the same files, or both add a migration, land one at a time: the second merges `main` in and re-runs its checks.
<!-- fleet:end starting-work -->

## Working in a container

<!-- fleet:begin working-in-a-container v1.1.0 -->
- **Start the session in the repository.** The session-start hook (in `.claude/hooks/`) sets up the toolchain, Poetry's environment and the web app's packages, but only for a session whose project directory is this repository. A session started one level up has none of it: the system `python3` lacks the dependencies, and the tests fail on their imports.
- **Run Python through `poetry run` or `make`** (`make test`, or `poetry run python -m unittest tests.test_x`), never the bare `python3`. `make setup` installs the dependencies by hand.
- **Subagents make their own worktree** under `.claude/worktrees/` (ignored by `.gitignore`), from the session's checkout, and push from there. A session whose working directory isn't a git repository can't create one for them.
- **Skills have two homes.** fleet's skills are under `.claude/skills/fleet-*` and are changed in fleet; the app's own are under `.claude/skills/`; skills go nowhere else (`fleet checks` checks it: `skill-homes`).
<!-- fleet:end working-in-a-container -->
- **Some checks only run in CI.** Only in CI: the Postgres suites, the Docker image build and run, the Verify flows check, the security scans and the independent agent review. A push reaches them; a local run doesn't.
- **A forecast or money-math test starts from `tests/forecast_support.py`'s `card_setup`** and the helpers in `tests/shared.py`. Build the case in a few lines with made-up figures, and prove the test fails before the fix.

## Conventions

- Match the surrounding code's style, comment density and naming.
- Add or update tests with the change. Don't skip, disable or delete a test to get CI passing; when one really goes, a `Removes-Test:` or `Skips-Test:` trailer says why (checked).

## Mistakes that keep coming back

<!-- fleet:begin mistakes v1.1.0 -->
**A mistake that repeats gets a check.** When review flags the same kind of mistake on a second pull request, the fix adds a check that would have caught it: a lint rule (ESLint, or Semgrep in `.semgrep/` with a failing and a passing example), a type (strict mypy, a `NewType` or `Literal`), or a test. The item below then names that check in its parenthesis (`checked: waypoint-trip-visibility`, `checked: tests/test_offline.py`). An item stays *human-judged* only when no check can express it, and says why. Every month the "Repeated review findings" issue lists the kinds that came back; a row there is closed by a check, not by a new sentence here.

Review findings on agents' pull requests fall into the same few kinds: check your change for each before you push. The reviews read agents' pull requests for the *human-judged* ones: the "Second review" on a risky pull request, the independent "Agent review" on every agent's pull request where the app turns it on, and any reviewer the repository adds that posts P1 and P2 review threads. A blocking finding, or an open P1 or P2 thread, fails the merge gate.

- **Failure paths** (human-judged: what a person should see when something fails is a product decision, which no rule can see). For every new error, rejection or early return, know what the user sees, and test it. Don't leave stale state that says things are fine: a scan or sync that half-fails must not clear the last warning, a failed reload must not leave old data on screen looking current, and a job that couldn't start isn't a failed run.
- **Access that outlives the person** (human-judged: each new kind of credential is new code that no existing check knows; its own test holds it once written). A new kind of session, grant, token, key or connection must end when its owner can no longer sign in, like browser sessions do; test that it does.
- **Undo and restore** (human-judged: which values are awkward depends on the data; the round-trip test holds each case once written). Putting something back restores exactly what was there, without normalizing it on the way; test the round trip with awkward values.
- **GitHub Actions** (partly checked: `fleet checks` checks bash by default, SHA pins with a version comment, paginated `gh api` lists, step-only contexts in a workflow's or job's `env:` and every `runs-on:`; zizmor checks `persist-credentials`, template injection and pinning; concurrency groups and agents' allowed commands are human-judged, because the right group and the commands an agent needs depend on what the workflow does). Workflows run bash with `-eo pipefail` (each file's `defaults`); keep it. On a pull request's `closed` event `github.ref` is main's, so build concurrency groups from `github.event.pull_request.number`. Check out with `persist-credentials: false`, pin actions by SHA with the version in a comment, pass event values to scripts as environment variables, paginate `gh api` lists, and allow an agent only the exact commands it needs. Run zizmor on a workflow you change.
- **Layout** (human-judged: whether a page reads well at a width is a judgement, which no rule can make; the screenshots at both widths show it). A layout change is checked at phone and desktop widths, not only the one it was written at.
- **Verification that doesn't cover the change** (partly checked: `fleet checks verification`; coverage is judged by the review). Where the app sets `[tool.fleet.verification]`, the pull request's Verification table names every behaviour the change touches as a dimension: `verified` with evidence that exists in the diff or the tree and exercises the change, or `not verified` with why and what would close the gap. A `verified` claim whose evidence doesn't exist, or a changed behaviour missing from the table, is blocking; a vague gap reason is advisory.
<!-- fleet:end mistakes -->

- **Tests that share the database** (human-judged). On Postgres every test module shares one schema, and CI runs modules in parallel. A test that writes settings, or runs a sync, takes its own database with `tests/shared.py`'s `own_database(self)`.
- **Private data in logs** (human-judged). Logs and error pages never carry amounts, merchants, account ids, query strings or credentials. Go through `monitoring.scrub`, and test what's logged.
- **Personal financial data in the repo** (human-judged). The repository is public. Use made-up figures and names, as `tests/` does.
- **Time zones** (partly checked: lint rejects a bare `date.today()` in sync and provider modules; the rest is human-judged). "Today" and the daily sync's hour are the machine's local time (`TZ`).
- **Tablet layout** (human-judged: `make verify` has no tablet width). A change that touches the 768–1023 px rail layout is also looked at by resizing a browser there; nothing of that is committed.

## Before you push

- The tests for what you changed, then `make pre-push`.

## Git and PRs

- **Screenshots live in one committed directory, `screenshots/`.** A PR that changes the UI runs `make screenshots` before its last push and commits what changed, never images copied in by hand. Only `make verify`'s made-up demo data is ever committed.

<!-- fleet:begin git-and-prs v1.1.0 -->
- Work on the branch you were given; never push to `main`.
- Keep commits focused, with a clear message. An agent's commit ends with a `Co-Authored-By: Claude <Model> <version> <noreply@anthropic.com>` trailer naming the model that wrote it (checked on every pull request).
- Before pushing to a pull request, run `make check`.
- Open a pull request for every change, once it's pushed, as a draft, and summarize what changed and why. Keep the description short and professional: a few bullets on the change and how it was tested, not an essay. Don't say who asked for it or reported it.
- Keep a pull request a draft until every check is green, every review has finished and every comment is addressed. Right after opening the draft, comment `@codex review` on it if the repository uses that reviewer (it doesn't review a draft by itself, but that comment asks it to), and again after a substantive push. Mark it ready for review only when every check is green, every review that runs on it has finished (the "Second review", the independent "Agent review" where the app turns it on, and any such reviewer), and every review comment has been fixed or answered and resolved. Then watch it until it merges.
- A pull request's title decides the next version when it's merged: `feat: …` releases a minor version, `fix: …` or anything else a patch, and `feat!: …` (or a `BREAKING CHANGE:` line) a major one, for a change that breaks an existing setup.
- Where the app sets `[tool.fleet.verification]`, a pull request's description carries a Verification block (the table between the `fleet-verification` markers: each dimension of the change `verified` with its evidence, or `not verified` with why) before it is marked ready, written with the `fleet-verify-change` skill; `fleet checks` reads it.
- A pull request is ready when its "Merge gate" check passes: every check green, including the "Second review" with no blocking finding (it reviews a risky pull request, labelled `safety-critical` or `security` or touching the paths the review lists, and passes any other as not risky), no open review thread with a P1 or P2 badge, and, where the app turns it on, the independent "Agent review" with no blocking finding. Dependabot's updates merge themselves through it.
- Don't hard-wrap lines in pull request descriptions, comments, issues or the body of a commit message: write each paragraph or list item as one line, and let GitHub wrap it to the screen. Only code and repo files keep their own wrapping. Keep the commit subject to one short line.
<!-- fleet:end git-and-prs -->
