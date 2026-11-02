# Review mining

How `.github/workflows/review-mining.yml` (`workflow_call`) and `fleet review-mine` turn the reviews' findings into the "Repeated review findings" issue, what the report looks like and what later issues may build on it. The rule it serves is the first line of the shared "Mistakes that keep coming back" block: a mistake that repeats gets a check. The reviews record each finding's kind; this job notices the repeats, deterministically and with no model. Beside the repeats, the same issue points at three other places where the harness needs to improve: the reviewer's guidance, when a kind is mostly dismissed; the merge path, when findings are still open at merge; and the app's verification tooling, when the same area keeps going unverified.

## What it reads

- The pull requests of the repository updated in the window (`--days`, default 90), listed with `gh api` page by page, newest first, until a page ends before the window.
- The issue comments updated in the window (`repos/<repo>/issues/comments?since=…`, paginated), kept only when they are on one of those pull requests, their author is `github-actions[bot]` and their first line is `<!-- agent-review -->` or `<!-- second-review -->`.
- Of such a comment, **only the third line**: the findings record `<!-- agent-review-findings <base64 JSON> -->` (or `second-review-findings`) that `agent_review.render` writes ([agent-review.md](agent-review.md)). The line must be at most 8 KB, decode to a JSON list of objects, and each finding must have a `severity` of `blocking` or `advisory`, a `kind` of at most 120 characters, a `title` of at most 200, and, when present, a `file` of at most 300 and a `check` of at most 300; a finding that doesn't is dropped, a record that isn't a list is ignored, and at most 50 findings of a record count. A findings line anywhere else in the comment, a comment by anyone else, and the rest of the comment (which quotes the pull request) are never read. When a pull request has several comments with the same marker, only the newest counts.
- Of the same comments (the newest per review and pull request), **the fourth line**: the outcomes record `<!-- agent-review-outcomes <base64 JSON> -->` (or `second-review-outcomes`), read with `review_outcomes.load` ([agent-review.md](agent-review.md)), with the pull request's `/fleet dismiss f:<id> <reason>` comments by the owner (`OWNER` association, a reason of at least 10 characters) applied on top. For this, each pull request with a review comment in the window, and each pull request merged in the window (even one last reviewed before it, whose history then serves only "Open at merge"), has its **whole** comment history read (`repos/<repo>/issues/<n>/comments`, page by page): the window picks the pull requests, not the comments, so a dismissal older than the window still counts. Up to 1,000 comments are read (one more page is asked for to tell exactly 1,000 from more); a history that fails to load or has more than 1,000 comments is skipped in "Often dismissed" and "Open at merge" and counted below them, never guessed; its findings still count as repeats. Each finding is `open`, `resolved` (fixed by a push) or `dismissed`, with the dismissal's reason and comment link.
- Where `--app-root` (the workflow checks out the default branch's `pyproject.toml` and Markdown files) sets `[tool.fleet.verification]`: the description of each pull request **merged in the window** (from the same list of pull requests; an open or closed-unmerged one is never read), parsed by `fleet_tools/verification.py` (`block` tells a description with no record, markers only inside a fenced example included, from one whose record doesn't parse; `parse` reads it; a record that raises `ParseError` is counted and skipped, its text never posted), and, for one with a `not verified` row, its changed files (`repos/<repo>/pulls/<n>/files`, paginated, up to 3,000, both names of a rename), matched to the verification guides' areas by `fleet_tools/verification_guides.py` (`load`, `areas`). A pull request whose files can't be listed is skipped and counted below the table, without the error's details. Nothing in a description or a guide is run.
- `AGENTS.md` (`--agents-md`; the workflow checks out the caller's), for the items under `## Mistakes that keep coming back`: each `- **Title** (…)` item's state is read from the start of its parenthesis, `checked: <name>` (the name up to the first `;`), `partly checked` or `human-judged`; anything else is `unstated`. A missing file leaves every kind `not in AGENTS.md`.

## What it counts

Per kind, the pull requests with at least one finding of that kind that the owner didn't dismiss. The findings of a review are those of its outcomes record, which keeps every finding the review ever raised on the pull request (`open`, `resolved` by a later push, or `dismissed`), plus any on the newest findings line without an outcome: a dismissed one doesn't count towards a repeat, a resolved one does and is counted in "Fixed". The proposed check and "Last seen" come from the newest findings line. A finding's `kind` is matched to an item's title case-insensitively (punctuation and spacing ignored); `other` findings are grouped by their title normalized the same way; a kind that matches no item is grouped by its own normalized text, with the state `not in AGENTS.md`. A kind becomes a row when it appears on **two or more** pull requests and isn't fully checked, or when it is **checked and still appears** on any pull request (the check misses a case). The proposed check is the `check` the reviewers named most often for the kind (ties alphabetical); without one, a checked kind proposes extending its named check, and the others fall back to the kind's usual check type (a test of the failing path, a round-trip test, a `fleet checks` workflow rule or zizmor audit, screenshots at both widths, or "a test (or a lint rule or a type) that fails on the case").

### The other tables

Each groups by kind the same way (AGENTS item, `other:` plus the normalized title, or the kind's own text), over the outcomes records.

- **Often dismissed**: a kind with **at least 3** dismissed findings in the window, where the dismissed findings are **at least half** of its findings (3 of 6 is listed, 3 of 7 isn't). Columns: kind; raised, fixed and dismissed counts; up to 3 dismissal reasons, each defused and cut to 120 characters, linked to the dismissal comment (only a `https://github.com/<repo>/…` link; otherwise the pull request's number); and the proposal "Review guidance: say when this isn't a finding, in the app's `.github/agent-review/context.md` or in fleet's `prompt.md` if it holds for every app".
- **Open at merge**: per kind, the findings still `open` on pull requests merged in the window, with the count and links to the pull requests. One is enough to list a kind. The outcomes here are a snapshot at the merge, read from the comments created before the pull request's `merged_at`, so a dismissal (or a review) after the merge doesn't hide a finding the merge let through. A re-review rewrites the review's existing comment in place, and GitHub keeps no history of the edit. GitHub's timestamps have one-second resolution, so a comment stamped in the same second as `merged_at` may have come just before or just after it. So when a comment that can carry outcomes was created or last updated at or after the merge instant (`>=` `merged_at`), the outcomes at the merge can't be known; only a comment created and last updated strictly before `merged_at` is used. A comment that can carry outcomes is a review comment (`agent-review` or `second-review` by `github-actions[bot]`), or a comment by the owner (`OWNER` association, whatever its text, so an edit out of a `/fleet dismiss` is caught too), or any comment holding `/fleet dismiss`. When such a comment was created or last updated at or after the merge instant, the pull request is left out of this table and counted below it ("N merged pull request(s) whose review or dismissal comments were posted or changed at or after the merge instant were skipped for Open at merge"), never guessed. This can under-report: a finding open at merge on such a pull request isn't listed, even when the owner's edit had nothing to do with it. Other comments created after the merge are simply left out. The other tables use the whole history.
- **Verification gaps**, only where the default branch sets `[tool.fleet.verification]`: the `not verified` rows of the merged pull requests' records, grouped by the guide areas whose globs match the pull request's changed files, or `ungrouped` when none does (or no `guides` is set). An area is listed when gaps show up on **2 or more** pull requests. Columns: area, pull requests, up to 3 sample dimensions with their reasons (the How cell, each defused and cut to 120 characters), and the proposal "a fake, flow or test that makes this verifiable, or an update to the area's guide". Below the table: the number of merged pull requests without a record, when `required` isn't `true`, and the number of records that didn't parse.

The issue is opened when any table has a row.

## The report

One issue per repository, titled "Repeated review findings" with the label `review-mining` (it must exist). The newest open one is rewritten each run; none open and nothing to report opens nothing; none open and rows to report opens one. The same text (minus its first line) is appended to the job summary. The body is:

```md
<!-- review-mining -->
Review findings that came back on more than one pull request of `owner/app` updated in the last 90 days (since 2026-07-03), …

### Repeated findings

| Kind | State | Pull requests | Fixed | Last seen | Proposed check |
| --- | --- | --- | --- | --- | --- |
| **Failure paths** | human-judged | [#10](…/pull/10), [#11](…/pull/11) | 1 | 2026-09-11 | a test of the failing path |

- [ ] <!-- row:repeated:failure-paths --> **Failure paths** becomes an Agent task

### Often dismissed

…

| Kind | Raised | Fixed | Dismissed | Dismissal reasons | Proposal |
| --- | --- | --- | --- | --- | --- |
| **Layout** | 5 | 1 | 4 | "desktop-only page" ([#12](…#issuecomment-1)); … | Review guidance: say when this isn't a finding, … |

- [ ] <!-- row:dismissed:layout --> **Layout** becomes an Agent task

### Open at merge

…

| Kind | Open at merge | Pull requests |
| --- | --- | --- |
| **Failure paths** | 2 | [#14](…/pull/14) |

- [ ] <!-- row:open-at-merge:failure-paths --> **Failure paths** becomes an Agent task

### Verification gaps

…

| Area | Pull requests | Samples | Proposal |
| --- | --- | --- | --- |
| **Screens and flows** | [#20](…/pull/20), [#21](…/pull/21) | Bank sync: no fake provider (#20); … | a fake, flow or test that makes this verifiable, or an update to the area's guide |

- [ ] <!-- row:gaps:screens --> **Screens and flows** becomes an Agent task

3 merged pull request(s) without a verification record (it isn't required).

<sub>3 pull request(s) updated in the window, 3 with review findings. From fleet's review-mining.yml ([run](…)).</sub>
```

- **Row ids.** Every row has a stable hidden id, `<!-- row:<table>:<key> -->`, on its checkbox line: the table is `repeated`, `dismissed`, `open-at-merge` or `gaps`; for the first three the key is the kind's title as a slug (`failure-paths`), or `other:<slug of the normalized title>` for `other` findings, a slug being the whole normalized text when it is at most 60 characters, and otherwise its first 47 followed by `-` and the first 12 hex digits of the SHA-256 of the whole text (so two kinds or titles that differ only late never share an id; short ids are unchanged); for `gaps` it is the guide's whole file name (`screens` for `screens.md`, never cut short, so two guides are never merged) or `ungrouped`. The same kind or area gets the same id on every run, in every repository.
- **The checkbox.** Each row ends with a task-list line, "becomes an Agent task", so the owner's tick is a click on the issue. Before rewriting, the run reads the open issue's row lines (`- [ ]`/`- [x]`, the id, and an optional trailing ` → #N`): a row whose id survives keeps its tick and its ` → #N`; a row that disappears loses both (the issue it pointed at stays). `FLEET-MINING-TASKS` (#23) opens an issue from a ticked row and writes the ` → #N` back. The tick of a row id is kept whichever table it is in.
- **Defused.** Kinds, titles, checks, file names, dismissal reasons, verification cells and guide areas go through `agent_review.defuse` before rendering and are cut short: `@` can't mention anyone and `<!--` can't forge a hidden line; every other `<` is written `&lt;`, so no tag renders; newlines are flattened and `|` is escaped in table cells. The only HTML comments in the body are the first-line marker and the row ids fleet writes.
- "Last seen" is the date of the newest review comment that carried the finding.

## The workflow

`review-mining.yml` has one job, `mine`, on `ubuntu-latest`, with `contents: read` (the default branch's `AGENTS.md`, `pyproject.toml` and Markdown files, which hold the verification guides, checked out sparse with `persist-credentials: false` and only read), `issues: write` (the one issue; it also reads the comments) and `pull-requests: read`, and no secrets. It checks out fleet at the pinned ref into `.fleet/` (`fleet_tools` and `VERSION`) and runs `python3 -E -s -m fleet_tools.cli review-mine` from there (with `--app-root` the caller's checkout), so nothing of the caller's is on the tool's path. Inputs: `days` (default `90`), `dry-run` (print the issue instead of posting it) and `fleet-ref`. The caller schedules it and may run it by hand; the issue is rewritten each run, so a weekly schedule is as safe as the monthly one:

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

Locally, from a checkout of fleet with `gh` signed in: `python3 -E -s -m fleet_tools.cli review-mine --repo owner/app --days 90 --agents-md /path/to/app/AGENTS.md --app-root /path/to/app --dry-run`.

## Testing it

`tests/test_review_mine.py` drives the command against the fake `gh` in `tests/fakes/security/` (recorded answers, logged writes): fixture comments give the expected table, kinds match AGENTS items case-insensitively and `other` titles group after normalizing, a findings line anywhere but line 3, a malformed or oversized record and another author's comment are ignored, a second run rewrites the open issue and keeps its ticks and links, rows keep their ids, and a dry run posts nothing. For the other tables: dismissed findings leave the repeats and fixed ones are counted, an owner's dismissal with a reason is the only one that counts, "Often dismissed" at 3 of 6 but not 3 of 7 or 2 of 2, reasons defused, cut and linked only to the repository's own comments, "Open at merge" only for merged pull requests, verification gaps only from pull requests merged in the window (an open one's record and files are never read), grouped by guide area or `ungrouped` at 2 pull requests, unparsable records counted and never posted, the count of pull requests without a record only when it isn't required, no table without the setting, and the same ids on a run with the data reversed. The self-test runs the workflow in dry run on fleet's own pull requests (fleet doesn't set `[tool.fleet.verification]`, so its report has no verification table).
