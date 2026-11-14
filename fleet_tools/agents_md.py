import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from fleet_tools import identity

ROOT = identity.ROOT
SECTIONS = Path("agents-md/sections")
ORDER = ("starting-work", "working-in-a-container", "mistakes", "git-and-prs")
AGENTS = "AGENTS.md"
VERSION_FILE = Path(".fleet/version")
VERSION = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
COMMIT = identity.COMMIT
LAST_WITHOUT_COMMIT = "v1.0.0"
BEGIN = re.compile(r"<!-- fleet:begin (?P<name>[a-z0-9-]+) (?P<version>v\d+\.\d+\.\d+) -->")
END = re.compile(r"<!-- fleet:end (?P<name>[a-z0-9-]+) -->")
MARKER = re.compile(r"<!-- fleet:(?:begin|end)\b")
FRONT_MATTER = re.compile(r"\A---\n(?P<fields>(?:[a-z-]+: .*\n)+)---\n\n?")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^(#{1,2}) ")


class MarkerError(Exception):
    pass


@dataclass(frozen=True)
class Section:
    name: str
    heading: str
    body: str


@dataclass(frozen=True)
class Block:
    name: str
    version: str
    start: int
    end: int
    body_start: int
    body_end: int


@dataclass(frozen=True)
class Content:
    version: str
    commit: str | None


def version_key(version: str) -> tuple[int, int, int]:
    found = VERSION.fullmatch(version)
    if not found:
        raise ValueError(f"{version!r} isn't a version (vX.Y.Z)")
    return int(found.group(1)), int(found.group(2)), int(found.group(3))


def needs_commit(version: str) -> bool:
    return version_key(version) > version_key(LAST_WITHOUT_COMMIT)


def render_version_file(version: str, commit: str) -> str:
    return f"{version}\n{commit}\n"


def parse_version_file(text: str) -> Content:
    name = VERSION_FILE.as_posix()
    lines = text.removesuffix("\n").split("\n")
    if len(lines) > 2:
        raise ValueError(f"{name} has {len(lines)} lines; it holds the version (line 1) and the commit it is at (line 2)")
    version = lines[0].strip()
    if not VERSION.fullmatch(version):
        raise ValueError(f"{name} holds {version[:40]!r}, not a version (vX.Y.Z)")
    if len(lines) == 1:
        return Content(version, None)
    commit = lines[1].strip(" \t")
    if not COMMIT.fullmatch(commit):
        if COMMIT.fullmatch(commit.strip()):
            raise ValueError(f"{name} line 2 holds {lines[1]!r}: tools/fleet reads it with `read -r`, which strips only spaces and tabs, "
                             "so a carriage return or other whitespace around the commit makes it reject the line")
        raise ValueError(f"{name} line 2 holds {commit[:40]!r}, not a commit (40 hex digits)")
    return Content(version, commit)


def parse_section(name: str, text: str) -> Section:
    found = FRONT_MATTER.match(text)
    if not found:
        raise MarkerError(f"{SECTIONS / name}.md: no front matter naming its heading")
    fields = dict(line.split(": ", 1) for line in found.group("fields").splitlines())
    heading = fields.get("heading", "").strip()
    body = text[found.end():]
    if not heading:
        raise MarkerError(f"{SECTIONS / name}.md: its front matter has no `heading`")
    if not body.endswith("\n") or MARKER.search(body):
        raise MarkerError(f"{SECTIONS / name}.md: the body must end with a newline and hold no fleet marker")
    return Section(name, heading, body)


def sections(root: Path | None = None) -> dict[str, Section]:
    root = root or ROOT
    return {name: parse_section(name, (root / SECTIONS / f"{name}.md").read_text(encoding="utf-8")) for name in ORDER}


def tagged_sections(version: str, root: Path) -> dict[str, Section] | None:
    if not (root / ".git").exists():
        return None
    try:
        listing = subprocess.run(["git", "ls-tree", "--name-only", f"{version}:{SECTIONS.as_posix()}"], cwd=root,
                                 check=True, capture_output=True, text=True).stdout.split()
        found = {}
        for file in listing:
            if file.endswith(".md"):
                text = subprocess.run(["git", "show", f"{version}:{(SECTIONS / file).as_posix()}"], cwd=root,
                                      check=True, capture_output=True, text=True).stdout
                found[file[:-3]] = parse_section(file[:-3], text)
    except (subprocess.CalledProcessError, OSError):
        return None
    return found


def sections_at(version: str, own: bool, root: Path | None = None) -> dict[str, Section] | None:
    root = root or ROOT
    return sections(root) if own else tagged_sections(version, root)


def render(section: Section, version: str) -> str:
    return f"<!-- fleet:begin {section.name} {version} -->\n{section.body}<!-- fleet:end {section.name} -->"


def lines_with_offsets(text: str) -> list[tuple[int, str]]:
    offsets, position = [], 0
    for line in text.splitlines(keepends=True):
        offsets.append((position, line))
        position += len(line)
    return offsets


def blocks(text: str) -> list[Block]:
    found: list[Block] = []
    open_block: tuple[str, str, int, int] | None = None
    for offset, line in lines_with_offsets(text):
        content = line.rstrip("\n")
        begin, end = BEGIN.fullmatch(content), END.fullmatch(content)
        if begin:
            if open_block:
                raise MarkerError(f"{AGENTS}: `{content}` opens a block inside `{open_block[0]}`")
            if any(block.name == begin.group("name") for block in found):
                raise MarkerError(f"{AGENTS}: the {begin.group('name')} block appears twice")
            open_block = (begin.group("name"), begin.group("version"), offset, offset + len(line))
        elif end:
            if not open_block or open_block[0] != end.group("name"):
                raise MarkerError(f"{AGENTS}: `{content}` closes no open {end.group('name')} block")
            name, version, start, body_start = open_block
            found.append(Block(name, version, start, offset + len(content), body_start, offset))
            open_block = None
        elif MARKER.search(content):
            raise MarkerError(f"{AGENTS}: a malformed fleet marker: `{content.strip()[:80]}`")
    if open_block:
        raise MarkerError(f"{AGENTS}: the {open_block[0]} block is never closed (`<!-- fleet:end {open_block[0]} -->`)")
    return found


def outside(text: str) -> str:
    pieces, position = [], 0
    for block in blocks(text):
        pieces += [text[position:block.start], f"<!-- fleet block {block.name} -->"]
        position = block.end
    return "".join([*pieces, text[position:]])


def heading_end(text: str, heading: str) -> int | None:
    fence = ""
    inside, last = False, None
    for offset, line in lines_with_offsets(text):
        content = line.rstrip("\n")
        opened = FENCE.match(content)
        if fence:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence):
                fence = ""
            if inside:
                last = offset + len(line)
            continue
        if opened:
            fence = opened.group(1)
        if HEADING.match(content):
            if inside:
                return last
            inside = content.rstrip() == f"## {heading}"
            if inside:
                last = offset + len(line)
            continue
        if inside and content.strip():
            last = offset + len(line)
    return last if inside else None


def insert(text: str, section: Section, version: str) -> str:
    block = render(section, version)
    position = heading_end(text, section.heading)
    if position is None:
        lead = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
        return f"{text}{lead}## {section.heading}\n\n{block}\n" if text else f"## {section.heading}\n\n{block}\n"
    before, after = text[:position], text[position:]
    if not before.endswith("\n"):
        before += "\n"
    return f"{before}\n{block}\n{after}"


def sync(text: str, shipped: dict[str, Section], version: str) -> tuple[str, list[str]]:
    found = blocks(text)
    unknown = [block.name for block in found if block.name not in shipped]
    if unknown:
        raise MarkerError(f"{AGENTS}: fleet {version} ships no {', '.join(unknown)} block; remove it (see CHANGELOG.md)")
    changes = []
    for block in reversed(found):
        rendered = render(shipped[block.name], version)
        if text[block.start:block.end] != rendered:
            changes.append(f"replaced {block.name}")
        text = text[:block.start] + rendered + text[block.end:]
    changes.reverse()
    present = {block.name for block in found}
    for name, section in shipped.items():
        if name not in present:
            text = insert(text, section, version)
            changes.append(f"inserted {name} under \"## {section.heading}\"")
    return text, changes
