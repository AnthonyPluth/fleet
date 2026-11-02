import argparse
import hashlib
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

from fleet_tools import config, review_outcomes, verification, verification_guides
from fleet_tools.agent_review import (
    FINDING_ID,
    FIRST,
    MAX_COMMENT,
    MAX_FINDINGS_LINE,
    NO_BLOCK,
    SECOND,
    SEVERITIES,
    Review,
    decode_findings,
    defuse,
)

HELP = ('the "Repeated review findings" issue: review findings that came back on several pull requests, with the check each '
        'should become, the kinds mostly dismissed, the findings open at merge and the areas that keep going unverified')

Json = dict[str, Any]

TITLE = "Repeated review findings"
LABEL = "review-mining"
BOT = "github-actions[bot]"
MARKER = "<!-- review-mining -->"
TABLE = "repeated"
DISMISSED_TABLE = "dismissed"
OPEN_TABLE = "open-at-merge"
GAPS_TABLE = "gaps"
UNGROUPED = "ungrouped"
MIN_DISMISSED = 3
MIN_GAP_PULLS = 2
MAX_SAMPLES = 3
MAX_REASON = 120
MAX_FILES = 3000
MAX_COMMENT_PAGES = 10
GUIDANCE = ("Review guidance: say when this isn't a finding, in the app's `.github/agent-review/context.md` or in fleet's "
            "`prompt.md` if it holds for every app")
GAP_PROPOSAL = "a fake, flow or test that makes this verifiable, or an update to the area's guide"
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
MINED = re.compile(r"^<!-- mined-from row:(?P<id>[a-z0-9:-]+) -->$")
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
    fixed: int = 0


@dataclass(frozen=True)
class Row:
    id: str
    kind: str
    state: str
    pulls: tuple[int, ...]
    last_seen: str
    check: str
    fixed: int = 0


@dataclass(frozen=True)
class Pull:
    number: int
    merged: bool
    body: str
    merged_at: str = ""


@dataclass
class Tally:
    label: str
    raised: int = 0
    fixed: int = 0
    dismissed: int = 0
    open: int = 0
    pulls: set[int] = field(default_factory=set)
    reasons: list[tuple[str, int, str]] = field(default_factory=list)


@dataclass(frozen=True)
class Signal:
    id: str
    label: str
    cells: tuple[str, ...]


@dataclass
class Gaps:
    required: bool
    rows: list[Signal] = field(default_factory=list)
    without_record: int = 0
    unparsed: int = 0
    unread_files: int = 0


@dataclass
class Signals:
    dismissed: list[Signal] = field(default_factory=list)
    open_at_merge: list[Signal] = field(default_factory=list)
    gaps: Gaps | None = None
    unread_outcomes: int = 0
    edited_after_merge: int = 0

    def count(self) -> int:
        return len(self.dismissed) + len(self.open_at_merge) + (len(self.gaps.rows) if self.gaps else 0)


def normalize(text: str) -> str:
    return NOT_WORD.sub(" ", text.lower()).strip()


def slug(text: str, limit: int = 60) -> str:
    full = normalize(text).replace(" ", "-") or "untitled"
    if len(full) <= limit:
        return full
    digest = hashlib.sha256(full.encode()).hexdigest()[:12]
    return f"{full[:limit - len(digest) - 1].strip('-')}-{digest}"


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


def mined(repo: str) -> dict[str, int]:
    found = gh("--paginate", f"repos/{repo}/issues?state=all&labels={LABEL}&per_page={PER_PAGE}")
    markers: dict[str, int] = {}
    for issue in found if isinstance(found, list) else []:
        if not isinstance(issue, dict) or "pull_request" in issue or not isinstance(issue.get("number"), int):
            continue
        user = issue.get("user")
        if not isinstance(user, dict) or user.get("login") != BOT:
            continue
        text = issue.get("body")
        for line in text.splitlines() if isinstance(text, str) else []:
            marker = MINED.match(line.strip())
            if marker:
                row_id = marker.group("id")
                markers[row_id] = min(markers.get(row_id, issue["number"]), issue["number"])
    return markers


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
    out = {"severity": raw["severity"], "kind": kind.strip(), "title": title, "file": "", "check": "",
           "id": raw["id"] if isinstance(raw.get("id"), str) and FINDING_ID.match(raw["id"]) else ""}
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


def classify(kind_text: str, title_text: str, items: dict[str, Item]) -> tuple[str, str, Item | None]:
    kind = normalize(kind_text)
    if kind == OTHER:
        return f"{OTHER}:{slug(normalize(title_text))}", f"other: {title_text}", None
    item = items.get(kind)
    if item is None:
        return slug(kind), kind_text, None
    return slug(item.title), item.title, item


def group_key(finding: Json, items: dict[str, Item]) -> tuple[str, str, Group]:
    key, label, item = classify(finding["kind"], finding["title"], items)
    group = Group(label, UNLISTED) if item is None else Group(item.title, item.state, existing_check=item.check)
    return f"{TABLE}:{key}", normalize(finding["kind"]), group


Outcomes = dict[tuple[int, str], list[Json]]


def counted(findings: list[Json], entries: list[Json]) -> list[tuple[Json, bool]]:
    current = {f["id"]: f for f in findings if f.get("id")}
    seen: set[str] = set()
    kept: list[tuple[Json, bool]] = []
    for entry in entries:
        seen.add(str(entry["id"]))
        if entry["status"] == "dismissed":
            continue
        found = current.get(str(entry["id"]))
        if found is None:
            kind, title = str(entry.get("kind") or "").strip(), str(entry.get("title") or "")
            if not kind:
                continue
            found = {"kind": kind, "title": title, "check": ""}
        kept.append((found, entry["status"] == "resolved"))
    kept += [(f, False) for f in findings if not f.get("id") or f["id"] not in seen]
    return kept


def groups_of(records: dict[tuple[int, str], tuple[str, list[Json]]], items: dict[str, Item],
              outcomes: Outcomes | None = None) -> dict[str, Group]:
    groups: dict[str, Group] = {}
    history = outcomes or {}
    for key in sorted(set(records) | set(history)):
        when, findings = records.get(key, ("", []))
        for finding, fixed in counted(findings, history.get(key, [])):
            row_id, _, fresh = group_key(finding, items)
            group = groups.setdefault(row_id, fresh)
            group.pulls.add(key[0])
            group.last_seen = max(group.last_seen, when[:10])
            if fixed:
                group.fixed += 1
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
    rows = [Row(row_id, group.kind, group.state, tuple(sorted(group.pulls)), group.last_seen, proposed_check(group), group.fixed)
            for row_id, group in groups.items() if listed(group)]
    return sorted(rows, key=lambda row: (-len(row.pulls), row.kind.lower(), row.id))


def cell(text: str, limit: int = 300) -> str:
    flat = str(text).replace("\r", " ").replace("\n", " ").replace("@\u200b", "@")
    return defuse(flat, limit).replace("<", "&lt;").replace("|", "\\|")


def pull_links(repo: str, numbers: set[int] | tuple[int, ...]) -> str:
    return ", ".join(f"[#{n}](https://github.com/{repo}/pull/{n})" for n in sorted(numbers))


def comment_link(repo: str, url: str) -> str:
    if review_outcomes.LINK.match(url) and url.startswith(f"https://github.com/{repo}/"):
        return url
    return ""


def shaped(comments: list[Json]) -> list[Json]:
    def login(comment: Json) -> object:
        user = comment.get("user")
        return user.get("login") if isinstance(user, dict) else None

    bots = sorted((c for c in comments if login(c) == BOT), key=stamp, reverse=True)
    others = sorted((c for c in comments if login(c) != BOT), key=lambda c: str(c.get("created_at") or ""))
    return [{"body": c.get("body"), "user": login(c), "association": c.get("author_association"), "url": c.get("html_url")}
            for c in [*bots, *others]]


def pull_comments(repo: str, number: int) -> list[Json] | None:
    found: list[Json] = []
    for page in range(1, MAX_COMMENT_PAGES + 2):
        try:
            batch = gh(f"repos/{repo}/issues/{number}/comments?per_page={PER_PAGE}&page={page}")
        except subprocess.CalledProcessError:
            return None
        if not isinstance(batch, list):
            return None
        if page > MAX_COMMENT_PAGES:
            return None if batch else found
        found += [c for c in batch if isinstance(c, dict)]
        if len(batch) < PER_PAGE:
            return found
    return None


def histories(repo: str, numbers: set[int]) -> tuple[dict[int, list[Json]], int]:
    per_pull: dict[int, list[Json]] = {}
    unread = 0
    for number in sorted(numbers):
        history = pull_comments(repo, number)
        if history is None:
            unread += 1
        else:
            per_pull[number] = history
    return per_pull, unread


def bears_outcomes(comment: Json) -> bool:
    user = comment.get("user")
    raw = comment.get("body")
    body = raw if isinstance(raw, str) else ""
    if isinstance(user, dict) and user.get("login") == BOT:
        return any(body.startswith(review.marker) for review in REVIEWS)
    return comment.get("author_association") in review_outcomes.DISMISSERS or "/fleet dismiss" in body


def created(comment: Json) -> str:
    return str(comment.get("created_at") or "~")


def indeterminate(comment: Json, when: str) -> bool:
    changed = str(comment.get("updated_at") or comment.get("created_at") or "~")
    return bears_outcomes(comment) and (created(comment) >= when or changed >= when)


def at_merge(per_pull: dict[int, list[Json]], merged_at: dict[int, str]) -> tuple[dict[int, list[Json]], int]:
    kept: dict[int, list[Json]] = {}
    skipped = 0
    for number, comments in per_pull.items():
        when = merged_at.get(number)
        if not when:
            continue
        before = [c for c in comments if created(c) <= when]
        if any(indeterminate(c, when) for c in before):
            skipped += 1
            continue
        kept[number] = before
    return kept, skipped


def outcomes_from(repo: str, per_pull: dict[int, list[Json]]) -> dict[tuple[int, str], list[Json]]:
    found: dict[tuple[int, str], list[Json]] = {}
    for number, listed_comments in sorted(per_pull.items()):
        ready = shaped(listed_comments)
        for review in REVIEWS:
            entries = review_outcomes.load(repo, number, review.marker, ready)
            if entries:
                found[(number, review.key)] = entries
    return found


def tallies(outcomes: dict[tuple[int, str], list[Json]], items: dict[str, Item],
            only: set[int] | None = None) -> dict[str, Tally]:
    found: dict[str, Tally] = {}
    for (number, _), entries in sorted(outcomes.items()):
        if only is not None and number not in only:
            continue
        for entry in entries:
            kind, title = str(entry.get("kind") or "").strip(), str(entry.get("title") or "")
            if not kind:
                continue
            key, label, _ = classify(kind, title, items)
            tally = found.setdefault(key, Tally(label))
            tally.raised += 1
            tally.pulls.add(number)
            if entry["status"] == "resolved":
                tally.fixed += 1
            elif entry["status"] == "open":
                tally.open += 1
            elif entry["status"] == "dismissed":
                tally.dismissed += 1
                tally.reasons.append((str(entry.get("reason") or ""), number, str(entry.get("url") or "")))
    return found


def often_dismissed(repo: str, found: dict[str, Tally]) -> list[Signal]:
    rows = []
    for key, tally in found.items():
        if tally.dismissed < MIN_DISMISSED or tally.dismissed * 2 < tally.raised:
            continue
        reasons = []
        for reason, number, url in tally.reasons[:MAX_SAMPLES]:
            link = comment_link(repo, url)
            where = f"[#{number}]({link})" if link else f"#{number}"
            reasons.append(f"\"{cell(reason, MAX_REASON)}\" ({where})")
        rows.append(Signal(f"{DISMISSED_TABLE}:{key}", tally.label,
                           (str(tally.raised), str(tally.fixed), str(tally.dismissed), "; ".join(reasons), GUIDANCE)))
    return sorted(rows, key=lambda row: (-int(row.cells[2]), row.label.lower(), row.id))


def open_at_merge(repo: str, found: dict[str, Tally]) -> list[Signal]:
    rows = [Signal(f"{OPEN_TABLE}:{key}", tally.label, (str(tally.open), pull_links(repo, tally.pulls)))
            for key, tally in found.items() if tally.open]
    return sorted(rows, key=lambda row: (-int(row.cells[0]), row.label.lower(), row.id))


def open_tallies(outcomes: dict[tuple[int, str], list[Json]], items: dict[str, Item], merged: set[int]) -> dict[str, Tally]:
    kept = {key: [e for e in entries if e["status"] == "open"] for key, entries in outcomes.items() if key[0] in merged}
    return tallies({key: entries for key, entries in kept.items() if entries}, items)


def verification_settings(root: Path | None) -> tuple[bool, list[verification_guides.Guide]] | None:
    if root is None:
        return None
    try:
        conf = config.load(root)
        if not conf.has(verification_guides.KEY):
            return None
        required = conf.section(verification_guides.KEY).get("required", False) is True
        relative = verification_guides.directory(conf)
    except config.ConfigError as error:
        print(f"review-mine: no verification gaps: {verification_guides.clean(str(error))[:300]}", file=sys.stderr)
        return None
    if relative is None:
        return required, []
    guides, errors = verification_guides.load(root, relative)
    for skipped in errors[:20]:
        print(f"review-mine: a verification guide is skipped: {verification_guides.clean(str(skipped))[:300]}", file=sys.stderr)
    return required, guides


def changed_files(repo: str, number: int) -> list[str]:
    listed_files = gh("--paginate", f"repos/{repo}/pulls/{number}/files?per_page={PER_PAGE}")
    paths: list[str] = []
    for item in listed_files[:MAX_FILES] if isinstance(listed_files, list) else []:
        if not isinstance(item, dict):
            continue
        for name in ("filename", "previous_filename"):
            value = item.get(name)
            if isinstance(value, str) and value:
                paths.append(value)
    return paths


def gap_areas(guides: list[verification_guides.Guide], paths: list[str]) -> list[tuple[str, str]]:
    found, _ = verification_guides.areas(guides, paths)
    return [(guide.name, guide.area) for guide, _ in found] or [(UNGROUPED, UNGROUPED)]


def verification_gaps(repo: str, pulls: list[Pull], required: bool, guides: list[verification_guides.Guide]) -> Gaps:
    gaps = Gaps(required)
    areas: dict[str, Tally] = {}
    samples: dict[str, list[str]] = {}
    for each in pulls:
        if not each.merged:
            continue
        try:
            verification.block(each.body.replace("\r\n", "\n").split("\n"))
            dimensions = verification.parse(each.body)
        except verification.ParseError as error:
            if error.rule.startswith(NO_BLOCK):
                gaps.without_record += 1
            else:
                gaps.unparsed += 1
            continue
        unverified = [d for d in dimensions if d.status == "not verified"]
        if not unverified:
            continue
        try:
            paths = changed_files(repo, each.number)
        except subprocess.CalledProcessError:
            gaps.unread_files += 1
            continue
        for key, label in gap_areas(guides, paths):
            tally = areas.setdefault(key, Tally(label))
            tally.pulls.add(each.number)
            kept = samples.setdefault(key, [])
            for dimension in unverified:
                if len(kept) < MAX_SAMPLES:
                    kept.append(f"{cell(dimension.dimension, MAX_REASON)}: {cell(dimension.how, MAX_REASON)} (#{each.number})")
    listed_areas = sorted(((key, tally) for key, tally in areas.items() if len(tally.pulls) >= MIN_GAP_PULLS),
                          key=lambda item: (-len(item[1].pulls), item[1].label.lower(), item[0]))
    gaps.rows = [Signal(f"{GAPS_TABLE}:{key}", tally.label, (pull_links(repo, tally.pulls), "; ".join(samples[key]), GAP_PROPOSAL))
                 for key, tally in listed_areas]
    return gaps


def ticks_of(body: str) -> dict[str, tuple[bool, str]]:
    kept: dict[str, tuple[bool, str]] = {}
    for line in body.splitlines():
        found = ROW.match(line.rstrip())
        if found:
            kept[found.group("id")] = (found.group("tick").lower() == "x", found.group("issue") or "")
    return kept


def tasks(rows: list[tuple[str, str]], kept: dict[str, tuple[bool, str]]) -> list[str]:
    lines = [""]
    for row_id, label in rows:
        ticked, issue = kept.get(row_id, (False, ""))
        line = f"- [{'x' if ticked else ' '}] <!-- row:{row_id} --> **{cell(label, 160)}** {TASK}"
        lines.append(f"{line} → #{issue}" if issue else line)
    return lines


def signal_table(heading: str, about: str, header: tuple[str, ...], rows: list[Signal], empty: str,
                 kept: dict[str, tuple[bool, str]]) -> list[str]:
    lines = ["", f"### {heading}", "", about, ""]
    if not rows:
        return [*lines, empty]
    lines += ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    for row in rows:
        lines.append(f"| **{cell(row.label, 160)}** | " + " | ".join(row.cells) + " |")
    return lines + tasks([(row.id, row.label) for row in rows], kept)


def render_signals(signals: Signals, kept: dict[str, tuple[bool, str]]) -> list[str]:
    lines = signal_table(
        "Often dismissed",
        f"Kinds with at least {MIN_DISMISSED} findings dismissed by the owner in the window, dismissals being at least half "
        "of the kind's findings: the reviewer's guidance needs to say when this isn't a finding.",
        ("Kind", "Raised", "Fixed", "Dismissed", "Dismissal reasons", "Proposal"), signals.dismissed,
        "No kind was mostly dismissed in the window.", kept)
    lines += signal_table(
        "Open at merge",
        "Findings still open when their pull request merged in the window, per kind: the merge path let them through.",
        ("Kind", "Open at merge", "Pull requests"), signals.open_at_merge, "No finding was open at merge in the window.", kept)
    if signals.edited_after_merge:
        lines += ["", f"{signals.edited_after_merge} merged pull request(s) whose review or dismissal comments were posted or "
                      "changed at or after the merge instant were skipped for Open at merge."]
    if signals.unread_outcomes:
        lines += ["", f"{signals.unread_outcomes} pull request(s) whose comments couldn't be read in full (an error, or over "
                      f"{MAX_COMMENT_PAGES * PER_PAGE} comments) were skipped in these two tables; their findings still count "
                      "as repeats, without their outcomes."]
    gaps = signals.gaps
    if gaps is None:
        return lines
    lines += signal_table(
        "Verification gaps",
        f"`not verified` rows of the verification records of the pull requests merged in the window, per area of the "
        f"verification guides that cover the changed files (`{UNGROUPED}` when none does), when the area has gaps on "
        f"{MIN_GAP_PULLS} or more pull requests.",
        ("Area", "Pull requests", "Samples", "Proposal"), gaps.rows, f"No area had gaps on {MIN_GAP_PULLS} or more pull requests.", kept)
    notes = []
    if not gaps.required:
        notes.append(f"{gaps.without_record} merged pull request(s) without a verification record (it isn't required).")
    if gaps.unparsed:
        notes.append(f"{gaps.unparsed} verification record(s) that don't parse were skipped.")
    if gaps.unread_files:
        notes.append(f"{gaps.unread_files} merged pull request(s) whose changed files couldn't be read were skipped.")
    return lines + (["", " ".join(notes)] if notes else [])


def render(rows: list[Row], repo: str, days: int, since: str, counts: tuple[int, int], previous: str = "",
           run_url: str = "", signals: Signals | None = None, markers: dict[str, int] | None = None) -> str:
    kept = ticks_of(previous)
    for row_id, number in (markers or {}).items():
        if not kept.get(row_id, (False, ""))[1]:
            kept[row_id] = (True, str(number))
    lines = [
        MARKER,
        f"Review findings that came back on more than one pull request of `{repo}` updated in the last {days} days "
        f"(since {since[:10]}), read from the \"Agent review\" and \"Second review\" comments and counted per kind of "
        f"`AGENTS.md`'s \"Mistakes that keep coming back\". A kind is listed when it appears on two or more pull requests "
        f"and isn't fully checked, or when it is checked and still appears (the check misses a case); dismissed findings "
        f"don't count. Each run rewrites this issue; a row is closed by a check, not by a new sentence in `AGENTS.md`. "
        f"Tick a row and it {TASK}: the tick and the issue opened from it stay through each rewrite.",
        "",
        "### Repeated findings",
        "",
    ]
    if not rows:
        lines.append("Nothing came back on two or more pull requests in the window.")
    else:
        lines += ["| Kind | State | Pull requests | Fixed | Last seen | Proposed check |", "| --- | --- | --- | --- | --- | --- |"]
        for row in rows:
            lines.append(f"| **{cell(row.kind, 160)}** | {row.state} | {pull_links(repo, row.pulls)} | {row.fixed} | "
                         f"{row.last_seen or '?'} | {cell(row.check)} |")
        lines += tasks([(row.id, row.kind) for row in rows], kept)
    if signals is not None:
        lines += render_signals(signals, kept)
    read, with_findings = counts
    about = f"{read} pull request(s) updated in the window, {with_findings} with review findings. From fleet's review-mining.yml"
    lines += ["", f"<sub>{about} ([run]({run_url})).</sub>" if run_url else f"<sub>{about}.</sub>"]
    return "\n".join(lines) + "\n"


def pulls_since(repo: str, since: str) -> list[Pull]:
    found: dict[int, Pull] = {}
    for page in range(1, MAX_PAGES + 1):
        batch = gh(f"repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page={PER_PAGE}&page={page}")
        if not isinstance(batch, list) or not batch:
            break
        for pull in batch:
            if isinstance(pull, dict) and isinstance(pull.get("number"), int) and str(pull.get("updated_at") or "") >= since:
                merged_at = pull.get("merged_at")
                body = pull.get("body")
                in_window = isinstance(merged_at, str) and merged_at >= since
                found.setdefault(pull["number"], Pull(pull["number"], in_window, body if isinstance(body, str) else "",
                                                      str(merged_at) if in_window else ""))
        last = batch[-1]
        if len(batch) < PER_PAGE or not isinstance(last, dict) or str(last.get("updated_at") or "") < since:
            break
    return [found[n] for n in sorted(found)]


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


def mine(repo: str, days: int, agents_md: Path | None, now: datetime, dry_run: bool, summary: Path | None,
         app_root: Path | None = None) -> int:
    since = window(now, days)
    items = read_mistakes(agents_md)
    pulls = pulls_since(repo, since)
    numbers = [each.number for each in pulls]
    merged = {each.number: each.merged_at for each in pulls if each.merged}
    comments = comments_since(repo, since)
    records = newest_records(comments, set(numbers))
    per_pull, unread = histories(repo, {n for n, _ in records} | set(merged))
    outcomes = {key: entries for key, entries in outcomes_from(repo, per_pull).items() if key in records}
    snapshot, edited = at_merge(per_pull, merged)
    merge_time = outcomes_from(repo, snapshot)
    rows = rows_of(groups_of(records, items, outcomes))
    signals = Signals(often_dismissed(repo, tallies(outcomes, items)), open_at_merge(repo, open_tallies(merge_time, items, set(merged))),
                      unread_outcomes=unread, edited_after_merge=edited)
    settings = verification_settings(app_root)
    if settings is not None:
        signals.gaps = verification_gaps(repo, pulls, *settings)
    markers = mined(repo)
    existing = report_issue(repo)
    previous = str(existing.get("body") or "") if existing else ""
    body = render(rows, repo, days, since, (len(numbers), len({n for n, _ in records})), previous, os.environ.get("RUN_URL", ""), signals,
                  markers)
    rows_count = len(rows) + signals.count()
    print(f"review-mine: {len(numbers)} pull request(s) updated since {since[:10]}, {len({n for n, _ in records})} with findings, "
          f"{len(merged)} merged, {rows_count} row(s)")
    if summary is not None:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(body.split("\n", 1)[1] + "\n")
    if dry_run:
        what = f"rewrite #{existing['number']}" if existing else ("open" if rows_count else "open nothing")
        print(f"review-mine: (dry run) would {what}:\n\n{body}")
        return 0
    if existing:
        gh("-X", "PATCH", f"repos/{repo}/issues/{existing['number']}", "--input", "-", stdin=json.dumps({"body": body}))
        print(f"review-mine: rewrote #{existing['number']}")
    elif rows_count:
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
    parser.add_argument("--app-root", type=Path,
                        help="a checkout of the repository's default branch, for [tool.fleet.verification] and its guides "
                             "(default: none, so no verification gaps)")
    parser.add_argument("--summary", type=Path, help="a file to append the report to ($GITHUB_STEP_SUMMARY)")
    parser.add_argument("--dry-run", action="store_true", help="print the issue instead of opening or rewriting it")
    parser.add_argument("--now", help="the end of the window as an ISO 8601 time (tests; default: now)")


def run(args: argparse.Namespace) -> int:
    if args.days < 1:
        print("review-mine: --days must be at least 1", file=sys.stderr)
        return 2
    try:
        return mine(args.repo, args.days, args.agents_md, parse_now(args.now), args.dry_run, args.summary, args.app_root)
    except subprocess.CalledProcessError as e:
        print(f"review-mine: gh api failed ({e.returncode}): {e.stderr.strip()}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fleet review-mine", description=HELP)
    configure(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
