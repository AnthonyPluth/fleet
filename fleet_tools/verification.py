import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from fleet_tools import verification_guides
from fleet_tools.agent_review import defuse

HELP = ("the verification record in a pull request's description: `show` prints its dimensions, each verified or not, "
        "from the pull request (--pr N, through gh) or a file (--body-file PATH, - for stdin); `guides` lists the "
        "verification guides' areas and `areas PATH...` names the areas that cover those paths")

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


AMBIGUOUS = "ambiguous (indent, container or fence form)"


def fence_of(line: str) -> tuple[str, str] | None:
    if not line or line[0] not in "`~":
        return None
    end = 0
    while end < len(line) and line[end] == line[0]:
        end += 1
    return (line[:end], line[end:]) if end >= 3 else None


def closes(found: tuple[str, str], opener: tuple[str, str]) -> bool:
    return found[0][0] == opener[0][0] and len(found[0]) >= len(opener[0]) and not found[1].strip()


def has_fence_run(line: str) -> bool:
    return "```" in line or "~~~" in line


def is_row(line: str) -> bool:
    return CELL_SPLIT.search(line) is not None


def is_separator(line: str) -> bool:
    found = cells(line)
    return bool(found) and all(SEPARATOR_CELL.fullmatch(cell) for cell in found)


def reject_ambiguity(index: int, line: str) -> None:
    if "\t" in line and has_fence_run(line):
        raise ParseError(index, "ambiguous indentation: a tab on a line with a code fence; indent with spaces")
    at = 0
    while True:
        opened, closed = line.find("<!--", at), line.find("-->", at)
        if opened < 0 and closed < 0:
            return
        if closed >= 0 and (opened < 0 or closed < opened):
            raise ParseError(index, "multiline HTML comment: a comment closes here without opening on its line; the only comments "
                                    "the record allows span one line each")
        closed = line.find("-->", opened + 4)
        if closed < 0:
            raise ParseError(index, "multiline HTML comment: a comment opened here doesn't close on its line; the only comments "
                                    "the record allows span one line each")
        at = closed + 3


def marker_lines(lines: list[str]) -> tuple[list[int], list[int]]:
    begins: list[int] = []
    ends: list[int] = []
    for index, line in enumerate(lines, start=1):
        reject_ambiguity(index, line)
    fence: tuple[str, str] | None = None
    opened = 0
    for index, line in enumerate(lines, start=1):
        found = fence_of(line)
        if found is None and has_fence_run(line):
            raise ParseError(index, f"{AMBIGUOUS}: a line holds a run of three or more backticks or tildes that isn't a plain "
                                    "top-level code fence; such a run can appear only as a fence at the first column, outside "
                                    "lists and blockquotes, the closing one with nothing else on its line; elsewhere write it out "
                                    "in words, such as three backticks")
        if found:
            if found[0][0] == "`" and "`" in found[1]:
                raise ParseError(index, f"{AMBIGUOUS}: a backtick fence whose info string holds a backtick isn't a fence on "
                                        "GitHub; drop the backtick or use a tilde fence")
            if fence is None:
                fence, opened = found, index
            elif closes(found, fence):
                fence = None
            continue
        if fence:
            continue
        if line == BEGIN:
            begins.append(index)
        elif line == END:
            ends.append(index)
        elif BEGIN in line or END in line:
            raise ParseError(index, f"{AMBIGUOUS}: a marker that isn't alone at the start of its line")
    if fence is not None:
        raise ParseError(opened, "a code fence opened here is never closed, so the record can't be told from example text")
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
    text = line.rstrip()
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
    for number, line in table:
        if not line.startswith("|"):
            raise ParseError(number, f"{AMBIGUOUS}: table rows must start with |; a line between the markers that holds an "
                                     "unescaped | is a row, so write it from the first column with its leading pipe, or escape "
                                     "its pipes as \\|")
    if not table:
        raise ParseError(begin, f"the verification block has no table; its header is `{HEADER}`")
    number, header = table[0]
    if header.rstrip() != HEADER:
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
    verification_guides.configure(actions)


def run(args: argparse.Namespace) -> int:
    if args.action in verification_guides.ACTIONS:
        return verification_guides.run(args)
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
