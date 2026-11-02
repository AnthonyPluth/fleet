# Loop stats

How `fleet loop-stats` measures whether the loop keeps turning: review findings and their outcomes, verification records and guides, the "Repeated review findings" report and the tasks opened from it, and the checks, guidance and skills those tasks produce. A stalled loop looks the same as a quiet one (nobody dismisses or fixes, records stop, rows are never ticked, `context.md` never changes); counting each step per repository shows where it stalls. It is deterministic and needs no outside service: `review-mining.yml` ([review-mining.md](review-mining.md)) runs it each time and puts the numbers in the job summary, a JSON artifact and a "Loop health" section of the report issue. There is no roll-up across repositories.

## The command

```sh
python3 -E -s -m fleet_tools.cli loop-stats --repo owner/app --days 30 --app-root /path/to/app --json loop-stats.json --markdown loop-stats.md
```

`--days` (default 30, 1 to 3650) is the window before now (`--now`, tests only, moves its end). `--app-root` is a checkout of the default branch, read for `[tool.fleet.verification]` and its guides only; without it there are no verification numbers. Everything else is read with `gh api` (REST): the pull requests, their comments and changed files (a pull request whose list reaches GitHub's 3,000-file cap may be cut short, so it counts in `unread_files` and is named in `missing`, not read as complete), and a verification guide that can't be loaded (or a guides directory that is missing, empty or over its cap) makes `guides`, `uncovered_paths` and `pulls_with_uncovered_paths` `null` and `uncovered_samples` empty, named in `missing`, instead of counting paths a skipped guide would have covered as uncovered, the `review-mining` issues, and the default branch's commits and file contents. Nothing it reads is run.

A part that can't be read (a failing API call, a list over its cap, a file that isn't UTF-8) is `null` in the JSON, `n/a` or "Not read." in the Markdown, and named in `missing` and under "Not read"; it is never counted as zero. The command exits 0 whenever it wrote both files, 1 when it can't write them, 2 on a usage error.

## What it counts

- **Reviews**, over the pull requests **merged in the window** (from `repos/<repo>/pulls?state=all&sort=updated`, page by page while a page ends inside the window, at most 30 pages; more is "not read", never cut short). For each, its whole comment history (up to 1,000 comments, as `review-mine` reads it; one that fails or is longer is counted in `unread` and left out): whether it has an "Agent review" and a "Second review" comment by `github-actions[bot]`, and the findings of each review, from its outcomes record with the owner's `/fleet dismiss` comments applied (`review_outcomes.load`), plus the findings of its newest findings line that have no outcome yet (`open`). A finding's severity comes from the newest findings line; one that isn't on it any more (fixed and gone from the latest review) is `unknown`. `resolved`, `dismissed` and `open` are counted now; `open_at_merge` counts the same findings from the comments created before the merge, as `review-mine`'s "Open at merge" does, and a pull request whose review or dismissal comments were posted or changed at or after the merge instant is left out of it and counted in `indeterminate_at_merge`.
- **Verification**, only where the default branch sets `[tool.fleet.verification]`: of the same merged pull requests, those whose description has a record (`fleet_tools/verification.py`), without one, and whose record doesn't parse; the dimensions `verified` and `not verified`, and the mean `not verified` per record; and the changed paths (`repos/<repo>/pulls/<n>/files`, both names of a rename, up to 3,000 per pull request) that no verification guide covers (all of them when `guides` isn't set), with the first 20, sorted.
- **The report**: the rows of the open "Repeated review findings" issue now (its `row:` checkbox lines) and how many are ticked, and the `review-mining` issues opened by `github-actions[bot]` carrying a `<!-- mined-from row:… -->` marker: all of them, those opened in the window (created between its start and its end, so a task created after the window's end isn't counted in it) and those closed in it. `review-mining.yml` measures before it rewrites the report, so the rows and ticks are the report as the owner left it over the last period.
- **Harness changes on the default branch** in the window: the commits touching `.github/agent-review/context.md`, `.github/agent-review/invariants.md`, the verification guides' directory (only where `guides` is set), the app's own skills (`.claude/skills/`, a commit counted only when it changes a file outside `.claude/skills/fleet-*`, read from each commit's file list, at most 100 such commits) and `.semgrep/`, and the distinct commits among them (`null` when any part wasn't read, or when `pyproject.toml` can't be parsed, since the verification guides' directory is then unknown); the "Mistakes that keep coming back" items of `AGENTS.md` (read through the contents API, at most 1 MB and UTF-8, or "not read"; a missing file has no items) whose parenthesis became `checked:` between the last commit before the window (strictly before it: the query asks for commits up to one second before the window's start, since GitHub's `until` includes the instant itself) and the last commit in it (an item counts once, however often it changed in between; one already checked at the start, or added in the window, doesn't count; no commit before the window leaves it `null`); and the days since each guidance file last changed: `AGENTS.md`, `context.md`, `invariants.md` and each verification guide (`null` when it never did).
- **fleet**: `.fleet/version` on the default branch (`null` when the file is missing; anything but `vX.Y.Z` is "not read") and whether a pull request from `fleet/sync` is open.

A share or a mean whose denominator is zero is `null` in the JSON and `n/a` in the Markdown.

## The JSON, schema 1

```json
{
  "schema": 1,
  "repo": "owner/app",
  "generated": "2026-10-01T00:00:00Z",
  "since": "2026-09-01T00:00:00Z",
  "days": 30,
  "default_branch": "main",
  "reviews": {
    "merged": 3, "unread": 0, "with_agent_review": 2, "with_second_review": 1,
    "findings": {
      "raised": 5, "by_severity": {"blocking": 2, "advisory": 2, "unknown": 1},
      "resolved": 2, "dismissed": 1, "open": 2, "resolved_share": 0.4, "dismissed_share": 0.2,
      "at_merge": 5, "open_at_merge": 3, "open_at_merge_share": 0.6, "indeterminate_at_merge": 0
    }
  },
  "verification": {
    "configured": true, "guides": 2, "merged": 3, "with_record": 2, "without_record": 1, "unparsed": 0,
    "verified": 1, "not_verified": 3, "mean_not_verified": 1.5,
    "uncovered_paths": 3, "pulls_with_uncovered_paths": 2, "unread_files": 0, "uncovered_samples": ["README.md"]
  },
  "report": {"issue": 60, "rows": 3, "ticked": 1, "tasks_opened": 3, "tasks_opened_in_window": 1, "tasks_closed_in_window": 1},
  "harness": {
    "commits": {"context": 1, "invariants": 0, "verification_guides": 1, "skills": 1, "semgrep": 1, "total": 3},
    "checked_in_window": 1, "checked_items": ["Layout"],
    "days_since_change": {"AGENTS.md": 10, ".github/agent-review/context.md": 0, ".github/agent-review/invariants.md": null}
  },
  "fleet": {"version": "v1.0.0", "sync_open": true},
  "missing": []
}
```

- Each group (`reviews`, `verification`, `report`, `harness`, `fleet`) is `null` when it couldn't be read at all; inside one, a single number is `null` when its own part couldn't. `verification` is `{"configured": false}` where `[tool.fleet.verification]` isn't set.
- Shares are rounded to 4 decimals, between 0 and 1. `missing` lists what wasn't read, one sentence each, never pull request text.
- Strings from pull requests (`uncovered_samples`) are data: control characters replaced, cut to 300 characters. The Markdown defuses them (`@` can't mention anyone, `<!--` can't forge a hidden line, every other `<` is `&lt;`, `|` escaped, backticks replaced).
- Every key above is always written: a group is the whole object or `null`, and `verification` is the whole object or `{"configured": false}`; `uncovered_samples`, `checked_items` and `missing` are lists and `days_since_change` a map of path to days. `review-mine --stats` accepts exactly this structure, from the same fleet checkout: a missing or unknown key, a group or nested object that isn't one, or a list that isn't one makes the file unreadable (below), while an explicit `null` is a number this run couldn't read. A field is added or removed together with that check; removing or renaming one, or changing its meaning, also takes `schema: 2`.

## In the report: "Loop health"

`fleet review-mine --stats loop-stats.json` puts a "Loop health" section at the top of the "Repeated review findings" issue, right after the first-line marker: a table of the headline numbers (merged pull requests, with an Agent review, with a Second review, findings raised, resolved, dismissed and open-at-merge shares, pull requests with a verification record, mean `not verified`, uncovered paths, rows and ticked rows, issues opened from rows and closed in the window, commits changing the harness, items newly `checked:`), this run's beside last run's. Last run's column is shown only when it covered the same number of days as this run (the window is in the text above the table); when the caller changed `loop-stats-days` in between, only this run's numbers are shown, with a line saying the last run covered another window and isn't compared, and the next run with the same window compares again.

Last run's numbers come from the issue itself: its **second line** is a hidden `<!-- loop-stats <base64 JSON> -->` that each run writes, holding `{"schema": 1, "date": "YYYY-MM-DD", "days": N, "values": {…}}`. The line is read only at that position and only when the first line is `<!-- review-mining -->`, and it is untrusted (anyone who can edit the issue can change it): at most 2,048 bytes, strict base64, UTF-8 JSON, exactly those four keys, `schema` 1, a real date, `days` 1 to 36,500, and `values` holding only known keys, each `null`, a count (an integer 0 to 10⁹), a share (0 to 1) or a mean (0 to 10⁶); booleans, strings, `NaN` and infinities are refused. Anything else and the whole line is ignored: the section shows only this run's numbers and says there are no earlier ones. Only numbers are kept in it, never text.

When this run's `loop-stats.json` can't be read (missing, over 1 MB, not UTF-8 JSON, not schema 1, not the whole structure above, written for another repository than the one `review-mine` is run for, or a headline number out of range), `review-mine` warns, the section says so and shows last run's numbers, and the hidden line keeps them for the next run. Without `--stats` the report is as before: no section and no hidden line. The report's text in the job summary leaves out the section and the hidden line: the job summary already has the full numbers from `loop-stats`' own Markdown, so they show once. When nothing is listed and no report is open, no issue is opened, so there are no earlier numbers for the next run; the job summary and the artifact still have them.

## In the workflow

`review-mining.yml` runs `fleet loop-stats` over `loop-stats-days` (default `30`) before rewriting the report, appends its Markdown to the job summary, uploads `loop-stats.json` as the artifact `loop-stats.json` (`actions/upload-artifact`, SHA-pinned, `archive: false`, `retention-days: 90`, `if-no-files-found: error`) and passes it to `review-mine --stats`. The rewrite runs whatever happened to the measuring and the upload, but only when the app's and fleet's checkouts succeeded (`if: ${{ !cancelled() && steps.app.outcome == 'success' && steps.fleet.outcome == 'success' }}`): a failed measurement or upload still fails the job, but the report is rewritten anyway, its section saying this run's numbers couldn't be read and keeping last run's; a failed checkout leaves the last report untouched, since without the app's `AGENTS.md` and verification settings the rewrite would drop the gaps and the unlinked ticks. The output `loop-stats-url` is the artifact's link. It needs no new permission: `contents: read` (already there for `AGENTS.md`) covers the commits and file contents. In a dry run the summary and the artifact are still produced; only the issue is printed instead of written.

## Testing it

`tests/test_loop_stats.py` drives the command against the fake `gh` in `tests/fakes/security/`: fixture pull requests, comments, files, issues, commits and contents give the expected JSON for each group, zero denominators give `null` and `n/a`, an item that moves to `checked:` in the window counts once and one already checked (or new) doesn't, each failing part is `null` and named in `missing` while the others are still counted, the caps (3,000 pull requests, 100 skill commits, a commit's file list page by page) hold, and paths are defused in the Markdown; the hidden line round-trips and a malformed, misplaced or oversized one is ignored. `tests/test_review_mine.py` checks the section and the hidden line in the report, last run's numbers beside this run's and not beside a run over another window, an unreadable or truncated stats file (a missing group or nested field) keeping last run's while explicit `null`s are this run's, the section left out of the job summary, no section without `--stats`, and the workflow's steps, the rewrite running whatever happened to the measuring. The self-test runs `review-mining.yml` in dry run on fleet itself, and `review-mining-artifact` checks that the `loop-stats.json` artifact was uploaded.
