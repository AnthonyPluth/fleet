# Review mining

How `.github/workflows/review-mining.yml` (`workflow_call`) and `fleet review-mine` turn the reviews' findings into the "Repeated review findings" issue, what the report looks like and what later issues may build on it. The rule it serves is the first line of the shared "Mistakes that keep coming back" block: a mistake that repeats gets a check. The reviews record each finding's kind; this job notices the repeats, deterministically and with no model. Beside the repeats, the same issue points at three other places where the harness needs to improve: the reviewer's guidance, when a kind is mostly dismissed; the merge path, when findings are still open at merge; and the app's verification tooling, when the same area keeps going unverified; where the app asks for it, a gap that keeps coming back also becomes an issue of its own ([Recurring verification gaps become issues](#recurring-verification-gaps-become-issues)). At its top, a "Loop health" section shows whether the loop keeps turning, this run's numbers beside last run's ([loop-stats.md](loop-stats.md)).

## What it reads

- The pull requests of the repository updated in the window (`--days`, default 90), listed with `gh api` page by page, newest first, until a page ends before the window.
- The issue comments updated in the window (`repos/<repo>/issues/comments?since=…`, paginated), kept only when they are on one of those pull requests, their author is `github-actions[bot]` and their first line is `<!-- agent-review -->` or `<!-- second-review -->`.
- Of such a comment, **only the third line**: the findings record `<!-- agent-review-findings <base64 JSON> -->` (or `second-review-findings`) that `agent_review.render` writes ([agent-review.md](agent-review.md)). The line must be at most 8 KB, decode to a JSON list of objects, and each finding must have a `severity` of `blocking` or `advisory`, a `kind` of at most 120 characters, a `title` of at most 200, and, when present, a `file` of at most 300 and a `check` of at most 300; a finding that doesn't is dropped, a record that isn't a list is ignored, and at most 50 findings of a record count. A findings line anywhere else in the comment, a comment by anyone else, and the rest of the comment (which quotes the pull request) are never read. When a pull request has several comments with the same marker, only the newest counts.
- Of the same comments (the newest per review and pull request), **the fourth line**: the outcomes record `<!-- agent-review-outcomes <base64 JSON> -->` (or `second-review-outcomes`), read with `review_outcomes.load` ([agent-review.md](agent-review.md)), with the pull request's `/fleet dismiss f:<id> <reason>` comments by the owner (`OWNER` association, a reason of at least 10 characters) applied on top. For this, each pull request with a review comment in the window, and each pull request merged in the window (even one last reviewed before it, whose history then serves only "Open at merge"), has its **whole** comment history read (`repos/<repo>/issues/<n>/comments`, page by page): the window picks the pull requests, not the comments, so a dismissal older than the window still counts. Up to 1,000 comments are read (one more page is asked for to tell exactly 1,000 from more); a history that fails to load or has more than 1,000 comments is skipped in "Often dismissed" and "Open at merge" and counted below them, never guessed; its findings still count as repeats. Each finding is `open`, `resolved` (fixed by a push) or `dismissed`, with the dismissal's reason and comment link.
- Where `--app-root` (the workflow checks out the default branch's `pyproject.toml` and Markdown files) sets `[tool.fleet.verification]`: the description of each pull request **merged in the window** (from the same list of pull requests; an open or closed-unmerged one is never read), parsed by `fleet_tools/verification.py` (`block` tells a description with no record, markers only inside a fenced example included, from one whose record doesn't parse; `parse` reads it; a record that raises `ParseError` is counted and skipped, its text never posted), and, for one with a `not verified` row, its changed files (`repos/<repo>/pulls/<n>/files`, paginated, both names of a rename; a list of 3,000 files is GitHub's cap and may be cut short, so it counts as not listed), matched to the verification guides' areas by `fleet_tools/verification_guides.py` (`load`, `areas`). A pull request whose files can't be listed in full (a failing call, or the 3,000-file cap) is skipped and counted below the table, without the error's details. Nothing in a description or a guide is run.
- Where the default branch also sets `[tool.fleet.verification.gap-issues]`: the same descriptions and each pull request's author (`user.login`), and, where `guides` is set, the changed files of every pull request merged in that table's window (each list read once a run, shared with the table above), for [the gap issues](#recurring-verification-gaps-become-issues).
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

One issue per repository, titled "Repeated review findings" with the label `review-mining` (it must exist). The newest open one is rewritten each run; none open and nothing to report opens nothing; none open and rows to report opens one. The same text (minus its first line, the hidden `loop-stats` line and the "Loop health" section, which the job summary already has in full from `loop-stats`) is appended to the job summary. The body is:

```md
<!-- review-mining -->
<!-- loop-stats eyJkYXRlIjoi… -->
### Loop health

Whether review, verification and this report are improving the harness, from fleet's `loop-stats` over the last 30 days: …

| Measure | Last run (2026-09-01) | This run (2026-10-01) |
| --- | --- | --- |
| Merged pull requests | 9 | 12 |
| … with an Agent review | 7 | 10 |
| … resolved | 30% | 45% |
| … open at merge | n/a | 5% |
…

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

- **Loop health.** With `--stats` (as `review-mining.yml` runs it), the section and the hidden second line `<!-- loop-stats <base64 JSON> -->` that carries this run's headline numbers to the next run; the line is read back only at that position, under a strict schema and a 2,048-byte cap, and ignored on any malformed value ([loop-stats.md](loop-stats.md#in-the-report-loop-health)). Without `--stats`, neither.
- **Row ids.** Every row has a stable hidden id, `<!-- row:<table>:<key> -->`, on its checkbox line: the table is `repeated`, `dismissed`, `open-at-merge` or `gaps`; for the first three the key is the kind's title as a slug (`failure-paths`), or `other:<slug of the normalized title>` for `other` findings, a slug being the whole normalized text when it is at most 60 characters, and otherwise its first 47 followed by `-` and the first 12 hex digits of the SHA-256 of the whole text (so two kinds or titles that differ only late never share an id; short ids are unchanged); for `gaps` it is the guide's whole file name (`screens` for `screens.md`, never cut short, so two guides are never merged) or `ungrouped`. The same kind or area gets the same id on every run, in every repository.
- **The checkbox.** Each row ends with a task-list line, "becomes an Agent task", so the owner's tick is a click on the issue. Before rewriting, the run reads the open issue's row lines (`- [ ]`/`- [x]`, the id, and an optional trailing ` → #N`): a row whose id survives keeps its tick and its ` → #N`; a row that disappears drops out of the body with them, and its issue stays; when the row comes back, it gets that issue's ` → #N` and its tick back through the issue's `mined-from` marker (below). The owner's tick opens an Agent task issue for the row and writes the ` → #N` back ([Ticked rows become Agent tasks](#ticked-rows-become-agent-tasks)). The tick of a row id is kept whichever table it is in. The run also lists the repository's `review-mining` issues, open and closed, and a row whose `mined-from` marker is on one opened by `github-actions[bot]` gets that issue's ` → #N` (and its tick) back if the body lost it.
- **Defused.** Kinds, titles, checks, file names, dismissal reasons, verification cells and guide areas go through `agent_review.defuse` before rendering and are cut short: `@` can't mention anyone and `<!--` can't forge a hidden line; every other `<` is written `&lt;`, so no tag renders; newlines are flattened and `|` is escaped in table cells. The only HTML comments in the body are the first-line marker, the hidden `loop-stats` line and the row ids fleet writes.
- "Last seen" is the date of the newest review comment that carried the finding.

## The workflow

`review-mining.yml` has one job, `mine`, on `ubuntu-latest`, with `contents: read` (the default branch's `AGENTS.md`, `pyproject.toml` and Markdown files, which hold the verification guides, checked out sparse with `persist-credentials: false` and only read, and the default branch's commits and file contents the loop stats read through the API), `issues: write` (the one issue; it also reads the comments) and `pull-requests: read`, and no secrets. It checks out fleet at the pinned ref into `.fleet/` (`fleet_tools` and `VERSION`) and, from there, so nothing of the caller's is on the tool's path, runs `python3 -E -s -m fleet_tools.cli loop-stats` (its Markdown appended to the job summary, its JSON uploaded as the `loop-stats.json` artifact, SHA-pinned `actions/upload-artifact`, `retention-days: 90`), then `review-mine --stats` with that JSON (both with `--app-root` the caller's checkout); the rewrite runs even when the measuring or the upload failed, and that failure still fails the job, but never after a failed checkout of the app or of fleet (`if: ${{ !cancelled() && steps.app.outcome == 'success' && steps.fleet.outcome == 'success' }}`), which would rewrite the report without the app's `AGENTS.md` and verification settings. Inputs: `days` (default `90`), `loop-stats-days` (default `30`, the loop stats' window), `dry-run` (print the issue, and the gap issues it would open, instead of posting them; the job summary and the artifact are still produced) and `fleet-ref`; output `loop-stats-url`, the artifact's link. The caller schedules it and may run it by hand; the issue is rewritten each run, so a weekly schedule is as safe as the monthly one:

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
    uses: AnthonyPluth/fleet/.github/workflows/review-mining.yml@<commit>   # v3.3.0
```

Locally, from a checkout of fleet with `gh` signed in: `python3 -E -s -m fleet_tools.cli review-mine --repo owner/app --days 90 --agents-md /path/to/app/AGENTS.md --app-root /path/to/app --dry-run`.

## Ticked rows become Agent tasks

`.github/workflows/mining-tasks.yml` (`workflow_call`) and `fleet mining-tasks` act on the owner's tick. The tick is the human review step; after it, opening the issue is mechanical, so the loop goes from finding to task without anyone retyping it.

**When it runs.** The caller triggers it on `issues: [edited]`. Its one job, `tasks`, runs only when the edited issue has the label `review-mining`, was opened by `github-actions[bot]` and the edit's sender is the repository's owner (`github.event.sender.login == github.repository_owner`; in a repository owned by an organization that never holds, so nothing runs). `fleet mining-tasks` checks the same again from the event, and also that the action is `edited`, the issue isn't a pull request, its body starts with `<!-- review-mining -->` (the report, not a task issue opened from it) and the edit changed the body (`changes.body.from` is there; only that the body was edited matters, not what changed). Anything else prints why and does nothing. The edits it makes itself use the workflow's token, which starts no workflow. Concurrency `mining-tasks-<issue>`, never cancelled. GitHub keeps one running and one waiting run per group, so a third quick edit drops the waiting one; that loses nothing, because every run acts on the report as it is then, not on its own event's difference.

**What it opens.** The event only says that the owner edited the report's body; what to act on comes from the report as it is now. Outside a dry run it reads the report again (`GET repos/<repo>/issues/<n>`; a dry run uses the event's body) and takes every row (by its `row:` id) that is ticked (`[x]`) and has no ` → #N`, in order, at most 20 per run (the rest wait for the next edit). So a tick whose own run was dropped or failed is acted on by the next run, a tick withdrawn before the run reads the report opens nothing, and unticked or linked rows do nothing. For each such row it reads the row's cells from its table (the table rows and checkbox lines of a section pair up in order; a section where they don't is skipped and the row printed as skipped) and opens one issue, label `review-mining`, no assignee:

| Table (row id) | Title | Scope | Model |
| --- | --- | --- | --- |
| Repeated findings (`repeated:`) | `Check for "<kind>"` | the Proposed check | Opus |
| Often dismissed (`dismissed:`) | `Review guidance for "<kind>"` | the Proposal | Opus: it changes the reviewer's guidance |
| Open at merge (`open-at-merge:`) | `Findings left open at merge: "<kind>"` | a check on the merge path, or of the kind, that stops such a finding staying open at merge | Opus |
| Verification gaps (`gaps:`) | `Verification gap: <area>` | the Proposal | Opus |

The body is in the agent-task shape (a `Verification gap` issue first has the four header lines of [the gap issues](#the-issue), its `Area:` the guide's file name from the row id, `Gap:` the Samples, `Evidence:` the pull requests' numbers and `Would make it verifiable:` the Proposal, and its first acceptance criterion is theirs): Context (each cell of the row as `- **Column**: value`, and a link to the report), Scope ("The row's proposal: …"), Out of scope, Acceptance criteria (`- [ ] The check, guidance or fake named in Scope exists and a test shows it catches the case.` and `- [ ] \`make check\` passes.`), Tests, Depends on ("Nothing."), Model, then a last line saying it was opened from a ticked row of the report and the owner edits it before an agent picks it up, and the hidden `<!-- mined-from row:<id> -->`. Starting an agent session on it and choosing its priority are left to the owner.

**Links back.** Right before writing, it reads the report once more and appends ` → #N` to each acted-on row's checkbox line of that freshest body (`\r\n` line endings kept; nothing else of the body changes), in one `PATCH`. If opening one issue fails, the others are still linked, the failed row stays ticked without a link for the next run to retry, and the run fails. If the `PATCH` fails, the run fails naming each row and the issue opened for it; the next run links them through their `mined-from` markers, opening nothing new. `fleet review-mine` keeps the tick and the ` → #N` on every row whose id survives its next rewrite, and also restores a missing ` → #N` (and the tick) on any row whose `mined-from` marker is on a `review-mining` issue opened by `github-actions[bot]`; a row that disappears drops out of the body, its issue stays, and a returning row gets that issue's ` → #N` and tick back through the marker.

**Once per row.** Before opening, it lists the repository's `review-mining` issues, open and closed (`repos/<repo>/issues?state=all&labels=review-mining`, paginated), and collects the `<!-- mined-from row:<id> -->` lines of those opened by `github-actions[bot]` (a marker in anyone else's issue doesn't count). A row whose marker exists opens nothing and is linked to the oldest such issue instead, so running the same event twice opens no second issue. The two writers of the report, this job and `review-mining.yml`'s `mine`, share no concurrency group: a shared group with one waiting slot would let an owner's edit drop the scheduled report run. Each instead reads the body right before writing it, and the markers are the record of what was opened: a link lost to the other writer's overlapping write is put back by the next run of either.

**Defused.** The title, every cell in the body and the rewritten row go through `agent_review.defuse` again, whatever the report's text was: `@` can't mention anyone, `<!--` can't forge a hidden line or a `mined-from` marker, every other `<` is written `&lt;`, newlines are flattened, and each cell is cut to 600 characters (the title to 200). The only HTML comments in a new issue are its `mined-from` marker; in the rewritten row, its `row:` id.

The workflow has `issues: write` only and no secrets, checks out only fleet at the pinned ref into `.fleet/` (`fleet_tools`, `VERSION` and the event fixtures) and runs `python3 -E -s -m fleet_tools.cli mining-tasks --repo "$REPO" --event "$GITHUB_EVENT_PATH"` from there; event values reach it only through the event file and `env:`. Inputs: `dry-run` (print the issues and links instead of writing them), `fixture` (self-test only: an event payload in fleet's checkout read in place of the real one; implies `dry-run`) and `fleet-ref`. The caller:

```yaml
name: Mining tasks

on:
  issues:
    types: [edited]

permissions: {}

jobs:
  tasks:
    permissions:
      issues: write
    uses: AnthonyPluth/fleet/.github/workflows/mining-tasks.yml@<commit>   # v3.3.0
```

Locally, with `gh` signed in and an event payload saved to a file: `python3 -E -s -m fleet_tools.cli mining-tasks --event event.json --repo owner/app --dry-run`.

## Recurring verification gaps become issues

The "Verification gaps" table waits for the owner's tick. Where the app asks for it, `fleet review-mine` also opens an issue by itself for a verification gap that keeps coming back, so the next agent can make that dimension verifiable. It is the first step of a self-reinforcing loop: a gap recorded on several pull requests becomes one issue, and the issue's work makes the gap go away.

**Opt-in.** It runs only where the default branch's `pyproject.toml` has the table (none of the apps had it when it shipped, and an app without it sees no change at all):

```toml
[tool.fleet.verification]
guides = "docs/verification"

[tool.fleet.verification.gap-issues]
pulls = 3   # the same gap on at least this many merged pull requests (default 3, from 2 to 100)
days = 60   # merged within this many days before the run (default 60, from 1 to 3650; at most the workflow's `days`)
```

Both keys are optional; the table's presence is the switch, and deleting it turns the issues off again (the ones already opened stay). A key out of range, any other key, or an `exempt-authors` that isn't a list of strings opens no gap issue and fails the run with the reason, after the report is rewritten. A `days` longer than the report's window is cut to it, and the run says so.

**What counts.** The pull requests merged in the last `days` (from the same list the report reads), except those by `[tool.fleet.verification]`'s `exempt-authors` (default `dependabot[bot]` and `fleet-bot[bot]`). Two kinds of gap:

- **The same reason**: the `not verified` rows of the pull requests' verification records (`fleet_tools/verification.py`; a description whose record doesn't parse has none), grouped by their How cell normalized as the report normalizes a title (lower case, every run of anything but letters and digits one space). A pull request counts once per reason, however many of its rows give it.
- **The same uncovered folder**, only where `guides` is set and every guide parses: the changed files (both names of a rename) that no guide's `## Paths` covers, outside the guides directory, the same paths the `verification-guides` check warns about, grouped by their folder: the first two directories of the path (`app/providers/` for `app/providers/bank/sync.py`, `lib/` for `lib/x.py`). A file at the repository root has no folder and never counts. A pull request whose files can't be listed in full counts for its reasons and not for folders.

A gap is listed when it shows up on at least `pulls` pull requests. Gaps open in order, most pull requests first, at most 3 a run; the rest wait for the next run.

**Once per gap.** Each gap has a stable id: `verification-gap:reason:<slug of the normalized reason>` (the report's slug: the whole text up to 60 characters, else its first 47 and 12 hex digits of its SHA-256) or `verification-gap:paths:<slug of the folder, up to 40>-<12 hex digits of the folder's SHA-256>` (exact, so `a-b/` and `a/b/` stay apart). The issue ends with `<!-- mined-from row:<id> -->`, the marker the ticked rows' issues carry; before opening, the run reads the same list of the repository's `review-mining` issues, open and closed, and a gap whose marker is on one opened by `github-actions[bot]` opens nothing (a marker in anyone else's issue doesn't count). So closing the issue, fixed or not planned, also keeps the gap from opening another, even while the old pull requests are still in the window. A known limitation: `review-mining.yml`'s job has no concurrency group (a fixed one would cancel a waiting run whenever a third overlaps, `cancel-in-progress: false` or not, leaving a cancelled check run, as fleet's own Self-test, which calls this workflow on every pull request, would see), so two runs that overlap (one started by hand while the scheduled one is still going) each read the markers before either opens, and can each open the same gap's issue; close the second as a duplicate (its marker keeps the gap from opening a third). The loop stats count these issues apart from the rows' tasks, as `gap_issues_opened` ([loop-stats.md](loop-stats.md)), although they carry the same marker.

### The issue

Titled `Verification gap: <the reason>` or `Verification gap: no verification guide covers <folder>`, with the label `review-mining`, no assignee. The reason or folder is pull request text too, so in the title it keeps only letters, digits, spaces and `._/,;'()-` (anything else, such as `@`, `#`, a backtick, `<`, `[` or a newline, becomes a space, and runs of spaces become one) and is cut to 80 characters at a word, ending ` …`: no mention, reference, code span or tag survives there. Its first four lines are a fixed header, one line each, in this order, so a skill can read it without parsing prose:

```md
Area: `screens`, `sync`
Gap: `no fake provider; a fake under 'tests/fakes/' would make it verifiable`
Evidence: #20, #21, #25
Would make it verifiable: what the gap names, or else a fake under `tests/fakes/`, a fixture or a flow that exercises it, and a step that uses it in the area's verification guide (`## How to verify`)
```

- `Area:` for a reason, the guides (by file name, `screens` for `screens.md`) that cover the changed files of those pull requests, the most common first, at most 3, or `` `ungrouped` `` when none does or `guides` isn't set; for a folder, the folder.
- `Gap:` for a reason, the reason as the lowest-numbered of those pull requests wrote it; for a folder, ``no verification guide covers `<folder>` ``.
- `Evidence:` the pull requests' numbers, lowest first, at most 20 and then `and N more`.
- `Would make it verifiable:` for a reason, the text above; for a folder, ``a verification guide whose `## Paths` covers `<folder>**`, with the steps that verify a change there (the fleet-verification-guide skill writes it)``.

Every value from a pull request (a reason, a dimension's name, a file's or a folder's name) is in a code span: newlines and runs of spaces flattened, control characters replaced, backticks written `'`, then defused as the report is (`@` can't mention anyone, `<!--` is written `&lt;!--`) and cut short (a reason to 300 characters in the header, 120 in a list). Inside a code span GitHub renders no Markdown, link, image, mention or reference, so nothing a pull request wrote is rendered or notifies anyone. The numbers are the API's. The only HTML comment is the marker on the last line.

After the header, the agent-task shape: Context (why it was opened, then one line per pull request, a link to it with the dimensions that gave the reason, or the uncovered files under the folder, at most 3 each), Scope (for a reason: build what the gap names, or else a fake under `tests/fakes/`, a fixture or a flow, and add the step that uses it to the area's guide, out of `## Not verifiable yet`; for a folder: write or extend the guide so its `## Paths` covers it, with steps; in both, run that step once in the session), Out of scope, Acceptance criteria:

```md
- [ ] The dimension can be verified: a guide, skill or fixture exists for it, and its steps were run once in the session.
- [ ] `make check` passes.
```

then Tests, Depends on ("Nothing."), Model (Opus: the evidence quotes pull requests, which are untrusted), and a last paragraph saying the bot opened it, so it isn't a task yet: an agent starts on it only once the owner labels it (the shared starting-work rule, "The owner's issue is the task"); the only label the bot sets is `review-mining`.

**When something fails.** The issues are opened after the report is rewritten, so they never hold it up. If opening one fails, the others are still opened, the run fails naming the gap, and the next run tries again (no marker was written). `fleet review-mine` then exits 1, as it does for settings that don't parse (and for a failing `gh api` call before the report is written); otherwise 0, and 2 for usage. A dry run (`dry-run: true`, `--dry-run`) prints each issue it would open and posts nothing; either way, the job summary ends with a "Verification gap issues" list of what was (or would be) opened, when there is any.

## Testing it

`tests/test_review_mine.py` drives the command against the fake `gh` in `tests/fakes/security/` (recorded answers, logged writes): fixture comments give the expected table, kinds match AGENTS items case-insensitively and `other` titles group after normalizing, a findings line anywhere but line 3, a malformed or oversized record and another author's comment are ignored, a second run rewrites the open issue and keeps its ticks and links, rows keep their ids, and a dry run posts nothing. For the other tables: dismissed findings leave the repeats and fixed ones are counted, an owner's dismissal with a reason is the only one that counts, "Often dismissed" at 3 of 6 but not 3 of 7 or 2 of 2, reasons defused, cut and linked only to the repository's own comments, "Open at merge" only for merged pull requests, verification gaps only from pull requests merged in the window (an open one's record and files are never read), grouped by guide area or `ungrouped` at 2 pull requests, unparsable records counted and never posted, the count of pull requests without a record only when it isn't required, no table without the setting, and the same ids on a run with the data reversed. The "Loop health" section: the hidden line round-trips last run's numbers, a run over another window isn't compared, a malformed, misplaced or oversized one is ignored, an unreadable stats file keeps last run's numbers, and without `--stats` nothing changes ([loop-stats.md](loop-stats.md#testing-it)). The gap issues (`GapIssuesTest`): nothing opens without `[tool.fleet.verification.gap-issues]` (the apps' fixtures and fleet itself have none); the same reason on three merged pull requests, told apart only by case and punctuation, opens one issue whose first four lines are the header, with the guides covering their files as its area and the acceptance criterion above; two pull requests don't, unless `pulls = 2`; the window holds at its edge, a shorter `days` leaves older pull requests out and a longer one is cut to the report's; a pull request counts once per reason; without guides the area is `ungrouped` and no file list is read; a marker on a bot's issue, open or closed, opens no second one and a marker on anyone else's doesn't count; exempt authors don't count (and do with `exempt-authors = []`); an uncovered folder on three pull requests opens one issue, root files, the guides directory and covered files left out, and none without guides or with a guide that doesn't parse; unreadable file lists count for reasons only; at most 3 a run, most pull requests first; a dry run posts nothing; hostile reasons, dimensions and file names stay in code spans, defused, with the marker the only HTML comment; settings out of range fail the run after the report; a failed open still opens the others and fails the run; the ids are stable and folders whose slugs meet stay apart. The self-test runs the workflow in dry run on fleet's own pull requests (fleet doesn't set `[tool.fleet.verification]`, so its report has no verification table and no gap issues), and `review-mining-artifact` checks the `loop-stats.json` artifact was uploaded. `tests/test_mining_tasks.py` drives `fleet mining-tasks` against the same fake `gh` with event fixtures: the owner ticking one row opens one issue and links it, two rows ticked in one edit open two, each table's title, scope and model, a `Verification gap` issue's header and acceptance criterion; a tick by anyone else, an edit of an issue not opened by the bot or without the label, and unticking or re-ticking a linked row do nothing; running the same event twice opens no second issue, a marker in an issue not opened by the bot doesn't count, a failed open still links the others; the new issue and the rewritten row are defused; a tick whose run was dropped and a failed open are picked up by the next edit, a tick withdrawn before the run reads the report opens nothing, the link is merged onto the freshest body, a failed link names the opened issues and the next run links them, more than 20 rows wait for the next run, and a dry run writes nothing. The self-test runs `mining-tasks.yml` in dry run on `tests/fixtures/events/mining-tasks-tick.json`.
