import argparse
import datetime
import json
import re
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fleet_tools import agents_md

HELP = ("fleet's release stamps: `prepare X.Y.Z` rewrites VERSION, CHANGELOG.md, every FLEET_REF, plugin.json, "
        "pyproject.toml, the snippets in docs/ (docs/adopting.md's and the others) and AGENTS.md's markers; `notes X.Y.Z` prints that CHANGELOG section")

VERSION_FILE = Path("VERSION")
CHANGELOG = Path("CHANGELOG.md")
PLUGIN = Path("plugins/fleet/.claude-plugin/plugin.json")
PYPROJECT = Path("pyproject.toml")
ADOPTING = Path("docs/adopting.md")
AGENTS = Path("AGENTS.md")
WORKFLOWS = Path(".github/workflows")
UNRELEASED = "## [Unreleased]"
PLACEHOLDER = "vX.Y.Z"
VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
HEADING = re.compile(r"^## .*$", re.MULTILINE)
RELEASED = re.compile(r"## \[(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\] - (?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
SNIPPET_VERSION = re.compile(r"(?P<head>uses: AnthonyPluth/fleet/\S+@\S+[ \t]+#[ \t]*|\"ref\":[ \t]*\"|fleet sync --to[ \t]+"
                             r"|fleet:begin [a-z0-9-]+ |AnthonyPluth/fleet[@/]|echo[ \t]+)(?P<tag>v(?:[0-9]+\.[0-9]+\.[0-9]+|X\.Y\.Z))\b")
FLEET_REF_LINE = re.compile(r"^(  FLEET_REF:[ \t]+)(\S+)", re.MULTILINE)
PLUGIN_VERSION = re.compile(r'^(\s*"version":\s*")([^"]*)(")', re.MULTILINE)
PYPROJECT_VERSION = re.compile(r'^(version\s*=\s*")([^"]*)(")', re.MULTILINE)


class ReleaseError(Exception):
    pass


@dataclass(frozen=True)
class Section:
    heading: str
    start: int
    body_start: int
    end: int


def is_date(text: str) -> bool:
    if not DATE_PATTERN.fullmatch(text):
        return False
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def parse_version(text: str) -> tuple[int, int, int]:
    if not VERSION_PATTERN.fullmatch(text):
        raise ReleaseError(f"{text!r} isn't a version (MAJOR.MINOR.PATCH, ASCII digits, no leading zeros)")
    major, minor, patch = (int(part) for part in text.split("."))
    return major, minor, patch


def sections(text: str) -> list[Section]:
    found = list(HEADING.finditer(text))
    return [Section(match.group(0).rstrip(), match.start(), match.end(),
                    found[index + 1].start() if index + 1 < len(found) else len(text))
            for index, match in enumerate(found)]


def released_heading(section: Section) -> re.Match[str] | None:
    return RELEASED.fullmatch(section.heading)


def changelog_errors(text: str, version: str) -> list[str]:
    name = CHANGELOG.as_posix()
    found = sections(text)
    if not found or found[0].heading != UNRELEASED:
        return [f"{name}: the first section must be `{UNRELEASED}`"]
    if sum(1 for section in found if section.heading == UNRELEASED) > 1:
        return [f"{name}: more than one `{UNRELEASED}` section"]
    if len(found) == 1:
        if parse_version(version)[0] >= 1:
            return [f"{name}: no `## [{version}] - YYYY-MM-DD` section for VERSION {version}"]
        return []
    top = released_heading(found[1])
    if top is None:
        return [f"{name}: the section after Unreleased, {found[1].heading!r}, isn't `## [X.Y.Z] - YYYY-MM-DD`"]
    if top.group("version") != version:
        return [f"{name}: the newest released section is {top.group('version')}, but VERSION is {version}"]
    if not is_date(top.group("date")):
        return [f"{name}: the section for {version} has the impossible date {top.group('date')}"]
    return []


def snippet_versions(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    fence = ""
    for number, line in enumerate(text.splitlines(), start=1):
        opener = FENCE.match(line)
        if fence:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence) and not line[opener.end():].strip():
                fence = ""
                continue
            found.extend((number, match.group("tag")) for match in SNIPPET_VERSION.finditer(line))
        elif opener:
            fence = opener.group(1)
    return found


def adopting_errors(text: str, version: str, name: Path = ADOPTING) -> list[str]:
    allowed = {f"v{version}"} | ({PLACEHOLDER} if version.startswith("0.") else set())
    return [f"{name.as_posix()}:{number}: a snippet says {found}, but VERSION is {version}"
            for number, found in snippet_versions(text) if found not in allowed]


def read(root: Path, path: Path) -> str | None:
    target = root / path
    return target.read_text(encoding="utf-8") if target.is_file() else None


def check(root: Path, version: str) -> list[str]:
    errors: list[str] = []
    changelog = read(root, CHANGELOG)
    if changelog is None:
        errors.append(f"{CHANGELOG.as_posix()}: missing")
    else:
        errors.extend(changelog_errors(changelog, version))
    plugin = read(root, PLUGIN)
    if plugin is None:
        errors.append(f"{PLUGIN.as_posix()}: missing")
    else:
        try:
            stamped = json.loads(plugin).get("version")
        except (json.JSONDecodeError, AttributeError):
            stamped = None
        if stamped != version:
            errors.append(f"{PLUGIN.as_posix()}: version is {stamped!r}, but VERSION is {version}")
    pyproject = read(root, PYPROJECT)
    if pyproject is None:
        errors.append(f"{PYPROJECT.as_posix()}: missing")
    else:
        try:
            project = tomllib.loads(pyproject).get("project", {})
        except tomllib.TOMLDecodeError:
            project = {}
        if project.get("version") != version:
            errors.append(f"{PYPROJECT.as_posix()}: [project] version is {project.get('version')!r}, but VERSION is {version}")
    if not (root / ADOPTING).is_file():
        errors.append(f"{ADOPTING.as_posix()}: missing")
    for doc in docs(root):
        errors.extend(adopting_errors((root / doc).read_text(encoding="utf-8"), version, doc))
    agents = read(root, AGENTS)
    if agents is not None:
        errors.extend(agents_errors(agents, version))
    return errors


def agents_errors(text: str, version: str) -> list[str]:
    try:
        found = agents_md.blocks(text)
    except agents_md.MarkerError as error:
        return [f"{AGENTS.as_posix()}: {error}"]
    return [f"{AGENTS.as_posix()}: the {block.name} block's marker says {block.version}, but VERSION is {version}"
            for block in found if block.version != f"v{version}"]


def docs(root: Path) -> list[Path]:
    return sorted(path.relative_to(root) for path in (root / ADOPTING.parent).glob("*.md") if path.is_file())


def notes(text: str, version: str) -> str:
    for section in sections(text):
        heading = released_heading(section)
        if heading and heading.group("version") == version:
            body = text[section.body_start:section.end].strip("\n")
            if not body.strip():
                raise ReleaseError(f"{CHANGELOG.as_posix()}: the section for {version} is empty")
            return body + "\n"
    raise ReleaseError(f"{CHANGELOG.as_posix()}: no `## [{version}] - YYYY-MM-DD` section")


def stamp_changelog(text: str, version: str, date: str) -> str:
    found = sections(text)
    if not found or found[0].heading != UNRELEASED:
        raise ReleaseError(f"{CHANGELOG.as_posix()}: the first section must be `{UNRELEASED}`")
    unreleased = found[0]
    if not re.search(r"^- ", text[unreleased.body_start:unreleased.end], re.MULTILINE):
        raise ReleaseError(f"{CHANGELOG.as_posix()}: the Unreleased section has no entries to release")
    if any((heading := released_heading(section)) and heading.group("version") == version for section in found):
        raise ReleaseError(f"{CHANGELOG.as_posix()}: there is already a section for {version}")
    return text[:unreleased.body_start] + f"\n\n## [{version}] - {date}" + text[unreleased.body_start:]


def stamp_one(pattern: re.Pattern[str], text: str, value: str, name: str) -> str:
    stamped, count = pattern.subn(lambda match: match.group(1) + value + match.group(3), text, count=1)
    if count != 1:
        raise ReleaseError(f"{name}: found no version to stamp")
    return stamped


def stamp_adopting(text: str, tag: str) -> str:
    numbers = {number for number, _ in snippet_versions(text)}
    lines = text.splitlines(keepends=True)
    for number in numbers:
        lines[number - 1] = SNIPPET_VERSION.sub(lambda match: match.group("head") + tag, lines[number - 1])
    return "".join(lines)


def reusable_workflows(root: Path) -> list[Path]:
    from fleet_tools import self_check

    directory = root / WORKFLOWS
    paths = sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")])
    return [path for path in paths if self_check.is_reusable(self_check.parse(path.read_text(encoding="utf-8")))]


def prepare(root: Path, version: str, date: str) -> list[str]:
    from fleet_tools import self_check

    new = parse_version(version)
    if new[0] < 1:
        raise ReleaseError(f"fleet releases start at 1.0.0; {version} is a pre-release version (docs/release.md)")
    current_text = read(root, VERSION_FILE)
    if current_text is None:
        raise ReleaseError("VERSION: missing")
    current = current_text.strip()
    if parse_version(current) >= new:
        raise ReleaseError(f"{version} isn't newer than VERSION {current}")
    if not is_date(date):
        raise ReleaseError(f"{date!r} isn't a date (YYYY-MM-DD)")
    before = self_check.check(root)
    if before:
        raise ReleaseError("the stamps disagree before preparing; fix these first:\n" + "\n".join(before))
    tag = f"v{version}"
    writes: dict[Path, str] = {VERSION_FILE: version + "\n"}
    stamps: list[tuple[Path, Callable[[str], str]]] = [
        (CHANGELOG, lambda text: stamp_changelog(text, version, date)),
        (PLUGIN, lambda text: stamp_one(PLUGIN_VERSION, text, version, PLUGIN.as_posix())),
        (PYPROJECT, lambda text: stamp_one(PYPROJECT_VERSION, text, version, PYPROJECT.as_posix())),
    ]
    stamps += [(doc, lambda text: stamp_adopting(text, tag)) for doc in docs(root)]
    for path, stamp in stamps:
        text = read(root, path)
        if text is None:
            raise ReleaseError(f"{path.as_posix()}: missing")
        writes[path] = stamp(text)
    for workflow in reusable_workflows(root):
        relative = workflow.relative_to(root)
        text = workflow.read_text(encoding="utf-8")
        if len(FLEET_REF_LINE.findall(text)) != 1:
            raise ReleaseError(f"{relative.as_posix()}: expected exactly one top-level `FLEET_REF:` line")
        writes[relative] = FLEET_REF_LINE.sub(lambda match: match.group(1) + tag, text)
    agents = read(root, AGENTS)
    if agents is not None:
        writes[AGENTS], _ = agents_md.sync(agents, agents_md.sections(root), tag)
    changed = []
    for path, text in writes.items():
        target = root / path
        if target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
            changed.append(path.as_posix())
    after = self_check.check(root)
    if after:
        raise ReleaseError("the stamps still disagree after preparing:\n" + "\n".join(after))
    return sorted(changed)


def configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", metavar="<action>", required=True)
    prepare_parser = actions.add_parser("prepare", help="stamp every version for the release X.Y.Z")
    prepare_parser.add_argument("version", metavar="X.Y.Z")
    prepare_parser.add_argument("--date", default=datetime.datetime.now(datetime.UTC).date().isoformat(),
                                help="the release date for CHANGELOG.md (default: today, UTC)")
    prepare_parser.add_argument("--root", type=Path, default=Path.cwd(), help="fleet's checkout (default: here)")
    notes_parser = actions.add_parser("notes", help="print CHANGELOG.md's section for X.Y.Z (the release notes)")
    notes_parser.add_argument("version", metavar="X.Y.Z")
    notes_parser.add_argument("--root", type=Path, default=Path.cwd(), help="fleet's checkout (default: here)")


def run(args: argparse.Namespace) -> int:
    root: Path = args.root
    try:
        parse_version(args.version)
        if args.action == "notes":
            text = read(root, CHANGELOG)
            if text is None:
                raise ReleaseError(f"{CHANGELOG.as_posix()}: missing")
            sys.stdout.write(notes(text, args.version))
            return 0
        changed = prepare(root, args.version, args.date)
    except (ReleaseError, agents_md.MarkerError) as error:
        print(f"fleet release: {error}", file=sys.stderr)
        return 1
    for path in changed:
        print(f"stamped {path}")
    print(f"fleet release: stamped v{args.version}. Next: `poetry run fleet sync --to v{args.version} --root <consumer>` "
          "for each tests/fixtures/consumer-* (and move their fleet pins' version comments), then `make check`, "
          "then open the release pull request (docs/release.md).")
    return 0
