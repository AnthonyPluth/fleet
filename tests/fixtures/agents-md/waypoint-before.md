# Agent guide

Instructions for AI coding agents working in Waypoint, a self-hosted travel app for a household (Python backend in `waypoint/`, Svelte web app in `frontend/`). Human contributors can use it too.

## Layout

- `waypoint/`: the backend, in packages whose imports `make lint` checks (`server/`, `domain/`, `providers/`, `storage/`).
- `frontend/`: the web app (Svelte, Vite, Tailwind, Vitest, ESLint). The app is dark only.
- `tests/`: backend tests, run with `unittest`.

## Commands

Python goes through Poetry (Python 3.14).

- `make check`: each tool once. Run it before pushing. The security scans run only in CI.
- `make verify`: runs the real app on demo data in a browser at phone and desktop widths and writes the screenshots to `docs/src/assets/screenshots/`.

## Starting work

<!-- fleet:begin starting-work v0.0.1 -->
- **One issue, one session.** Work starts from an issue made with the "Agent task" template.
- **Done means checked.** The PR shows the output, not a description of it.
<!-- fleet:end starting-work -->
- **Postgres in proportion.** CI runs the Postgres suites on every pull request: run `make test-pg` yourself only for changes to the database, migrations or tests (see "Tests that share the database" below), on a database of your own that you drop afterwards.

## Working in a container

- **Some checks only run in CI.** Locally: ruff, mypy, import-linter, Waypoint's Semgrep rules, the Python tests on SQLite, the web app's checks and the docs build. Only in CI: the Postgres suites, the Docker image build and run, the security scans and the agent review. A push reaches them; a local run doesn't.

## Conventions

- No comments or docstrings in code (Python, TypeScript, JavaScript, Svelte, CSS): name things so the code says what it does.
- Real typography (’ – −) in user-facing strings is intentional; don't "fix" it to ASCII.

## Mistakes that keep coming back

<!-- fleet:begin mistakes v0.0.1 -->
Review findings on agents' pull requests fall into the same few kinds. Codex's review reads every pull request for them.

- **Failure paths** (human-judged). For every new error, know what the user sees.
<!-- fleet:end mistakes -->

- **Tests that share the database** (human-judged). On Postgres every test module shares one database, and CI runs modules in parallel. A test that writes settings, or runs a mail scan or a request through the server, takes its own database with `tests/shared.py`'s `own_database(self)`.
- **Private data in logs** (partly checked: lint sends every log line through `monitoring.log`; the rest is human-judged). Logs and error pages never carry email content, traveller names, confirmation codes or credentials.
- **Personal data in the repo** (human-judged). The repository is public. Fixtures are made-up emails with invented names, codes and numbers.
- **Visibility** (checked: Semgrep's `waypoint-trip-visibility`). Every query for trips or segments goes through the one visibility helper; a route answers 404 for a trip you can't see.
- **Time zones** (partly checked: lint rejects a bare `date.today()` in background jobs; the rest is human-judged). A booking's times are its airports' or hotel's.

## Git and PRs

<!-- fleet:begin git-and-prs v0.0.1 -->
- Open a PR for every change you're asked to make, once it's pushed.
<!-- fleet:end git-and-prs -->
- A UI PR commits the images `make verify` changed in `docs/src/assets/screenshots/`, so a reviewer sees each page as before and after in the diff. Only commit images of `make verify`'s made-up demo data. Skip it for a PR with no UI change.

## Model routing

See [CLAUDE.md](CLAUDE.md) for which model to use for which kind of task.
