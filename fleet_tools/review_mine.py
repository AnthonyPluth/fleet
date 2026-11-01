import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fleet_tools.agent_review import (
    FIRST,
    MAX_COMMENT,
    MAX_FINDINGS_LINE,
    SECOND,
    SEVERITIES,
    Review,
    decode_findings,
    defuse,
)

HELP = 'the "Repeated review findings" issue: review findings that came back on several pull requests, with the check each should become'

Json = dict[str, Any]

TITLE = "Repeated review findings"
LABEL = "review-mining"
BOT = "github-actions[bot]"
MARKER = "<!-- review-mining -->"
TABLE = "repeated"
TASK = "becomes an Agent task"
REVIEWS = (FIRST, SECOND)
FINDINGS_LINE = 2
MAX_FINDINGS = 50
MAX_PAGES = 30
PER_PAGE = 100
LIMITS = {"kind": 120, "title": 200, "file": 300, "check": 300}
MISTAKES_HEADING = "## Mistakes that keep coming back"
ITEM = re.compile(r"^- \*\*(?P<title>[^*]+)\*\*(?:\s*\((?P<state>[^)]*))?")
ROW = re.compile(r"^- \[(?P<tick>[ xX])\] <!-- row:(?P<id>[a-z0-9:-]+) -->(?P<rest>.*?)(?: → #(?P<issue>\d+))?$")
ISSUE_URL = re.compile(r"/issues/(\d+)$")
NOT_WORD = re.compile(r"[^a-z0-9]+")

CHECKED = "checked"
PARTLY = "partly checked"
HUMAN = "human-judged"
UNSTATED = "unstated"
UNLISTED = "not in AGENTS.md"
OTHER = "other"

USUAL_CHECKS = {
    "failure paths": "a test of what the user sees on the failing path",
    "access that outlives the person": "a test that the credential ends when its owner can no longer sign in",
    "undo and restore": "a round-trip test with awkward values",
    "github actions": "a `fleet checks` workflow rule, or a zizmor audit",
    "layout": "screenshots at phone and desktop widths from `make verify`",
}
DEFAULT_CHECK = "a test (or a lint rule or a type) that fails on the case"


@dataclass(frozen=True)
class Item:
    title: str
    state: str
    check: str


@dataclass
class Group:
    kind: str
    state: str
    pulls: set[int] = field(default_factory=set)
    last_seen: str = ""
    checks: Counter[str] = field(default_factory=Counter)
    existing_check: str = ""


@dataclass(frozen=True)
class Row:
    id: str
    kind: str
    state: str
    pulls: tuple[int, ...]
    last_seen: str
    check: str


def normalize(text: str) -> str:
    return NOT_WORD.sub(" ", text.lower()).strip()


def slug(text: str, limit: int = 60) -> str:
    return normalize(text).replace(" ", "-")[:limit].strip("-") or "untitled"


def gh(*args: str, stdin: str | None = None) -> Any:
    result = subprocess.run(["gh", "api", *args], capture_output=True, text=True, input=stdin, check=True)
    decoder = json.JSONDecoder()
    text = result.stdout.strip()
    pages: list[Any] = []
    index = 0
    while index < len(text):
        page, end = decoder.raw_decode(text, index)
        pages.append(page)
        index = end
        while index < len(text) and text[index].isspace():
            index += 1
    if len(pages) == 1:
        return pages[0]
    joined: list[Any] = []
    for page in pages:
        if isinstance(page, list):
            joined.extend(page)
    return joined


def mistakes(text: str) -> dict[str, Item]:
    items: dict[str, Item] = {}
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line.strip() == MISTAKES_HEADING
            continue
        found = ITEM.match(line) if inside else None
        if not found:
            continue
        title, paren = found.group("title").strip(), (found.group("state") or "").strip()
        lowered = paren.lower()
        state, check = UNSTATED, ""
        if lowered.startswith(PARTLY):
            state = PARTLY
        elif lowered.startswith(CHECKED):
            state = CHECKED
            check = paren.partition(":")[2].split(";", 1)[0].strip()
        elif lowered.startswith(HUMAN):
            state = HUMAN
        items[normalize(title)] = Item(title, state, check)
    return items


def read_mistakes(path: Path | None) -> dict[str, Item]:
    if path is None or not path.is_file():
        return {}
    return mistakes(path.read_text(encoding="utf-8"))


def clean(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or len(value) > limit:
        return None
    return defuse(value.replace("\r", " ").replace("\n", " "), limit)


def valid_finding(raw: Json) -> Json | None:
    if raw.get("severity") not in SEVERITIES:
        return None
    kind = clean(raw.get("kind"), LIMITS["kind"])
    title = clean(raw.get("title"), LIMITS["title"])
    if not kind or not kind.strip() or title is None:
        return None
    out = {"severity": raw["severity"], "kind": kind.strip(), "title": title, "file": "", "check": ""}
    for name in ("file", "check"):
        if raw.get(name) is None:
            continue
        value = clean(raw[name], LIMITS[name])
        if value is None:
            return None
        out[name] = value.strip()
    return out


def findings_of(comment: Json) -> tuple[Review, list[Json]] | None:
    user = comment.get("user")
    if not isinstance(user, dict) or user.get("login") != BOT:
        return None
    body = comment.get("body")
    if not isinstance(body, str) or len(body) > MAX_COMMENT:
        return None
    lines = body.splitlines()
    if len(lines) <= FINDINGS_LINE:
        return None
    for review in REVIEWS:
        if lines[0] != review.marker:
            continue
        line = lines[FINDINGS_LINE]
        if len(line.encode()) > MAX_FINDINGS_LINE:
            return None
        found = review.findings.match(line)
        if not found:
            return None
        decoded = decode_findings(found.group(1))
        if decoded is None:
            return None
        return review, [f for f in (valid_finding(item) for item in decoded[:MAX_FINDINGS]) if f is not None]
    return None


def issue_number(comment: Json) -> int | None:
    found = ISSUE_URL.search(str(comment.get("issue_url") or ""))
    return int(found.group(1)) if found else None


def stamp(comment: Json) -> str:
    value = comment.get("updated_at") or comment.get("created_at") or ""
    return value if isinstance(value, str) else ""


def newest_records(comments: list[Json], pulls: set[int]) -> dict[tuple[int, str], tuple[str, list[Json]]]:
    records: dict[tuple[int, str], tuple[str, list[Json]]] = {}
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        number = issue_number(comment)
        if number is None or number not in pulls:
            continue
        parsed = findings_of(comment)
        if parsed is None:
            continue
        review, findings = parsed
        key = (number, review.key)
        when = stamp(comment)
        if key not in records or when > records[key][0]:
            records[key] = (when, findings)
    return records


def group_key(finding: Json, items: dict[str, Item]) -> tuple[str, str, Group]:
    kind = normalize(finding["kind"])
    if kind == OTHER:
        title = normalize(finding["title"])
        return f"{TABLE}:{OTHER}:{slug(title)}", title, Group(f"other: {finding['title']}", UNLISTED)
    item = items.get(kind)
    if item is None:
        return f"{TABLE}:{slug(kind)}", kind, Group(finding["kind"], UNLISTED)
    return f"{TABLE}:{slug(item.title)}", kind, Group(item.title, item.state, existing_check=item.check)


def groups_of(records: dict[tuple[int, str], tuple[str, list[Json]]], items: dict[str, Item]) -> dict[str, Group]:
    groups: dict[str, Group] = {}
    for (number, _), (when, findings) in sorted(records.items()):
        for finding in findings:
            row_id, _, fresh = group_key(finding, items)
            group = groups.setdefault(row_id, fresh)
            group.pulls.add(number)
            group.last_seen = max(group.last_seen, when[:10])
            if finding["check"]:
                group.checks[finding["check"]] += 1
    return groups


def proposed_check(group: Group) -> str:
    if group.checks:
        return sorted(group.checks.items(), key=lambda item: (-item[1], item[0]))[0][0]
    if group.state == CHECKED:
        return f"extend `{group.existing_check}`: it missed these" if group.existing_check else "extend the existing check: it missed these"
    return USUAL_CHECKS.get(normalize(group.kind), DEFAULT_CHECK)


def listed(group: Group) -> bool:
    if group.state == CHECKED:
        return len(group.pulls) >= 1
    return len(group.pulls) >= 2


def rows_of(groups: dict[str, Group]) -> list[Row]:
    rows = [Row(row_id, group.kind, group.state, tuple(sorted(group.pulls)), group.last_seen, proposed_check(group))
            for row_id, group in groups.items() if listed(group)]
    return sorted(rows, key=lambda row: (-len(row.pulls), row.kind.lower(), row.id))


def cell(text: str, limit: int = 300) -> str:
    return defuse(text, limit).replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def ticks_of(body: str) -> dict[str, tuple[bool, str]]:
    kept: dict[str, tuple[bool, str]] = {}
    for line in body.splitlines():
        found = ROW.match(line.rstrip())
        if found:
            kept[found.group("id")] = (found.group("tick").lower() == "x", found.group("issue") or "")
    return kept


def render(rows: list[Row], repo: str, days: int, since: str, counts: tuple[int, int], previous: str = "",
           run_url: str = "") -> str:
    kept = ticks_of(previous)
    link = f"https://github.com/{repo}/pull/"
    lines = [
        MARKER,
        f"Review findings that came back on more than one pull request of `{repo}` updated in the last {days} days "
        f"(since {since[:10]}), read from the \"Agent review\" and \"Second review\" comments and counted per kind of "
        f"`AGENTS.md`'s \"Mistakes that keep coming back\". A kind is listed when it appears on two or more pull requests "
        f"and isn't fully checked, or when it is checked and still appears (the check misses a case). Each run rewrites "
        f"this issue; a row is closed by a check, not by a new sentence in `AGENTS.md`.",
        "",
    ]
    if not rows:
        lines.append("Nothing came back on two or more pull requests in the window.")
    else:
        lines += ["| Kind | State | Pull requests | Last seen | Proposed check |", "| --- | --- | --- | --- | --- |"]
        for row in rows:
            pulls = ", ".join(f"[#{n}]({link}{n})" for n in row.pulls)
            lines.append(f"| **{cell(row.kind, 160)}** | {row.state} | {pulls} | {row.last_seen or '?'} | {cell(row.check)} |")
        lines += ["", f"Tick a row and it {TASK}: the tick and the issue opened from it stay through each rewrite.", ""]
        for row in rows:
            ticked, issue = kept.get(row.id, (False, ""))
            line = f"- [{'x' if ticked else ' '}] <!-- row:{row.id} --> **{cell(row.kind, 160)}** {TASK}"
            lines.append(f"{line} → #{issue}" if issue else line)
    read, with_findings = counts
    about = f"{read} pull request(s) updated in the window, {with_findings} with review findings. From fleet's review-mining.yml"
    lines += ["", f"<sub>{about} ([run]({run_url})).</sub>" if run_url else f"<sub>{about}.</sub>"]
    return "\n".join(lines) + "\n"


def pulls_since(repo: str, since: str) -> list[int]:
    numbers: list[int] = []
    for page in range(1, MAX_PAGES + 1):
        batch = gh(f"repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page={PER_PAGE}&page={page}")
        if not isinstance(batch, list) or not batch:
            break
        for pull in batch:
            if isinstance(pull, dict) and isinstance(pull.get("number"), int) and str(pull.get("updated_at") or "") >= since:
                numbers.append(pull["number"])
        last = batch[-1]
        if len(batch) < PER_PAGE or not isinstance(last, dict) or str(last.get("updated_at") or "") < since:
            break
    return sorted(set(numbers))


def comments_since(repo: str, since: str) -> list[Json]:
    listed_comments = gh("--paginate", f"repos/{repo}/issues/comments?since={since}&per_page={PER_PAGE}")
    return [c for c in listed_comments if isinstance(c, dict)] if isinstance(listed_comments, list) else []


def report_issue(repo: str) -> Json | None:
    found = gh("--paginate", f"repos/{repo}/issues?state=open&labels={LABEL}&per_page={PER_PAGE}")
    candidates = [i for i in found if isinstance(i, dict) and "pull_request" not in i and i.get("title") == TITLE
                  and isinstance(i.get("number"), int)] if isinstance(found, list) else []
    return max(candidates, key=lambda i: int(i["number"])) if candidates else None


def window(now: datetime, days: int) -> str:
    return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_now(text: str | None) -> datetime:
    if not text:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def mine(repo: str, days: int, agents_md: Path | None, now: datetime, dry_run: bool, summary: Path | None) -> int:
    since = window(now, days)
    items = read_mistakes(agents_md)
    numbers = pulls_since(repo, since)
    records = newest_records(comments_since(repo, since), set(numbers))
    rows = rows_of(groups_of(records, items))
    existing = report_issue(repo)
    previous = str(existing.get("body") or "") if existing else ""
    body = render(rows, repo, days, since, (len(numbers), len({n for n, _ in records})), previous, os.environ.get("RUN_URL", ""))
    print(f"review-mine: {len(numbers)} pull request(s) updated since {since[:10]}, {len({n for n, _ in records})} with findings, {len(rows)} row(s)")
    if summary is not None:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(body.split("\n", 1)[1] + "\n")
    if dry_run:
        what = f"rewrite #{existing['number']}" if existing else ("open" if rows else "open nothing")
        print(f"review-mine: (dry run) would {what}:\n\n{body}")
        return 0
    if existing:
        gh("-X", "PATCH", f"repos/{repo}/issues/{existing['number']}", "--input", "-", stdin=json.dumps({"body": body}))
        print(f"review-mine: rewrote #{existing['number']}")
    elif rows:
        created = gh("-X", "POST", f"repos/{repo}/issues", "--input", "-",
                     stdin=json.dumps({"title": TITLE, "body": body, "labels": [LABEL]}))
        print(f"review-mine: opened #{created['number']}")
    else:
        print("review-mine: nothing repeated and no issue open; nothing to do")
    return 0


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--days", type=int, default=90, help="the window, in days before now (default 90)")
    parser.add_argument("--agents-md", type=Path, default=Path("AGENTS.md"),
                        help="the repository's AGENTS.md, for each kind's state (default: ./AGENTS.md; missing: every kind unlisted)")
    parser.add_argument("--summary", type=Path, help="a file to append the report to ($GITHUB_STEP_SUMMARY)")
    parser.add_argument("--dry-run", action="store_true", help="print the issue instead of opening or rewriting it")
    parser.add_argument("--now", help="the end of the window as an ISO 8601 time (tests; default: now)")


def run(args: argparse.Namespace) -> int:
    if args.days < 1:
        print("review-mine: --days must be at least 1", file=sys.stderr)
        return 2
    try:
        return mine(args.repo, args.days, args.agents_md, parse_now(args.now), args.dry_run, args.summary)
    except subprocess.CalledProcessError as e:
        print(f"review-mine: gh api failed ({e.returncode}): {e.stderr.strip()}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fleet review-mine", description=HELP)
    configure(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
