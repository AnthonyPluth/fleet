# Adopting fleet

How an app calls fleet's reusable workflows, configures `[tool.fleet]` in its `pyproject.toml` and syncs fleet's content. Filled in as the pieces land; complete with the v1.0.0 release (`FLEET-RELEASE`).

## The `fleet` command

`fleet_tools` is standard library only (Python 3.11 or newer), so a runner's system `python3` runs it from a checkout of fleet with nothing installed:

```sh
python3 -E -s -m fleet_tools.cli checks --root /path/to/app --commits origin/main..HEAD
```

Each command finds the app's root as the nearest `pyproject.toml` up from the working directory, unless `--root` names it.

| Command | What it does | Exit codes |
| --- | --- | --- |
| `fleet checks [--commits BASE..HEAD]` | Every check the app's `[tool.fleet]` configures. With `--commits` (a pull request's range; BASE is merged with HEAD to find where the branch left), also the commit trailers, removed or skipped tests and committed images. The one entry point for `make fleet-checks` and CI. | 0 all passed (the line lists what ran), 1 problems (one per line on stderr, `::error::` on GitHub Actions), 2 a config error or a failing `git` |
| `fleet no-comments` | No comments or docstrings in tracked Python, CSS and Svelte. Opt-in: does nothing without `[tool.fleet.no-comments]`. | 0, 1, 2 |
| `fleet coverage-floor [coverage.json]` | coverage.py's JSON report against `[tool.fleet.coverage]`. | 0, 1, 2 (no floors, or no report) |
| `fleet next-version [--sha] [--output]` | The next release version from the commits since the last tag (`feat:` a minor; `!:`, `BREAKING CHANGE:` or a trailing `#major` a major; else a patch; a trailing `#minor` a minor). Appends `newest`, then `version` and `new`, or `skip`, to `$GITHUB_OUTPUT`. Replaces `.github/scripts/next-version.sh`. | 0, 1 a failing `git` |
| `fleet test-timings DIR [--top N]` | Sums up the per-test timing files a timing test result wrote. | 0 |

## `[tool.fleet]`

A check runs only where its key is set, so an app opts in key by key. Paths are relative to the app's root. Waypoint's values:

```toml
[tool.fleet]
migrations = "waypoint/storage/migrations/versions"
migration-tests = "tests/test_migrations.py"
tested-from = "0001"
image-homes = ["docs/public/", "waypoint/static/", "waypoint/storage/logos/"]
image-files = ["docs/src/assets/logo.png"]
skills = ".claude/skills"
feature-maps = ["tools/user_feature_map.py"]
extra-checks = "tools.app_checks:CHECKS"

[tool.fleet.screenshots]
dir = "docs/src/assets/screenshots/"
names = "pages"
verify = "frontend/verify/verify.mjs"
flows = "frontend/verify/flows"

[[tool.fleet.generated]]
path = ".claude/skills/verify-waypoint/references/cli.md"
command = "make --no-print-directory -s verify-skill-print"
fix = "make verify-skill"

[tool.fleet.no-comments]
paths = ["waypoint", "tests", "tools", "frontend/src"]

[tool.fleet.coverage]
overall = 85
modules = { "waypoint/domain/" = 90, "waypoint/providers/" = 90, "waypoint/server/api/" = 90 }
```

| Key | Check | Meaning |
| --- | --- | --- |
| `migrations` | migrations | The Alembic versions directory: one head, revisions unique, four digits and the start of their file's name. |
| `migration-tests` | migrations | The file holding a `test_<revision>_...` method per migration (default `tests/test_migrations.py`). |
| `tested-from` | migrations | The first revision that needs its own test (default `0001`); the older ones predate the rule. Never lower it. |
| `test-paths` | tests removed (`--commits`) | Where tests live (default `["tests/", "frontend/"]`): `tests/**/test_*.py` and `*.test.ts`/`.mjs`/`.js`. A removed test needs a `Removes-Test: <name> — <why>` trailer, an added skip a `Skips-Test: <name> — <why>` one. `[]` turns the check off. |
| `image-homes` | images (`--commits`) | Directories where any image belongs. |
| `image-files` | images (`--commits`) | Single image files that belong, matched exactly. |
| `[tool.fleet.screenshots]` | images (`--commits`) | `dir`: where `make verify` writes its screenshots; `names`: `"pages"` (`<page>-<width>.png` and `<page>-<width>-hero.png` for each of `verify.mjs`'s `PAGES`) or `"pages-and-flows"` (`<page>-<width>.png`, `<page>-<TOP>-<width>.png`, and `flow-<name>-<width>.png` and `flow-<name>-<step>-<width>.png` for each flow and its `screenshot` steps); `verify` (default `frontend/verify/verify.mjs`) and `flows` (default `frontend/verify/flows`) are read at the branch's head. Widths are `verify.mjs`'s `DEFAULT_VIEWPORTS`, else `phone` and `desktop`. |
| `skills` | skills | The skills directory: each `*/SKILL.md` has frontmatter with `name` (its folder's) and a `description` (at most 1024 characters, no `<` or `>`), a body, and relative links that lead somewhere. |
| `[[tool.fleet.generated]]` | skills | A generated file: `path`, the `command` that prints what it should hold (run from the app's root), and optionally `fix`, the command that rewrites it. |
| `feature-maps` | skills | Python files that define `OUT` (the map's path), `generate()` (its text and any problems) and `check(committed, generated)` (the problems). |
| `extra-checks` | the app's own | `module:NAME`, a list of callables in the app's tree (`tools/app_checks.py`, or a package's `__init__.py`). Each takes the check context (`.root`, `.config`, `.commits`) and returns a list of problem strings, reported like fleet's own. A missing module or name, a failed import or a check that raises is a problem, never a silent pass. |
| `[tool.fleet.no-comments]` | `fleet no-comments` | `paths`: what to read (default: every tracked file); `skip`: substrings of paths to leave out (default `.semgrep/examples/`, `node_modules/`). |
| `[tool.fleet.coverage]` | `fleet coverage-floor` | `overall`: the floor for the whole report; `modules`: path prefix to floor, for each module with statements (the longest matching prefix wins). |

The workflow conventions check runs wherever `.github/workflows/` or `.github/actions/` exists: bash as each workflow's default shell, every action pinned by its commit SHA with its version in a comment, `gh api ... per_page=` with `--paginate`, no `runner`, `steps`, `job` or `env` expression in a workflow's or job's `env:`, and every `runs-on:` either fleet's `vars.RUNS_ON` line (`fleet_tools/checks/workflows.py`) or `runs-on: ubuntu-latest   # hosted: <why it needs GitHub's runners>`.
