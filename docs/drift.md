# Drift between the apps

Where Waypoint and Runway did the same thing differently, and which way fleet took. Each extraction issue adds its rows. Where the two differ, fleet's own code follows the stricter convention (Waypoint's): no comments or docstrings in Python, actions pinned by SHA with a version comment, `bash -eo pipefail` as each workflow's default shell, and `persist-credentials: false` on every checkout.

## Planned (from the backlog)

The decisions the backlog made before the extractions; each extraction issue replaces its row with the detail below when it lands.

| Piece | Waypoint | Runway | fleet takes | Why |
| --- | --- | --- | --- | --- |
| Agent review | Disabled; Codex comments; no required status | On, required | On, required, plus the second review | An independent review gate is the point; Codex comments don't gate merges. Waypoint decides in WP-REVIEW-DECISION (`first-review: false` is available) |
| `ready_for_review` | Not a trigger | Triggers a review | Triggers one only if the head has no review yet | Runway's coverage without paying twice for a reviewed draft |
| `agent_review.py` | Never omits fixtures/demo/sample data; more image types | Omits the forecast golden fixture | Waypoint's rules, plus the caller's `generated` list, where an explicit entry wins | The privacy check is the review's job; Runway's golden fixture stays out by listing it |
| Review prompt | Travel wording | Finance wording | One generic prompt plus each app's `context.md` | One prompt to maintain |
| Review model | Sonnet | Sonnet ("never Opus", owner decision) | Sonnet first; Fable second on risky PRs | The owner's new rule; recorded in the workflow header |
| `merge-gate.sh` | Requires no status | Requires Agent review; ignores Preview and Screenshot diff | One script; required and ignored lists are inputs; triggers stay in the caller | Called workflows can't declare triggers |
| `security.yml` | `vars.RUNS_ON` option | Hosted only; adds `extension/` | Inputs for paths and the runner; `sarif-upload`/`scorecard` switches | Darkroom can't upload SARIF |
| Session-start hook | Venv-version guard, `npm install --no-save` | `node_modules` completeness check | Union, with `--no-save` | A session must not rewrite a lockfile |
| AGENTS "Git and PRs" | Open a PR for every change | Open a PR only when asked | Every change | Every push to `main` releases, so all work lands through PRs |
| `docker.yml`, `tools/control`, verify-* skills | Own | Own | Stay per app (v2 candidates) | Hundreds of lines of app-specific drift |

## The fleet command (`FLEET-TOOLS`)

`tools/fleet_checks.py` (Waypoint 443 lines, Runway 425) became `fleet checks`, split by concern under `fleet_tools/checks/`; each check runs only where the consumer's `[tool.fleet]` configures it ([adopting.md](adopting.md)).

| Piece | Waypoint | Runway | fleet takes | Why |
| --- | --- | --- | --- | --- |
| `fleet_checks.py` as a whole | Test-removal trailers, `# hosted:` rule, parser fixtures | `EARLY_ENV` rule, a11y baseline, flow inheritance | The union of shared checks; app-only checks via `extra-checks` | Each gains the other's general checks |
| Paths | Hard-coded for `waypoint/` | Hard-coded for `runway/` | `[tool.fleet]` keys; a check with no key doesn't run | One tool, two layouts |
| Migrations (`checks/migrations.py`) | One head, four-digit ids, a test per migration from `0001` | The same, from `0036` | Identical logic; `migrations`, `migration-tests`, `tested-from` | The rule is the same; only where it starts differs |
| Commit trailers (`checks/commits.py`) | Exactly one `Co-Authored-By: Claude <Model> <version>`, `Opus\|Sonnet\|Haiku\|Fable`, keyed on `Claude-Session` | Identical | Identical | No drift |
| Removed or skipped tests (`checks/tests_removed.py`) | `Removes-Test:` / `Skips-Test:` trailers | Absent | Waypoint's, on for every consumer given `--commits`; `test-paths` (default `tests/`, `frontend/`) | A silently deleted test is the same mistake in both apps; Runway gains the check |
| Workflow conventions (`checks/workflows.py`) | bash default, SHA pins with a version comment, paginated `gh api`, `vars.RUNS_ON` or `# hosted: <why>` | bash default, pins, paginated `gh api`, `EARLY_ENV` (no `runner`/`steps`/`job`/`env` in a workflow's or job's `env:`) | Both rule sets | Waypoint gains `EARLY_ENV` (a file GitHub rejects never runs); Runway gains the `# hosted:` rule, so each of its jobs says why it stays on GitHub's runners |
| Images (`checks/images.py`) | Screenshots in `docs/src/assets/screenshots/`, `<page>-<width>[-hero].png` from `verify.mjs`'s `PAGES`; homes and the docs logo | Screenshots in `screenshots/`, pages (`<page>[-top]-<width>.png`) and flows (`flow-<name>[-<step>]-<width>.png`); icons, static assets and the docs logo | `[tool.fleet.screenshots] names = "pages"` or `"pages-and-flows"`, plus `image-homes` and `image-files`; widths from `DEFAULT_VIEWPORTS` (else `phone`, `desktop`); names read from the branch's head | Two naming schemes that each app's `make verify` writes; Waypoint's widths were hard-coded to the same defaults |
| Skills (`checks/skills.py`) | Frontmatter, name, description, body, links | Identical | Identical, for the directory `skills` names | No drift |
| Generated `references/cli.md` | Hard-coded `node frontend/verify/control.mjs help --markdown` | The same, for `verify-runway` | `[[tool.fleet.generated]]`: `path`, `command` (run from the consumer's root), optional `fix` | Any generated reference, not only one; the generator stays in the app |
| User feature map | `tools/user_feature_map.py`'s `generate()` and `check()` | The same module shape | `feature-maps`: a list of such modules | Same shape in both; the content is the app's |
| Mail parser fixtures | In `fleet_checks.py` | — | Stays in Waypoint, through `extra-checks` | App-only |
| a11y baseline (shrink-only), flow inheritance | — | In `fleet_checks.py` | Stay in Runway, through `extra-checks` (its adoption issue) | App-only |
| Dark-only stylesheets, manage links tested | In `fleet_checks.py` | — | Stay in Waypoint, through `extra-checks` (its adoption issue) | App-only: one app's theme and one app's provider table |
| Output and exit codes | Problems on stderr (`::error::` on Actions), exit 1; a fixed "passed" sentence | The same | The same; the "passed" sentence lists the checks that ran; a config error or a failing `git` exits 2 | A consumer sees what was checked; a broken setup isn't a failed check |
| `no_comments.py`, `coverage_floor.py` | Present | Absent | Opt-in: `fleet no-comments` runs only where `[tool.fleet.no-comments]` exists; `fleet coverage-floor` reads `[tool.fleet.coverage]` | Runway's convention allows comments ("match the surrounding comment density") |
| `no_comments.py` paths | Every tracked file | — | `paths` (default: every tracked file), `skip` (default `.semgrep/examples/`, `node_modules/`) | Waypoint's behaviour, made configurable |
| `coverage_floor.py` floors | 85% overall, 90% for `domain/`, `providers/`, `server/api/` in code | — | `overall` and `modules` (prefix → floor; the longest prefix wins) in config | The numbers are the app's |
| `.github/scripts/next-version.sh` | Identical in both | Identical | `fleet next-version`, the same outputs (`version`, `new`, `newest`, `skip`) to `$GITHUB_OUTPUT` | Python is testable and runs where bash differs; `--remote` and `--branch` default to `origin` and `main` |
| `tools/test_timings.py` | "summed over all workers" wording | "summed over all processes" wording; `dict(phases)`, `int(count)` | Runway's | 25 lines of wording and typing; Runway's is the tidier one |

## Merge gate

| Piece | Waypoint | Runway | fleet takes | Why |
| --- | --- | --- | --- | --- |
| `security.yml` | `changes` filters on `^(waypoint\|frontend/src\|\.semgrep)/`; every job on `vars.RUNS_ON` with the fallback to `ubuntu-latest` for Dependabot and forks; Scorecard hosted | `changes` filters on `^(runway\|frontend/src\|extension\|\.semgrep)/`; hosted runners only | One reusable workflow, the union of both: the paths are inputs (`code-paths`, `semgrep-paths`, `web-dirs`, `image-paths`, `dockerfile`), the runner is `runs-on` (JSON; Waypoint passes its `vars.RUNS_ON` expression), the app's own Semgrep rules and example check come from its `.semgrep/`; `sarif-upload: false` makes Semgrep, Trivy and zizmor fail on findings and `scorecard: false` skips Scorecard, for a private repository without code scanning. Trivy's file scan always runs; `.github/` changes run everything. Triggers (push, pull request, the weekly schedule, dispatch) stay in the caller | Darkroom can't upload SARIF or run CodeQL; a called workflow can't declare triggers |
| Deep security scans | None | None | An owner runbook (`docs/deep-scan.md`) and the `deep-scan-due` reusable workflow, which opens one "Deep security scan due" issue (labels `manual`, `security`) unless one is open | The claude-security plugin asks a confirmation only the person's own request can answer, refuses in a non-interactive session and needs the session's Workflow tool, so a workflow can only remind |
| `merge-gate.sh` | Requires no status (its review is off); ignores `Cancel runs on close` and `Merge gate` | Requires `Agent review`; also ignores `Preview` and `Screenshot diff` | One script, configured by the reusable workflow's inputs (`required-workflows`, `ignored-workflows`, `required-statuses`, `ignored-apps`, polling, Dependabot merging, the workflows started after a merge); `Merge gate` and `Cancel runs on close` always ignored; defaults require `Security scans`, `CodeQL`, `Agent review` and `Second review`. New rule: a Dependabot pull request that changes an `AnthonyPluth/fleet/` reference passes but isn't merged by the gate | The scripts were identical apart from configuration; a fleet bump changes the review and the gate themselves, so the owner merges it |
| `merge-gate.yml` | `workflow_run` on `Docker image, Security scans, Docs, Control smoke` | `workflow_run` on `Docker image, Security scans, Docs, Verify flows` | The triggers, the job `if:` and the concurrency group stay in each app's caller; the job calls fleet's workflow | A called workflow can't declare triggers |

## AGENTS.md blocks (`FLEET-AGENTS-MD`)

| Piece | Waypoint | Runway | fleet takes | Why |
| --- | --- | --- | --- | --- |
| "Starting work" | The "Agent task" template; Postgres in "Verify in proportion" | No template named; the same Postgres text | One generic block (an issue written as an agent task; `make check` before pushing); the Postgres sentence stays app text after the block | The rest was identical; the database is each app's |
| "Working in a container" | Hook, `poetry run`/`make`, worktrees, CI-only list | The same, plus the forecast-test bullet | The first three as the block; "checks that only run in CI" and the forecast bullet stay app text | Each app's CI-only list differs |
| "Mistakes" intro | Codex reads the human-judged items; the independent review is disabled | The independent review reads them; a blocking finding fails the gate | The rule "A mistake that repeats gets a check" verbatim, then the Agent review (and Second review) reading the human-judged items | Both said a checkable item moves into a check, but an item could stay human-judged forever; now it says why, and the monthly "Repeated review findings" issue closes rows only with checks |
| Shared mistakes | Failure paths, Access that outlives the person, Undo and restore, GitHub Actions, Layout | Near-identical; Layout adds the tablet rail | Those five, each human-judged item saying why; GitHub Actions is `partly checked` (it named checks but kept human-judged parts); the tablet note stays Runway's | One wording; `oidc.access_lapsed` is named by neither block, since it is each app's |
| App mistakes | Tests that share the database, Private data in logs, Personal data in the repo, Visibility, Time zones | The same kinds, finance wording | Stay app items after the block | Their checks and wording are the app's |
| "Git and PRs" | Open a PR for every change | Open a PR only when asked | Every change | Every push to `main` releases, so all work lands through PRs |
| "Git and PRs" readiness | Merge gate; Codex comments instead of the review | Merge gate including Agent review | Merge gate including "Agent review" and, on a risky pull request, "Second review" | The planned review gate (above); Waypoint decides in WP-REVIEW-DECISION |
| Screenshots bullet | `docs/src/assets/screenshots/` | `screenshots/`, `make screenshots` | Stays app text in "Git and PRs" | Two layouts |

## Agent review (`FLEET-REVIEW`)

Waypoint's and Runway's `agent-review.yml`, `agent_review.py`, `agent-review-run.sh`, prompts and tests became one reusable workflow, `fleet_tools/agent_review.py` and one prompt ([agent-review.md](agent-review.md)). The design both shared (`pull_request_target` runs main's copy; the `review` job reads the code and holds the secret, the `report` job posts with the write token and sees neither; a re-review reads the diff since the last reviewed commit; a push that only merges main carries a passing verdict forward) is unchanged.

| Piece | Waypoint | Runway | fleet takes | Why |
| --- | --- | --- | --- | --- |
| The trigger | Disabled ("Codex reviews pull requests now"; `workflow_dispatch` only) | `pull_request_target` on `[opened, synchronize, reopened, ready_for_review]` | The caller declares `pull_request_target` on `main` with `[opened, synchronize, reopened, ready_for_review, labeled, unlabeled]`; `first-review: false` for a consumer that keeps the first review off | A called workflow can't declare triggers; the label events are for the second review |
| `ready_for_review` | Not a trigger | Reviews again | Reviews only when the head has no "Agent review" status yet (`wanted`) | Runway's coverage of drafts without paying twice for a reviewed one |
| Label events | — | — | Run no first review; the concurrency group is the review job's, so they can't cancel a running review | Only `FLEET-SECOND-REVIEW` reacts to labels |
| `GENERATED` | The API contract and the feature map, hard-coded | The same plus `tests/fixtures/forecast_golden.json` | The `generated` input, one path per line | Each app's generated files are its own |
| Fixtures, demo and sample data | Never left out of the diff (`SAMPLE_DATA`) | Left out when in `GENERATED` | Never left out unless `generated` names the file: an explicit entry wins | The privacy check is the review's job; Runway's golden fixture stays out by listing it |
| `IMAGES` | `.png .jpg .jpeg .gif .webp .avif .ico .heic .bmp .tif .tiff` | `.png .jpg .jpeg .gif .webp .avif .ico` | Waypoint's | The wider list |
| Docstrings | None; a `USAGE` string | Module and function docstrings explaining the decisions | Waypoint's style; Runway's explanations moved to `docs/agent-review.md` | fleet's no-docstrings convention |
| The prompt | Travel wording: travellers' data, mail and flight fixtures, `waypoint/domain/demo.py` | Finance wording: balances, merchants, the golden fixture | One generic `prompt.md`; the app, what private data looks like there, where it lands and what is generated come from the consumer's `.github/agent-review/context.md` (required; missing is an `error` status naming the file) | One prompt to maintain |
| `incremental.md` | Re-reads fixture, demo and sample lines for private data | Doesn't say so | Waypoint's | The fuller instruction |
| The model | `sonnet`, no variable | `sonnet`, no variable; the header records the owner's "never Opus" decision | `sonnet` fixed in the workflow, no input; the header records the decisions: Sonnet first review on every agent PR, Fable second review on risky PRs | The owner's rule; a change is a fleet pull request |
| Budget and turns | `vars.AGENT_REVIEW_BUDGET_USD`, `vars.AGENT_REVIEW_MAX_TURNS` | The same | Inputs `budget-usd` (3) and `max-turns` (60); callers pass their variables | A called workflow doesn't read the caller's `vars` |
| Findings | `severity`, `title`, `detail`, `file`, `line` | The same | Plus required `kind` (the AGENTS.md "Mistakes that keep coming back" item, or `other`) and optional `check`; the comment's third hidden line carries the findings as data (at most 8 KB) | `FLEET-MINING` reads them |
| The secret check | `report` had both secrets in its `env` to refuse an answer that quotes one | The same | The secrets appear only in the Review step's `env`; `agent-review-run.sh` replaces an answer that quotes the secret with an error result (`error_secret_in_answer`) before the Verdict step; `report()` still refuses one when its environment has a secret | One step with a secret instead of two |
| Node for Claude Code | `trusted/.nvmrc` | The same | `engines.node` in fleet's `.github/agent-review/package.json`, read by `setup-node` | fleet has no `.nvmrc`; the one manifest holds both versions |
| Claude Code's version | `.github/agent-review/package-lock.json`, Dependabot npm entry | The same | fleet's lockfile and Dependabot entry; consumers drop theirs | One version for every review |
| Trusted inputs | `trusted/` sparse checkout of main's `.github/agent-review` and `.github/scripts` | The same | fleet at `FLEET_REF` (`.fleet/`: `.github/agent-review`, `.github/scripts`, `fleet_tools`, `tests/fakes`) and the consumer's base commit (`trusted/`: `AGENTS.md`, `.github/agent-review/context.md`) | The tools come from fleet, the standard from the app |
| Self-test | None | None | The reusable workflow on fleet's pull requests with `dry-run: true` and `fake-review` (the stub `tests/fakes/claude`: pass, blocking, malformed → error); fleet dogfoods the real review through `agent-review-caller.yml` | The plumbing is tested without a secret |
| `tests/test_agent_review.py` | 558 lines, the Plan cases above | 452 lines, a subset | The union, on `fleet_tools.agent_review`, plus the new decisions | No case lost |

## Second review (`FLEET-SECOND-REVIEW`)

Neither app had one. The second review is new, in the same reusable workflow as the first ([agent-review.md](agent-review.md)).

| Piece | Waypoint | Runway | fleet takes | Why |
| --- | --- | --- | --- | --- |
| A second review | — | — ("every review uses Sonnet and never Opus", owner decision) | The `second-review` job on `claude-fable-5-1`, fixed, on risky pull requests only: a label in `second-review-labels` (`safety-critical,security`) or a path in `second-review-paths` (`.github/**`, `.claude/**` always); its own "Second review" status and `<!-- second-review -->` comment; "Not a risky pull request" passes so the gate can require it everywhere; `labeled`/`unlabeled` run only it, and only the owner's `unlabeled` re-evaluates; budget 10, turns 120; the app's `invariants.md` required with `second-review-paths` | The owner's new rule supersedes Runway's; the first review stays Sonnet |

## Review mining (`FLEET-MINING`)

Neither app counted its review findings; both carried the rule that a repeated mistake becomes a check, with nobody to notice the repeats. The report is new ([review-mining.md](review-mining.md)).

| Piece | Waypoint | Runway | fleet takes | Why |
| --- | --- | --- | --- | --- |
| Noticing repeats | By hand, when someone remembered | By hand | `review-mining.yml` and `fleet review-mine`: the reviews' hidden findings records (now with the reviewer's `check`), counted per AGENTS.md kind over the pull requests of the window, into one "Repeated review findings" issue rewritten each run, with a stable `row:` id and a "becomes an Agent task" checkbox per row; monthly by default, weekly as an option; no model, no secrets | The rule needs a counter, and counting needs no judgement |

## Opt-in workflows (`FLEET-OPTIONAL`)

What each app runs beyond the core, as reusable workflows an app calls or doesn't ([adopting.md](adopting.md)). Each keeps its triggers, path filters and concurrency group in the caller.

| Piece | Waypoint | Runway | fleet takes | Why |
| --- | --- | --- | --- | --- |
| `control-smoke.yml` / `verify.yml` | "Control smoke": `make control-smoke` (tools/control end to end) on changes to `frontend/verify/**`, `waypoint/verify.py`, `tools/control`, the lockfiles; `vars.RUNS_ON`; 20 minutes | "Verify flows": `run.py verify` then `control-smoke.mjs` on changes to `frontend/**`, the demo data, `runway/verify.py`; hosted; 15 minutes; `poetry install --only main` | One `verify.yml`: `make-target` (the app's own target does the run), `needs-faketime` (default `true`), `timeout-minutes` (default 15), `runs-on` (JSON; Waypoint passes its `vars.RUNS_ON` expression), `poetry-install-args`, and the Python, Node and browser setup both did in their local actions, each switchable; `working-directory` for the self-test's fixture tree. The caller's job name keeps the status ("Control smoke", "Verify flows") and its `on: paths:` stay as they are | Both set up the same things and then ran one command; the differences are inputs. A called workflow can't declare triggers, so the path filters stay where they were |
| `screenshot-diff.yml`, `tools/screenshot_diff.py` | — | `screenshots/`, marker `<!-- screenshot-diff -->`; the tool runs from the pull request's own checkout with Pillow from the dev group | `fleet screenshot-diff` (`fleet_tools/screenshot_diff.py`, Pillow as the optional extra `fleet[images]`), `--dir` and `--marker` arguments; the workflow's `dir` (`screenshots/` for Runway, `docs/src/assets/screenshots/` for Waypoint) and `comment-marker` inputs. Still `pull_request`, but the tool is installed from fleet's checkout at FLEET_REF and run with `python -I`, so a pull request can't change the tool that reports on it. `dry-run` prints the comment; `fixture` is the self-test's | Waypoint gains the diff for its docs screenshots; the tool is now part of what the pull request can't touch |
| `cancel-on-close.yml` | Joins `docker-image-refs/pull/N/merge` and `security-refs/pull/N/merge`; `vars.RUNS_ON`; 5 minutes | Those two and `verify-N`; hosted; no timeout | One workflow with a matrix over the `groups` input (JSON patterns with `{0}` for the pull request's number, or prefixes); `runs-on`; 5 minutes. No permission: joining a group is what cancels | The jobs were identical apart from the list of groups |
| `tidy-releases.yml` | Image `waypoint`; `vars.RUNS_ON`; 10 minutes | Image `runway`; hosted; no timeout | Inputs `image`, `versions`, `retag`, `confirm` (the caller's `workflow_dispatch` passes them through) and `runs-on`; 10 minutes; `github.token`. The `docker-image-refs/heads/main` concurrency group stays in the caller, with its `docker.yml` | Only the image name and the runner differed |
| `preview.yml` | — | Present: Runway's own image with its sample data behind a Cloudflare quick tunnel | Stays in Runway | Runway-specific deploy; nothing for the others to reuse yet |
