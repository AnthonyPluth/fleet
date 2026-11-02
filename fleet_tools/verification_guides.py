import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from fleet_tools import config

KEY = "verification"
ACTIONS = ("guides", "areas")
SECTIONS = ("Paths", "How to verify", "Not verifiable yet")
NOTHING = ("Nothing", "Nothing.")
MAX_GUIDE = 65536
MAX_GUIDES = 200
MAX_ITEMS = 200
MAX_STDIN = 16 * 1024 * 1024
TITLE = re.compile(r"# (?P<area>\S.*)")
HEADING = re.compile(r"## (?P<name>.*)")
ITEM = re.compile(r"[-*] (?P<text>.*\S)")
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
FENCE = ("```", "~~~")
NOT_GLOB = re.compile(r"[\s\\\[\]{}]")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
LAYOUT = ("a guide is `# <Area>` on line 1, then `## Paths`, `## How to verify` and `## Not verifiable yet`, "
          "in that order, each a list")


class GuideError(Exception):
    def __init__(self, source: str, line: int | None, rule: str) -> None:
        super().__init__(f"{source}, line {line}: {rule}" if line else f"{source}: {rule}")
        self.source = source
        self.line = line
        self.rule = rule


@dataclass(frozen=True)
class Glob:
    text: str
    line: int
    pattern: re.Pattern[str]

    def matches(self, path: str) -> bool:
        return self.pattern.fullmatch(path) is not None


@dataclass(frozen=True)
class Guide:
    name: str
    area: str
    source: str
    paths: tuple[Glob, ...]
    steps: tuple[str, ...]
    gaps: tuple[str, ...]

    def covers(self, path: str) -> bool:
        return any(glob.matches(path) for glob in self.paths)

    def as_json(self) -> dict[str, object]:
        return {"name": self.name, "area": self.area, "guide": self.source, "paths": [glob.text for glob in self.paths],
                "how_to_verify": list(self.steps), "not_verifiable_yet": list(self.gaps)}


def translate(text: str) -> re.Pattern[str]:
    parts = text.split("/")
    out = []
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "**":
            out.append(".+" if last else "(?:[^/]+/)*")
            continue
        piece = "".join("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char) for char in part)
        out.append(piece if last else piece + "/")
    return re.compile("".join(out), re.DOTALL)


def glob_of(source: str, line: int, item: str) -> Glob:
    text = item[1:-1] if len(item) > 2 and item[0] == item[-1] == "`" else item
    if not text or NOT_GLOB.search(text) or "`" in text:
        raise GuideError(source, line, "a path is one glob, optionally in backticks: no spaces, backslashes, brackets or braces")
    if text.startswith(("/", "./", "!")) or text.endswith("/"):
        raise GuideError(source, line, "a glob is relative to the repository root: no leading `/`, `./` or `!`, and a directory "
                                       "is written `<dir>/**`")
    segments = text.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise GuideError(source, line, "a glob has an empty, `.` or `..` segment")
    if any("**" in segment and segment != "**" for segment in segments):
        raise GuideError(source, line, "`**` stands alone as a whole segment (`src/**`, `**/test_*.py`)")
    return Glob(text, line, translate(text))


def sections(source: str, lines: list[str]) -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
    current: str | None = None
    for number, raw in enumerate(lines[1:], start=2):
        line = raw.rstrip()
        if line.lstrip().startswith(FENCE):
            raise GuideError(source, number, "a code fence; write a command inline, in backticks, in its list item")
        if line.startswith("#"):
            heading = HEADING.fullmatch(line)
            name = heading.group("name").strip() if heading else ""
            if name not in SECTIONS:
                raise GuideError(source, number, f"an unknown heading; {LAYOUT}")
            if name in found:
                raise GuideError(source, number, f"a second `## {name}` section")
            expected = SECTIONS[len(found)]
            if name != expected:
                raise GuideError(source, number, f"`## {name}` comes before `## {expected}`; {LAYOUT}")
            found[name] = []
            current = name
            continue
        if current is None or not line.strip():
            continue
        items = found[current]
        item = ITEM.fullmatch(line)
        if item:
            if len(items) >= MAX_ITEMS:
                raise GuideError(source, number, f"`## {current}` has over {MAX_ITEMS} items")
            items.append((number, item.group("text").strip()))
        elif raw[:1] in (" ", "\t") and items:
            items[-1] = (items[-1][0], f"{items[-1][1]} {line.strip()}")
        else:
            raise GuideError(source, number, f"a line in `## {current}` that isn't a list item (`- ...`) or an indented "
                                             "continuation of one")
    last = len(lines)
    for name in SECTIONS:
        if name not in found:
            raise GuideError(source, last, f"no `## {name}` section; {LAYOUT}")
    return found


def parse_guide(text: str, source: str, name: str) -> Guide:
    if len(text) > MAX_GUIDE:
        raise GuideError(source, None, f"the guide is over {MAX_GUIDE:,} characters")
    lines = text.replace("\r\n", "\n").split("\n")
    title = TITLE.fullmatch(lines[0].rstrip())
    if not title:
        raise GuideError(source, 1, "line 1 isn't the area's title, `# <Area>`")
    found = sections(source, lines)
    paths, steps, gaps = (found[section] for section in SECTIONS)
    last = len(lines)
    if not paths:
        raise GuideError(source, last, "`## Paths` lists no globs; a guide covers at least one path")
    if not steps:
        raise GuideError(source, last, "`## How to verify` lists no steps; each names a real command, flow, fake or test")
    if not gaps:
        raise GuideError(source, last, "`## Not verifiable yet` is empty; list each gap with why and what would change it, "
                                       "or the single item `Nothing`")
    nothing = [number for number, item in gaps if item in NOTHING]
    if nothing and len(gaps) > 1:
        raise GuideError(source, nothing[0], "`Nothing` stands alone under `## Not verifiable yet`")
    return Guide(name, title.group("area").strip(), source, tuple(glob_of(source, number, item) for number, item in paths),
                 tuple(item for _, item in steps), () if nothing else tuple(item for _, item in gaps))


def directory(conf: config.Config) -> str | None:
    value = conf.section(KEY).get("guides")
    if value is None:
        return None
    if isinstance(value, str) and value.strip() and "\\" not in value:
        path = PurePosixPath(value)
        if not path.is_absolute() and ".." not in path.parts and path.as_posix() != ".":
            return path.as_posix()
    raise config.ConfigError(f"[tool.fleet.{KEY}] guides must be a directory's path relative to the repository root, below it (not `.`, `..` or absolute)")


def load(root: Path, relative: str) -> tuple[list[Guide], list[GuideError]]:
    base = root / relative
    inside = base.resolve().is_relative_to(root.resolve())
    if not inside or base.is_symlink() or not base.is_dir():
        return [], [GuideError(relative, None, "the guides directory ([tool.fleet.verification] guides) doesn't exist inside "
                                               "the repository")]
    files = sorted(base.glob("*.md"))
    if not files:
        return [], [GuideError(relative, None, "the guides directory holds no guides (`<area>.md`)")]
    if len(files) > MAX_GUIDES:
        return [], [GuideError(relative, None, f"the guides directory holds over {MAX_GUIDES} guides")]
    guides: list[Guide] = []
    errors: list[GuideError] = []
    for path in files:
        source = f"{relative}/{path.name}" if relative != "." else path.name
        try:
            if not NAME.fullmatch(path.stem):
                raise GuideError(source, None, "a guide's file name is its area's name: lowercase letters, digits and `-`, then `.md`")
            if path.is_symlink() or not path.is_file():
                raise GuideError(source, None, "a guide is a regular file, not a link or a directory")
            data = path.read_bytes()
            if len(data) > MAX_GUIDE:
                raise GuideError(source, None, f"the guide is over {MAX_GUIDE:,} bytes")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise GuideError(source, None, "the guide isn't UTF-8") from None
            guides.append(parse_guide(text, source, path.stem))
        except GuideError as error:
            errors.append(error)
        except OSError as error:
            errors.append(GuideError(source, None, f"can't be read ({type(error).__name__})"))
    return guides, errors


def tracked(root: Path) -> list[str]:
    done = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True, text=True)
    return [path for path in done.stdout.split("\0") if path]


def normalize(path: str) -> str:
    while path.startswith("./"):
        path = path[2:]
    return path


def areas(guides: list[Guide], paths: list[str]) -> tuple[list[tuple[Guide, list[str]]], list[str]]:
    wanted = list(dict.fromkeys(normalize(path) for path in paths))
    matched = [(guide, [path for path in wanted if guide.covers(path)]) for guide in guides]
    covered = {path for _, found in matched for path in found}
    return [(guide, found) for guide, found in matched if found], [path for path in wanted if path not in covered]


def clean(text: str) -> str:
    return CONTROL.sub("?", text)


def heading(guide: Guide) -> str:
    return f"{clean(guide.name)}  {clean(guide.area)}  ({clean(guide.source)})\n"


def describe_guides(guides: list[Guide]) -> str:
    return "".join(heading(guide) + "".join(f"    {clean(glob.text)}\n" for glob in guide.paths) for guide in guides)


def describe_areas(found: list[tuple[Guide, list[str]]], uncovered: list[str]) -> str:
    text = "".join(heading(guide) + "".join(f"    {clean(path)}\n" for path in paths) for guide, paths in found)
    if uncovered:
        text += "no guide covers:\n" + "".join(f"    {clean(path)}\n" for path in uncovered)
    return text


def configure(actions: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    listing = actions.add_parser("guides", help="list the verification guides' areas and their globs",
                                 description="list the areas in the guides directory ([tool.fleet.verification] guides) "
                                             "with their globs")
    matching = actions.add_parser("areas", help="name the areas whose guides cover these paths",
                                  description="name the areas whose globs match the given paths (pass both names of a "
                                              "renamed file); paths no guide covers are listed after them")
    matching.add_argument("paths", nargs="*", metavar="PATH", help="a path relative to the repository root")
    matching.add_argument("--stdin0", action="store_true", help="also read NUL-separated paths from stdin (`git diff -z "
                          f"--name-only`), up to {MAX_STDIN // 1024 // 1024} MiB; one run, one result, whatever the count")
    for parser in (listing, matching):
        parser.add_argument("--root", type=Path, help="the repository (default: the nearest pyproject.toml up from here)")
        parser.add_argument("--json", action="store_true", help="print JSON")


def stdin_paths() -> list[str]:
    data = sys.stdin.buffer.read(MAX_STDIN + 1)
    if len(data) > MAX_STDIN:
        raise config.ConfigError(f"--stdin0: the paths on stdin are over {MAX_STDIN:,} bytes")
    return [path for path in data.decode("utf-8", errors="replace").split("\0") if path]


def run(args: argparse.Namespace) -> int:
    try:
        if args.action == "areas" and args.stdin0:
            args.paths = [*args.paths, *stdin_paths()]
        elif args.action == "areas" and not args.paths:
            raise config.ConfigError("areas needs a PATH, or --stdin0 with the paths on stdin")
        root = (args.root or config.find_root(Path.cwd())).resolve()
        relative = directory(config.load(root))
        if relative is None:
            raise config.ConfigError(f"[tool.fleet.{KEY}] guides isn't set, so the repository has no verification guides")
    except config.ConfigError as error:
        print(f"fleet verification: {error}", file=sys.stderr)
        return 2
    guides, errors = load(root, relative)
    for found_error in errors:
        print(f"fleet verification: {clean(str(found_error))}", file=sys.stderr)
    if errors:
        return 1
    if args.action == "guides":
        if args.json:
            print(json.dumps({"guides": relative, "areas": [guide.as_json() for guide in guides]}, indent=2, ensure_ascii=False))
        else:
            sys.stdout.write(describe_guides(guides))
        return 0
    found, uncovered = areas(guides, args.paths)
    if args.json:
        print(json.dumps({"areas": [{**guide.as_json(), "matched": paths} for guide, paths in found], "uncovered": uncovered},
                         indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(describe_areas(found, uncovered))
    return 0
