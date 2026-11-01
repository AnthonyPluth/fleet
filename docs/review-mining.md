# Review mining

How `.github/workflows/review-mining.yml` (`workflow_call`) and `fleet review-mine` turn the reviews' findings into the "Repeated review findings" issue, what the report looks like and what later issues may build on it. The rule it serves is the first line of the shared "Mistakes that keep coming back" block: a mistake that repeats gets a check. The reviews record each finding's kind; this job notices the repeats, deterministically and with no model.

## What it reads

- The pull requests of the repository updated in the window (`--days`, default 90), listed with `gh api` page by page, newest first, until a page ends before the window.
- The issue comments updated in the window (`repos/<repo>/issues/comments?since=…`, paginated), kept only when they are on one of those pull requests, their author is `github-actions[bot]` and their first line is `<!-- agent-review -->` or `<!-- second-review -->`.
- Of such a comment, **only the third line**: the findings record `<!-- agent-review-findings <base64 JSON> -->` (or `second-review-findings`) that `agent_review.render` writes ([agent-review.md](agent-review.md)). The line must be at most 8 KB, decode to a JSON list of objects, and each finding must have a `severity` of `blocking` or `advisory`, a `kind` of at most 120 characters, a `title` of at most 200, and, when present, a `file` of at most 300 and a `check` of at most 300; a finding that doesn't is dropped, a record that isn't a list is ignored, and at most 50 findings of a record count. A findings line anywhere else in the comment, a comment by anyone else, and the rest of the comment (which quotes the pull request) are never read. When a pull request has several comments with the same marker, only the newest counts.
- `AGENTS.md` (`--agents-md`; the workflow checks out the caller's), for the items under `## Mistakes that keep coming back`: each `- **Title** (…)` item's state is read from the start of its parenthesis, `checked: <name>` (the name up to the first `;`), `partly checked` or `human-judged`; anything else is `unstated`. A missing file leaves every kind `not in AGENTS.md`.

## What it counts

Per kind, the pull requests with at least one finding of that kind. A finding's `kind` is matched to an item's title case-insensitively (punctuation and spacing ignored); `other` findings are grouped by their title normalized the same way; a kind that matches no item is grouped by its own normalized text, with the state `not in AGENTS.md`. A kind becomes a row when it appears on **two or more** pull requests and isn't fully checked, or when it is **checked and still appears** on any pull request (the check misses a case). The proposed check is the `check` the reviewers named most often for the kind (ties alphabetical); without one, a checked kind proposes extending its named check, and the others fall back to the kind's usual check type (a test of the failing path, a round-trip test, a `fleet checks` workflow rule or zizmor audit, screenshots at both widths, or "a test (or a lint rule or a type) that fails on the case").

## The report

One issue per repository, titled "Repeated review findings" with the label `review-mining` (it must exist). The newest open one is rewritten each run; none open and nothing to report opens nothing; none open and rows to report opens one. The same text (minus its first line) is appended to the job summary. The body is:

```md
<!-- review-mining -->
Review findings that came back on more than one pull request of `owner/app` updated in the last 90 days (since 2026-07-03), …

| Kind | State | Pull requests | Last seen | Proposed check |
| --- | --- | --- | --- | --- |
| **Failure paths** | human-judged | [#10](…/pull/10), [#11](…/pull/11) | 2026-09-11 | a test of the failing path |

Tick a row and it becomes an Agent task: the tick and the issue opened from it stay through each rewrite.

- [ ] <!-- row:repeated:failure-paths --> **Failure paths** becomes an Agent task

<sub>3 pull request(s) updated in the window, 3 with review findings. From fleet's review-mining.yml ([run](…)).</sub>
```

- **Row ids.** Every row has a stable hidden id, `<!-- row:<table>:<key> -->`, on its checkbox line: the table is `repeated`, the key is the kind's title as a slug (`failure-paths`), or `other:<slug of the normalized title>` for `other` findings. The same kind gets the same id on every run, in every repository.
- **The checkbox.** Each row ends with a task-list line, "becomes an Agent task", so the owner's tick is a click on the issue. Before rewriting, the run reads the open issue's row lines (`- [ ]`/`- [x]`, the id, and an optional trailing ` → #N`): a row whose id survives keeps its tick and its ` → #N`; a row that disappears loses both (the issue it pointed at stays). `FLEET-MINING-TASKS` (#23) opens an issue from a ticked row and writes the ` → #N` back; `FLEET-MINING-SIGNALS` (#22) adds more tables with the same row shape.
- **Defused.** Kinds, titles, checks and file names go through `agent_review.defuse` before rendering: `@` can't mention anyone and `<!--` can't forge a hidden line; `|` is escaped in table cells. The only HTML comments in the body are the first-line marker and the row ids fleet writes.
- "Last seen" is the date of the newest review comment that carried the finding.

## The workflow

`review-mining.yml` has one job, `mine`, on `ubuntu-latest`, with `contents: read` (the caller's `AGENTS.md`, checked out sparse with `persist-credentials: false`), `issues: write` (the one issue; it also reads the comments) and `pull-requests: read`, and no secrets. It checks out fleet at the pinned ref into `.fleet/` (`fleet_tools` and `VERSION`) and runs `python3 -E -s -m fleet_tools.cli review-mine` from there, so nothing of the caller's is on the tool's path. Inputs: `days` (default `90`), `dry-run` (print the issue instead of posting it) and `fleet-ref`. The caller schedules it and may run it by hand; the issue is rewritten each run, so a weekly schedule is as safe as the monthly one:

```yaml
name: Review mining

on:
  schedule:
    - cron: "17 6 1 * *"   # monthly; weekly instead: "17 6 * * 1"
  workflow_dispatch:

permissions: {}

jobs:
  mine:
    permissions:
      contents: read
      issues: write
      pull-requests: read
    uses: AnthonyPluth/fleet/.github/workflows/review-mining.yml@<commit>   # vX.Y.Z
```

Locally, from a checkout of fleet with `gh` signed in: `python3 -E -s -m fleet_tools.cli review-mine --repo owner/app --days 90 --agents-md /path/to/app/AGENTS.md --dry-run`.

## Testing it

`tests/test_review_mine.py` drives the command against the fake `gh` in `tests/fakes/security/` (recorded answers, logged writes): fixture comments give the expected table, kinds match AGENTS items case-insensitively and `other` titles group after normalizing, a findings line anywhere but line 3, a malformed or oversized record and another author's comment are ignored, a second run rewrites the open issue and keeps its ticks and links, rows keep their ids, and a dry run posts nothing. The self-test runs the workflow in dry run on fleet's own pull requests.
