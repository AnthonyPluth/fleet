import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from fleet_tools.agent_review import defuse

HELP = ("the verification record in a pull request's description: `show` prints its dimensions, each verified or not, "
        "from the pull request (--pr N, through gh) or a file (--body-file PATH, - for stdin)")

BEGIN = "<!-- fleet-verification -->"
END = "<!-- /fleet-verification -->"
HEADER = "| Dimension | Status | How | Evidence |"
COLUMNS = ("dimension", "status", "how", "evidence")
STATUSES = ("verified", "not verified")
EMPTY = ("", "-", "—")
MAX_BODY = 65536
MAX_ROWS = 100
MAX_CELL = 1000
SEPARATOR_CELL = re.compile(r":?-+:?")
CONTAINER_PUNCTUATION = " \t>-*+0123456789.)"
MAX_CONTAINERS = 32
LIST_MARKER = re.compile(r"(?:[-*+]|[0-9]{1,9}[.)])[ \t]")
CELL_SPLIT = re.compile(r"(?<!\\)\|")


class ParseError(Exception):
    def __init__(self, line: int, rule: str) -> None:
        super().__init__(f"line {line}: {rule}")
        self.line = line
        self.rule = rule


@dataclass(frozen=True)
class Dimension:
    dimension: str
    status: str
    how: str
    evidence: str


@dataclass(frozen=True)
class Fence:
    marks: str
    rest: str
    depth: int
    listed: bool
    indent: int
    content: int
    ambiguous: bool = False

    def closes(self, opener: "Fence") -> bool:
        if self.ambiguous or opener.ambiguous:
            return False
        return (self.marks[0] == opener.marks[0] and len(self.marks) >= len(opener.marks) and not self.rest.strip()
                and self.depth == opener.depth and not self.listed and opener.content <= self.indent <= opener.content + 3)


def fence_run(line: str, at: int) -> tuple[str, str] | None:
    if at >= len(line) or line[at] not in "`~":
        return None
    end = at
    while end < len(line) and line[end] == line[at]:
        end += 1
    if end - at < 3:
        return None
    return line[at:end], line[end:]


def fence_of(line: str) -> Fence | None:
    at = depth = start = containers = 0
    listed = False
    while containers <= MAX_CONTAINERS:
        while at < len(line) and line[at] in " \t":
            at += 1
        if at < len(line) and line[at] == ">":
            at += 1
            depth += 1
            start = at
            containers += 1
            continue
        marker = LIST_MARKER.match(line, at)
        if marker:
            at = marker.end()
            listed = True
            containers += 1
            continue
        break
    indent = at - start
    found = fence_run(line, at) if containers <= MAX_CONTAINERS else None
    if found:
        return Fence(found[0], found[1], depth, listed, indent, at - start if listed else 0)
    stripped = line.lstrip(CONTAINER_PUNCTUATION)
    found = fence_run(line, len(line) - len(stripped))
    if found and len(stripped) < len(line):
        return Fence(found[0], found[1], depth, listed, indent, 0, ambiguous=True)
    return None


def prefixed_marker(line: str) -> bool:
    stripped = line.lstrip(CONTAINER_PUNCTUATION).strip()
    return stripped in (BEGIN, END) and line.strip() != stripped


def is_row(line: str) -> bool:
    return line.lstrip().startswith("|") or CELL_SPLIT.search(line) is not None


def is_separator(line: str) -> bool:
    found = cells(line)
    return bool(found) and all(SEPARATOR_CELL.fullmatch(cell) for cell in found)


def marker_lines(lines: list[str]) -> tuple[list[int], list[int]]:
    begins: list[int] = []
    ends: list[int] = []
    fence: Fence | None = None
    opened = 0
    for index, line in enumerate(lines, start=1):
        found = fence_of(line)
        if found:
            if fence is None:
                fence, opened = found, index
            elif found.closes(fence):
                fence = None
            continue
        if fence:
            continue
        if line.strip() == BEGIN:
            begins.append(index)
        elif line.strip() == END:
            ends.append(index)
        elif prefixed_marker(line):
            raise ParseError(index, "a marker line inside a list item or blockquote; the markers stand alone at the top level")
    if fence is not None:
        raise ParseError(opened, "a code fence opened here is never closed (a closing fence sits in the same list item or "
                                 "blockquote as the opening one), so the record can't be told from example text")
    return begins, ends


def block(lines: list[str]) -> tuple[int, int]:
    begins, ends = marker_lines(lines)
    if not begins:
        last = max((number for number, line in enumerate(lines, start=1) if line.strip()), default=1)
        raise ParseError(last, "no verification block: the `fleet-verification` and `/fleet-verification` marker lines "
                               "(HTML comments, each alone on its line) are missing")
    if len(begins) > 1:
        raise ParseError(begins[1], "a second verification block begins here; a description holds exactly one")
    begin = begins[0]
    after = [end for end in ends if end > begin]
    if not after:
        raise ParseError(begin, "the verification block that begins here has no closing `/fleet-verification` marker line")
    if len(ends) > 1:
        raise ParseError(ends[1] if ends[0] > begin else ends[0], "a stray closing marker; a description holds exactly one block")
    return begin, after[0]


def cells(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    return [cell.strip() for cell in CELL_SPLIT.split(text)]


def unescape(cell: str) -> str:
    return cell.replace("\\|", "|")


def is_empty(cell: str) -> bool:
    return cell.strip() in EMPTY


def parse_row(number: int, line: str) -> Dimension:
    found = cells(line)
    if len(found) != len(COLUMNS):
        raise ParseError(number, f"a row has {len(found)} cells, not {len(COLUMNS)} (Dimension, Status, How, Evidence; "
                                 "write a pipe inside a cell as \\|)")
    for cell in found:
        if len(cell) > MAX_CELL:
            raise ParseError(number, f"a cell is over {MAX_CELL:,} characters")
    dimension, status, how, evidence = (unescape(cell) for cell in found)
    if is_empty(dimension):
        raise ParseError(number, "the Dimension cell is empty")
    if status not in STATUSES:
        raise ParseError(number, "the Status cell isn't `verified` or `not verified`")
    if status == "verified" and (is_empty(how) or is_empty(evidence)):
        raise ParseError(number, "a `verified` row needs a non-empty How (the command, flow, test or manual step) and "
                                 "Evidence (a test name, a flow name, a screenshot path, a command's output summary)")
    if status == "not verified" and is_empty(how):
        raise ParseError(number, "a `not verified` row needs a non-empty How: why it couldn't be verified and what "
                                 "would make it verifiable")
    return Dimension(dimension, status, how, evidence)


def parse(body: str) -> list[Dimension]:
    if len(body) > MAX_BODY:
        raise ParseError(1, f"the description is over {MAX_BODY:,} characters")
    lines = body.replace("\r\n", "\n").split("\n")
    begin, end = block(lines)
    table = [(number, lines[number - 1]) for number in range(begin + 1, end) if is_row(lines[number - 1])]
    if not table:
        raise ParseError(begin, f"the verification block has no table; its header is `{HEADER}`")
    number, header = table[0]
    if header.strip() != HEADER:
        raise ParseError(number, f"the table's header isn't exactly `{HEADER}`")
    rows = table[1:]
    if rows and is_separator(rows[0][1]):
        rows = rows[1:]
    if len(rows) > MAX_ROWS:
        raise ParseError(rows[MAX_ROWS][0], f"the table has over {MAX_ROWS} rows")
    if not rows:
        raise ParseError(number, "the table has no rows; every dimension of the change is one, `verified` or `not verified`")
    return [parse_row(line_number, line) for line_number, line in rows]


def fetch_body(pr: int) -> str:
    done = subprocess.run(["gh", "api", f"repos/{{owner}}/{{repo}}/pulls/{int(pr)}", "--jq", ".body"], check=True,
                          capture_output=True, text=True)
    return done.stdout


def describe(dimensions: list[Dimension]) -> str:
    lines = []
    for each in dimensions:
        lines.append(f"{each.status:<12}  {defuse(each.dimension, 200)}")
        lines.append(f"              how: {defuse(each.how, 1000)}")
        if not is_empty(each.evidence):
            lines.append(f"              evidence: {defuse(each.evidence, 1000)}")
    return "".join(line.replace("\n", " ") + "\n" for line in lines)


def configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", metavar="<action>", required=True)
    show = actions.add_parser("show", help="print the record's dimensions", description="print the record's dimensions")
    source = show.add_mutually_exclusive_group(required=True)
    source.add_argument("--pr", type=int, metavar="N", help="read pull request N's description in the current repository (gh api)")
    source.add_argument("--body-file", metavar="PATH", help="read the description from this file (- for stdin)")
    show.add_argument("--json", action="store_true", help="print {\"pr\", \"dimensions\"} as JSON")


def run(args: argparse.Namespace) -> int:
    try:
        if args.pr is not None:
            body = fetch_body(args.pr)
        elif args.body_file == "-":
            body = sys.stdin.read()
        else:
            body = Path(args.body_file).read_text(encoding="utf-8")
        dimensions = parse(body)
    except subprocess.CalledProcessError as error:
        print(f"fleet verification: `{' '.join(map(str, error.cmd))}` failed: {(error.stderr or '').strip()[:300]}",
              file=sys.stderr)
        return 1
    except (OSError, UnicodeDecodeError) as error:
        print(f"fleet verification: can't read the description: {error}", file=sys.stderr)
        return 1
    except ParseError as error:
        print(f"fleet verification: the description's verification record doesn't parse, {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"pr": args.pr, "dimensions": [asdict(each) for each in dimensions]}, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(describe(dimensions))
    return 0
