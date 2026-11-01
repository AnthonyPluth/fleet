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

<!-- fleet:begin starting-work v0.0.1 -->
<!-- fleet:end starting-work -->

## Working in a container

<!-- fleet:begin working-in-a-container v0.0.1 -->
<!-- fleet:end working-in-a-container -->
- **Some checks only run in CI.** Only in CI: the Postgres suites, the Docker image build and run, the Verify flows check, the security scans and the independent agent review. A push reaches them; a local run doesn't.
- **A forecast or money-math test starts from `tests/forecast_support.py`'s `card_setup`** and the helpers in `tests/shared.py`. Build the case in a few lines with made-up figures, and prove the test fails before the fix.

## Conventions

- Match the surrounding code's style, comment density and naming.
- Add or update tests with the change. Don't skip, disable or delete a test to get CI passing; when one really goes, a `Removes-Test:` or `Skips-Test:` trailer says why (checked).

## Mistakes that keep coming back

<!-- fleet:begin mistakes v0.0.1 -->
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
