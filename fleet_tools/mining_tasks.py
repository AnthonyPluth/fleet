import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleet_tools import review_mine as rm
from fleet_tools.agent_review import defuse

HELP = ('open one Agent task issue per row the owner ticked in the "Repeated review findings" report, once, and link the '
        'row to it')

Json = dict[str, Any]

MINED = re.compile(r"^<!-- mined-from row:(?P<id>[a-z0-9:-]+) -->$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LABEL_IN_ROW = re.compile(r"^\s*\*\*(?P<label>.*)\*\*\s*$")
CELL_SPLIT = re.compile(r"(?<!\\)\|")
MAX_TASKS = 20
MAX_CELL = 600
MAX_TITLE = 200
MAX_ROW = 400
MAX_EVENT = 1_000_000
FABLE = "Fable (`claude-fable-5-1`): it changes the reviewer's guidance, which reads untrusted pull request content."
OPUS = "Opus."
OPEN_PROPOSAL = ("a check on the merge path, or a check of the kind itself, that stops a finding of this kind from staying "
                 "open at merge")


@dataclass(frozen=True)
class Table:
    heading: str
    title: str
    model: str
    proposal: str = ""


TABLES = {
    rm.TABLE: Table("Repeated findings", 'Check for "{}"', OPUS),
    rm.DISMISSED_TABLE: Table("Often dismissed", 'Review guidance for "{}"', FABLE),
    rm.OPEN_TABLE: Table("Open at merge", 'Findings left open at merge: "{}"', OPUS, OPEN_PROPOSAL),
    rm.GAPS_TABLE: Table("Verification gaps", "Verification gap: {}", OPUS),
}


@dataclass(frozen=True)
class Task:
    id: str
    table: Table
    header: tuple[str, ...]
    cells: tuple[str, ...]

    def label(self) -> str:
        found = LABEL_IN_ROW.match(self.cells[0])
        return found.group("label") if found else self.cells[0]


def safe(text: str, limit: int = MAX_CELL) -> str:
    flat = text.replace("\r", " ").replace("\n", " ").replace("@​", "@")
    return defuse(flat, limit).replace("<", "&lt;")


def plain(text: str, limit: int = MAX_TITLE) -> str:
    flat = text.replace("\r", " ").replace("\n", " ").replace("@​", "@").replace("&lt;", "<").replace("\\|", "|")
    return defuse(flat.replace("**", "").strip(), limit)


def split_cells(line: str) -> tuple[str, ...]:
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") and not inner.endswith("\\|") else inner
    return tuple(part.strip() for part in CELL_SPLIT.split(inner))


def sections(body: str) -> dict[str, tuple[tuple[str, ...], list[tuple[str, ...]], list[str]]]:
    found: dict[str, tuple[tuple[str, ...], list[tuple[str, ...]], list[str]]] = {}
    heading = ""
    table: list[str] = []
    ids: list[str] = []

    def close() -> None:
        if heading and len(table) >= 2:
            found[heading] = (split_cells(table[0]), [split_cells(line) for line in table[2:]], list(ids))

    for raw in body.splitlines():
        line = raw.rstrip()
        if line.startswith("### "):
            close()
            heading, table, ids = line[4:].strip(), [], []
            continue
        if line.startswith("|"):
            table.append(line)
            continue
        row = rm.ROW.match(line)
        if row:
            ids.append(row.group("id"))
    close()
    return found


def tasks_in(body: str) -> dict[str, Task]:
    by_heading = {table.heading: (prefix, table) for prefix, table in TABLES.items()}
    found: dict[str, Task] = {}
    for heading, (header, rows, ids) in sections(body).items():
        if heading not in by_heading or len(rows) != len(ids):
            continue
        prefix, table = by_heading[heading]
        for row_id, cells in zip(ids, rows, strict=True):
            if row_id.split(":", 1)[0] == prefix and len(cells) == len(header) and cells[0]:
                found[row_id] = Task(row_id, table, header, cells)
    return found


def newly_ticked(before: str, after: str) -> list[str]:
    old = rm.ticks_of(before)
    new = rm.ticks_of(after)
    return [row_id for row_id, (ticked, link) in new.items()
            if ticked and not link and row_id in old and not old[row_id][0] and not old[row_id][1]]


def title(task: Task) -> str:
    return task.table.title.format(plain(task.label(), MAX_TITLE - 40))


def body(task: Task, repo: str, report: int) -> str:
    link = f"https://github.com/{repo}/issues/{report}"
    proposal = task.table.proposal or safe(task.cells[-1])
    facts = [f"- **{safe(name, 80)}**: {safe(value)}" for name, value in zip(task.header, task.cells, strict=True)]
    lines = [
        "## Context",
        "",
        f"A row of the \"{task.table.heading}\" table of the \"{rm.TITLE}\" report, [#{report}]({link}), that the owner ticked:",
        "",
        *facts,
        "",
        "## Scope",
        "",
        f"The row's proposal: {proposal}",
        "",
        "## Out of scope",
        "",
        "The report itself and its other rows; anything the proposal above doesn't name.",
        "",
        "## Acceptance criteria",
        "",
        "- [ ] The check, guidance or fake named in Scope exists and a test shows it catches the case.",
        "- [ ] `make check` passes.",
        "",
        "## Tests",
        "",
        "A test that fails before the change, on a case like the ones this row counts.",
        "",
        "## Depends on",
        "",
        "Nothing.",
        "",
        "## Model",
        "",
        task.table.model,
        "",
        f"Opened by fleet's mining-tasks.yml from a ticked row of #{report}; the owner edits this issue before an agent picks it up.",
        f"<!-- mined-from row:{task.id} -->",
    ]
    return "\n".join(lines) + "\n"


def linked(text: str, links: dict[str, int]) -> str:
    out: list[str] = []
    for line in text.split("\n"):
        ending = "\r" if line.endswith("\r") else ""
        core = line[:-1] if ending else line
        row = rm.ROW.match(core.rstrip())
        if row and row.group("id") in links and not row.group("issue"):
            rest = safe(row.group("rest"), MAX_ROW)
            core = f"- [{row.group('tick')}] <!-- row:{row.group('id')} -->{rest} → #{links[row.group('id')]}"
        out.append(core + ending)
    return "\n".join(out)


def mined(repo: str) -> dict[str, int]:
    found = rm.gh("--paginate", f"repos/{repo}/issues?state=all&labels={rm.LABEL}&per_page={rm.PER_PAGE}")
    markers: dict[str, int] = {}
    for issue in found if isinstance(found, list) else []:
        if not isinstance(issue, dict) or "pull_request" in issue or not isinstance(issue.get("number"), int):
            continue
        user = issue.get("user")
        if not isinstance(user, dict) or user.get("login") != rm.BOT:
            continue
        text = issue.get("body")
        for line in text.splitlines() if isinstance(text, str) else []:
            marker = MINED.match(line.strip())
            if marker:
                row_id = marker.group("id")
                markers[row_id] = min(markers.get(row_id, issue["number"]), issue["number"])
    return markers


def login(node: object) -> str:
    return str(node.get("login") or "") if isinstance(node, dict) else ""


def refusal(event: Json) -> str:
    issue = event.get("issue")
    if event.get("action") != "edited" or not isinstance(issue, dict):
        return "not an edit of an issue"
    if "pull_request" in issue:
        return "a pull request, not an issue"
    if login(issue.get("user")) != rm.BOT:
        return f"the issue wasn't opened by {rm.BOT}"
    labels = issue.get("labels")
    names = {label.get("name") for label in labels if isinstance(label, dict)} if isinstance(labels, list) else set()
    if rm.LABEL not in names:
        return f"the issue has no {rm.LABEL} label"
    repository = event.get("repository")
    owner = login(repository.get("owner")) if isinstance(repository, dict) else ""
    if not owner or login(event.get("sender")) != owner:
        return "the edit isn't the repository owner's"
    if not isinstance(issue.get("number"), int) or not isinstance(issue.get("body"), str):
        return "the issue has no number or body"
    if not issue["body"].startswith(rm.MARKER):
        return "the issue isn't the report"
    changes = event.get("changes")
    body_change = changes.get("body") if isinstance(changes, dict) else None
    if not isinstance(body_change, dict) or not isinstance(body_change.get("from"), str):
        return "the body didn't change"
    return ""


def act(event: Json, repo: str, dry_run: bool) -> int:
    reason = refusal(event)
    if reason:
        print(f"mining-tasks: nothing to do: {reason}")
        return 0
    issue = event["issue"]
    report: int = issue["number"]
    current: str = issue["body"]
    ticked = newly_ticked(event["changes"]["body"]["from"], current)
    if not ticked:
        print("mining-tasks: nothing to do: no row went from unticked to ticked")
        return 0
    if len(ticked) > MAX_TASKS:
        print(f"mining-tasks: {len(ticked)} rows ticked in one edit; acting on the first {MAX_TASKS}")
        ticked = ticked[:MAX_TASKS]
    if not dry_run:
        fresh = rm.gh(f"repos/{repo}/issues/{report}")
        current = str(fresh.get("body") or "") if isinstance(fresh, dict) else ""
        still = rm.ticks_of(current)
        ticked = [row_id for row_id in ticked if row_id in still and not still[row_id][1]]
    tasks = tasks_in(issue["body"])
    existing = mined(repo)
    links: dict[str, int] = {}
    planned: list[str] = []
    failed = 0
    for row_id in ticked:
        if row_id in existing:
            links[row_id] = existing[row_id]
            print(f"mining-tasks: row:{row_id} already has #{existing[row_id]}; linking it, opening nothing")
            continue
        task = tasks.get(row_id)
        if task is None:
            print(f"mining-tasks: row:{row_id} has no table row to read; skipped")
            continue
        payload = {"title": title(task), "body": body(task, repo, report), "labels": [rm.LABEL]}
        if dry_run:
            print(f"mining-tasks: (dry run) would open \"{payload['title']}\" for row:{row_id}:\n\n{payload['body']}")
            planned.append(row_id)
            continue
        try:
            created = rm.gh("-X", "POST", f"repos/{repo}/issues", "--input", "-", stdin=json.dumps(payload))
        except subprocess.CalledProcessError as error:
            print(f"mining-tasks: opening the issue for row:{row_id} failed ({error.returncode}): {error.stderr.strip()[:300]}",
                  file=sys.stderr)
            failed += 1
            continue
        links[row_id] = int(created["number"])
        print(f"mining-tasks: opened #{links[row_id]} for row:{row_id}")
    rewritten = linked(current, links)
    if dry_run:
        changed = [line for line in rewritten.splitlines() if line not in current.splitlines()]
        changed += [f"row:{row_id} → #<the new issue>" for row_id in planned]
        print("mining-tasks: (dry run) would link:" + "".join(f"\n{line}" for line in changed) if changed else
              "mining-tasks: (dry run) nothing to link")
        return 0
    if rewritten != current:
        rm.gh("-X", "PATCH", f"repos/{repo}/issues/{report}", "--input", "-", stdin=json.dumps({"body": rewritten}))
        print(f"mining-tasks: linked {len(links)} row(s) in #{report}")
    return 1 if failed else 0


def read_event(path: Path) -> Json | None:
    try:
        if path.stat().st_size > MAX_EVENT:
            return None
        event = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return event if isinstance(event, dict) else None


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--event", type=Path, required=True, help="the issues event payload ($GITHUB_EVENT_PATH)")
    parser.add_argument("--repo", help="owner/name (default: the event's repository)")
    parser.add_argument("--dry-run", action="store_true", help="print the issues and the links instead of writing them")


def run(args: argparse.Namespace) -> int:
    event = read_event(args.event)
    if event is None:
        print(f"mining-tasks: {args.event} isn't a readable JSON event", file=sys.stderr)
        return 2
    repository = event.get("repository")
    repo = args.repo or (str(repository.get("full_name") or "") if isinstance(repository, dict) else "")
    if not REPO.match(repo):
        print("mining-tasks: no owner/name repository (--repo, or the event's)", file=sys.stderr)
        return 2
    try:
        return act(event, repo, args.dry_run)
    except subprocess.CalledProcessError as e:
        print(f"mining-tasks: gh api failed ({e.returncode}): {e.stderr.strip()[:300]}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fleet mining-tasks", description=HELP)
    configure(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
