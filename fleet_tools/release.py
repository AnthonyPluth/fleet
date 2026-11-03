import argparse
import datetime
import json
import re
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fleet_tools import agents_md, sync, vendor

HELP = ("fleet's release stamps: `prepare X.Y.Z` rewrites VERSION, CHANGELOG.md, every FLEET_REF, plugin.json, "
        "pyproject.toml, the snippets in docs/ (docs/adopting.md's and the others) and AGENTS.md's markers, and re-syncs "
        "the fixture consumers and the drifted fixture; `notes X.Y.Z` prints that CHANGELOG section")

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
RELEASED = re.compile(r"## \[(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\] - (?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
SNIPPET_VERSION = re.compile(r"(?P<head>uses: AnthonyPluth/fleet/\S+@\S+[ \t]+#[ \t]*|\"AnthonyPluth/fleet\",[ \t]*\"ref\":[ \t]*\""
                             r"|fleet sync --to[ \t]+|fleet:begin [a-z0-9-]+[ \t]+)(?P<tag>[^\s\"/]*)")
FLEET_URL = re.compile(r"(?:github\.com/AnthonyPluth/fleet(?:\.git)?@|raw\.githubusercontent\.com/AnthonyPluth/fleet/)(?P<ref>[^\s\"'/]*)")
COMMIT_PLACEHOLDER = "<commit>"
FIXTURES = Path("tests/fixtures")
DRIFTED = FIXTURES / "drifted"
FIXTURE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
FIXTURE_PIN = re.compile(r"^([ \t-]*uses: AnthonyPluth/fleet/\S+@\S+[ \t]+#[ \t]*)v[0-9]+\.[0-9]+\.[0-9]+", re.MULTILINE)
DRIFTED_MARKER = re.compile(r"(<!-- fleet:begin [a-z0-9-]+ )v[0-9]+\.[0-9]+\.[0-9]+")
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


def read_version(root: Path) -> str:
    path = root / VERSION_FILE
    if not path.is_file():
        raise ReleaseError("VERSION: missing")
    raw = path.read_bytes()
    text = raw[:-1] if raw.endswith(b"\n") else raw
    try:
        version = text.decode("ascii")
    except UnicodeDecodeError:
        version = ""
    if not VERSION_PATTERN.fullmatch(version):
        raise ReleaseError(f"VERSION: {raw!r} isn't a version (MAJOR.MINOR.PATCH, ASCII digits, no leading zeros, "
                           "at most one newline after it and nothing else)")
    return version


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
    headings: list[tuple[str, int, int]] = []
    fence = ""
    fence_line = 0
    offset = 0
    for number, line in enumerate(text.splitlines(keepends=True), start=1):
        content = line.rstrip("\r\n")
        opener = FENCE.match(content)
        if fence:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence) and not content[opener.end():].strip():
                fence = ""
        elif opener:
            fence, fence_line = opener.group(1), number
        elif content.startswith("## "):
            headings.append((content.rstrip(), offset, offset + len(content)))
        offset += len(line)
    if fence:
        raise ReleaseError(f"{CHANGELOG.as_posix()}:{fence_line}: a code fence that is never closed")
    return [Section(heading, start, body_start, headings[index + 1][1] if index + 1 < len(headings) else len(text))
            for index, (heading, start, body_start) in enumerate(headings)]


def released_heading(section: Section) -> re.Match[str] | None:
    return RELEASED.fullmatch(section.heading)


def changelog_errors(text: str, version: str) -> list[str]:
    name = CHANGELOG.as_posix()
    try:
        found = sections(text)
    except ReleaseError as error:
        return [str(error)]
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


def fenced_lines(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    fence = ""
    for number, line in enumerate(text.splitlines(), start=1):
        opener = FENCE.match(line)
        if fence:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence) and not line[opener.end():].strip():
                fence = ""
                continue
            found.append((number, line))
        elif opener:
            fence = opener.group(1)
    return found


def snippet_versions(text: str) -> list[tuple[int, str]]:
    return [(number, match.group("tag")) for number, line in fenced_lines(text) for match in SNIPPET_VERSION.finditer(line)]


def adopting_errors(text: str, version: str, name: Path = ADOPTING) -> list[str]:
    allowed = {f"v{version}"} | ({PLACEHOLDER} if version.startswith("0.") else set())
    errors = [f"{name.as_posix()}:{number}: a snippet says {found}, but VERSION is {version}"
              for number, found in snippet_versions(text) if found not in allowed]
    errors += [f"{name.as_posix()}:{number}: a snippet fetches fleet at {match.group('ref')[:40]!r}, a ref that can move; "
               "name the commit (`@<commit>`)"
               for number, line in fenced_lines(text) for match in FLEET_URL.finditer(line)
               if match.group("ref") != COMMIT_PLACEHOLDER and not agents_md.COMMIT.fullmatch(match.group("ref"))]
    return errors


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
    if agents is None:
        errors.append(f"{AGENTS.as_posix()}: missing")
    else:
        errors.extend(agents_errors(agents, version))
    return errors


def agents_errors(text: str, version: str) -> list[str]:
    try:
        found = agents_md.blocks(text)
    except agents_md.MarkerError as error:
        return [f"{AGENTS.as_posix()}: {error}"]
    names = [block.name for block in found]
    errors = [f"{AGENTS.as_posix()}: the {name} block is missing; fleet ships it (agents-md/sections/{name}.md)"
              for name in agents_md.ORDER if name not in names]
    errors += [f"{AGENTS.as_posix()}: the {name} block isn't one fleet ships ({', '.join(agents_md.ORDER)})"
               for name in names if name not in agents_md.ORDER]
    errors += [f"{AGENTS.as_posix()}: the {block.name} block's marker says {block.version}, but VERSION is {version}"
               for block in found if block.version != f"v{version}"]
    return errors


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
    found = []
    for path in paths:
        try:
            workflow = self_check.parse(path.read_text(encoding="utf-8"))
        except self_check.Unreadable as error:
            raise ReleaseError(self_check.unreadable(path.relative_to(root).as_posix(), error)) from error
        if self_check.is_reusable(workflow):
            found.append(path)
    return found


def consumers(root: Path) -> list[Path]:
    return sorted(path for path in (root / FIXTURES).glob("consumer-*") if path.is_dir())


def stamp_fixtures(root: Path, tag: str) -> dict[Path, str]:
    writes: dict[Path, str] = {}
    for consumer in consumers(root):
        for workflow in sorted((consumer / WORKFLOWS).glob("*.y*ml")):
            text = workflow.read_text(encoding="utf-8")
            writes[workflow.relative_to(root)] = FIXTURE_PIN.sub(lambda match: match.group(1) + tag, text)
    drifted = root / DRIFTED
    if (drifted / AGENTS).is_file():
        text = (drifted / AGENTS).read_text(encoding="utf-8")
        writes[DRIFTED / AGENTS] = DRIFTED_MARKER.sub(lambda match: match.group(1) + tag, text)
        writes[DRIFTED / agents_md.VERSION_FILE] = agents_md.render_version_file(tag, FIXTURE_COMMIT)
    return writes


def prepare(root: Path, version: str, date: str) -> list[str]:
    from fleet_tools import self_check

    new = parse_version(version)
    if new[0] < 1:
        raise ReleaseError(f"fleet releases start at 1.0.0; {version} is a pre-release version (docs/release.md)")
    current = read_version(root)
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
    if agents is None:
        raise ReleaseError(f"{AGENTS.as_posix()}: missing")
    writes[AGENTS], _ = agents_md.sync(agents, agents_md.sections(root), tag)
    writes.update(stamp_fixtures(root, tag))
    snapshot = Snapshot(root, [*writes, *(path.relative_to(root) for consumer in consumers(root)
                                           for path in consumer.rglob("*"))])
    try:
        changed = []
        for path, text in writes.items():
            target = root / path
            if target.read_text(encoding="utf-8") != text:
                target.write_text(text, encoding="utf-8")
                changed.append(path.as_posix())
        for consumer in consumers(root):
            changed.extend(f"{consumer.relative_to(root).as_posix()}/{change.split(':', 1)[0]}"
                           for change in sync.sync(consumer, tag, root))
        after = self_check.check(root)
        if after:
            raise ReleaseError("the stamps still disagree after preparing:\n" + "\n".join(after))
    except BaseException:
        snapshot.restore()
        raise
    return sorted(changed)


class Snapshot:
    def __init__(self, root: Path, paths: list[Path]) -> None:
        self.root = root
        self.files: dict[Path, tuple[bytes, int]] = {}
        self.directories: set[Path] = set()
        for path in paths:
            target = root / path
            if target.is_file() and not target.is_symlink():
                self.files[path] = (target.read_bytes(), target.stat().st_mode)
            elif target.is_dir():
                self.directories.add(path)
        self.consumers = [consumer.relative_to(root) for consumer in consumers(root)]

    def restore(self) -> None:
        for consumer in self.consumers:
            for target in sorted((self.root / consumer).rglob("*"), reverse=True):
                relative = target.relative_to(self.root)
                if target.is_file() and relative not in self.files:
                    target.unlink()
                elif target.is_dir() and relative not in self.directories and not any(target.iterdir()):
                    target.rmdir()
        for path, (data, mode) in self.files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or target.read_bytes() != data:
                target.write_bytes(data)
            if target.stat().st_mode != mode:
                target.chmod(mode)


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
    except (ReleaseError, agents_md.MarkerError, vendor.VendorError) as error:
        print(f"fleet release: {error}", file=sys.stderr)
        return 1
    for path in changed:
        print(f"stamped {path}")
    print(f"fleet release: stamped v{args.version}. Next: `make check`, then open the release pull request "
          "(docs/release.md).")
    return 0
