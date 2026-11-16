import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from fleet_tools import agents_md

HELP = ("fleet's changelog and release notes: `notes X.Y.Z --since vW --at REF` prints the notes of the release made "
        "from REF, the changelog fragments (changelog.d/<name>.md) its tree adds to vW's, as release.yml publishes each "
        "merge to main; `notes X.Y.Z` alone prints an older release's (changelog.d/released/X.Y.Z.md up to 5.0.0, "
        "CHANGELOG.md's section up to 4.0.0); `check --base REF` checks that a pull request adds a changelog fragment that "
        "parses and changes no merged one, and with --title that the title agrees with it: breaking exactly when it is "
        "`kind: breaking`, and `feat:` (or breaking) when it is `kind: added`")

VERSION_FILE = Path("VERSION")
CHANGELOG = Path("CHANGELOG.md")
FRAGMENTS = Path("changelog.d")
RELEASE_NOTES = FRAGMENTS / "released"
FROZEN = "4.0.0"
FOLDED = "5.0.0"
UNVERSIONED = "0.0.0"
KINDS = ("breaking", "added", "changed", "fixed")
GROUPS = (("Added", ("added",)), ("Changed", ("breaking", "changed")), ("Fixed", ("fixed",)))
BREAKING = "**Breaking:** "
FRAGMENT_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.md")
FRAGMENT_FORMAT = f"`---`, `kind: <{'|'.join(KINDS)}>`, `---`, then the entry on one line"
MARKUP_START = re.compile(r"(?:[-*+]|[0-9]+[.)]|#{1,6})(?:\s|$)")
PLUGIN = Path("plugins/fleet/.claude-plugin/plugin.json")
PYPROJECT = Path("pyproject.toml")
ADOPTING = Path("docs/adopting.md")
AGENTS = Path("AGENTS.md")
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
FIXTURE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
FIXTURE_VERSION = f"v{FOLDED}"
TITLE_MARKER = re.compile(r"(?:^|[ \t])#(?:major|minor)[ \t]*$")


class ReleaseError(Exception):
    pass


@dataclass(frozen=True)
class Section:
    heading: str
    start: int
    body_start: int
    end: int


@dataclass(frozen=True)
class Fragment:
    path: Path
    kind: str
    entry: str


@dataclass(frozen=True)
class Notes:
    version: str
    date: str
    body: str
    source: str


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


def version_key(text: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in text.split("."))
    return major, minor, patch


def sections(text: str, name: str = CHANGELOG.as_posix()) -> list[Section]:
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
        raise ReleaseError(f"{name}:{fence_line}: a code fence that is never closed")
    return [Section(heading, start, body_start, headings[index + 1][1] if index + 1 < len(headings) else len(text))
            for index, (heading, start, body_start) in enumerate(headings)]


def released_heading(section: Section) -> re.Match[str] | None:
    return RELEASED.fullmatch(section.heading)


def frozen(text: str) -> tuple[str | None, list[str]]:
    name = CHANGELOG.as_posix()
    try:
        found = sections(text)
    except ReleaseError as error:
        return None, [str(error)]
    if any(section.heading == UNRELEASED for section in found):
        return None, [f"{name}: it has an `{UNRELEASED}` section, but it is frozen at {FROZEN}: a change's entry is a "
                      f"fragment, {FRAGMENTS.as_posix()}/<name>.md (docs/release.md)"]
    if not found:
        return None, []
    top = released_heading(found[0])
    if top is None:
        return None, [f"{name}: its first section, {found[0].heading!r}, isn't `## [X.Y.Z] - YYYY-MM-DD`"]
    newest = top.group("version")
    if version_key(newest) > version_key(FROZEN):
        return None, [f"{name}: it is frozen at {FROZEN}, but its newest section is {newest}: a later release's notes are "
                      f"the changelog fragments it adds, {FRAGMENTS.as_posix()}/<name>.md (docs/release.md)"]
    if not is_date(top.group("date")):
        return None, [f"{name}: the section for {newest} has the impossible date {top.group('date')}"]
    return newest, []


def changelog_errors(text: str) -> list[str]:
    return frozen(text)[1]


def parse_fragment(path: Path, text: str) -> Fragment:
    name = path.as_posix()
    if "\r" in text:
        raise ReleaseError(f"{name}: has a carriage return; save it with Unix line endings")
    lines = text.split("\n")
    if lines[0] != "---":
        raise ReleaseError(f"{name}: a fragment is {FRAGMENT_FORMAT}")
    if "---" not in lines[1:]:
        raise ReleaseError(f"{name}: its front matter never closes (a second `---`)")
    close = lines.index("---", 1)
    fields = lines[1:close]
    if len(fields) != 1 or not fields[0].startswith("kind: "):
        raise ReleaseError(f"{name}: its front matter is one line, `kind: <{'|'.join(KINDS)}>`")
    kind = fields[0].removeprefix("kind: ").strip()
    if kind not in KINDS:
        raise ReleaseError(f"{name}: kind {kind[:40]!r} isn't one of {', '.join(KINDS)}")
    body = [line.rstrip() for line in lines[close + 1:] if line.strip()]
    if not body:
        raise ReleaseError(f"{name}: has no entry after its front matter")
    if len(body) > 1:
        raise ReleaseError(f"{name}: its entry is one line, as CHANGELOG.md's are (don't wrap it)")
    entry = body[0]
    if entry[0].isspace():
        raise ReleaseError(f"{name}: its entry starts with a space")
    if MARKUP_START.match(entry):
        raise ReleaseError(f"{name}: its entry is the text alone, with no list marker or heading (folding adds the `- `)")
    if entry.startswith("**Breaking"):
        raise ReleaseError(f"{name}: its entry starts with **Breaking; `kind: breaking` adds `{BREAKING.strip()}` when "
                           "it's folded")
    return Fragment(path, kind, entry)


def fragments(root: Path) -> tuple[list[Fragment], list[str]]:
    directory = root / FRAGMENTS
    found: list[Fragment] = []
    errors: list[str] = []
    if not directory.exists() and not directory.is_symlink():
        return found, errors
    if directory.is_symlink() or not directory.is_dir():
        return found, [f"{FRAGMENTS.as_posix()}: isn't a directory"]
    for path in sorted(directory.iterdir()):
        relative = path.relative_to(root)
        name = relative.as_posix()
        if path.name.startswith(".") or (relative == RELEASE_NOTES and path.is_dir() and not path.is_symlink()):
            continue
        if path.is_symlink() or not path.is_file():
            errors.append(f"{name}: isn't a fragment; {FRAGMENTS.as_posix()}/ holds fragments (<name>.md) and released/")
        elif not FRAGMENT_NAME.fullmatch(path.name):
            errors.append(f"{name}: a fragment's name is lowercase letters, digits and hyphens, then `.md`")
        else:
            try:
                found.append(parse_fragment(relative, path.read_bytes().decode("utf-8")))
            except UnicodeDecodeError:
                errors.append(f"{name}: isn't UTF-8")
            except ReleaseError as error:
                errors.append(str(error))
    return found, errors


def grouped(found: Sequence[Fragment], others: Sequence[str] = ()) -> list[str]:
    lines: list[str] = []
    ordered = sorted(found, key=lambda fragment: fragment.path.name)
    for heading, kinds in GROUPS:
        entries = [("- " + BREAKING if kind == "breaking" else "- ") + fragment.entry
                   for kind in kinds for fragment in ordered if fragment.kind == kind]
        if heading == "Changed":
            entries += [f"- {other}" for other in others]
        if entries:
            lines += ["", f"### {heading}", "", *entries]
    return lines


def parse_notes(text: str, version: str, name: str) -> Notes:
    parts = sections(text, name)
    heading = released_heading(parts[0]) if len(parts) == 1 else None
    if heading is None or heading.group("version") != version or text[:parts[0].start].strip():
        raise ReleaseError(f"{name}: a release's notes are one section, `## [{version}] - YYYY-MM-DD`, with nothing "
                           "before it")
    if not is_date(heading.group("date")):
        raise ReleaseError(f"{name}: the section for {version} has the impossible date {heading.group('date')}")
    body = text[parts[0].body_start:parts[0].end].strip("\n")
    if not re.search(r"^- ", body, re.MULTILINE):
        raise ReleaseError(f"{name}: the section for {version} has no entries")
    return Notes(version, heading.group("date"), body + "\n", name)


def released(root: Path) -> tuple[list[Notes], list[str]]:
    directory = root / RELEASE_NOTES
    found: list[Notes] = []
    errors: list[str] = []
    if not directory.exists() and not directory.is_symlink():
        return found, errors
    if directory.is_symlink() or not directory.is_dir():
        return found, [f"{RELEASE_NOTES.as_posix()}: isn't a directory"]
    for path in sorted(directory.iterdir()):
        if path.name.startswith("."):
            continue
        name = path.relative_to(root).as_posix()
        version = path.name.removesuffix(".md")
        if path.is_symlink() or not path.is_file() or not path.name.endswith(".md") or not VERSION_PATTERN.fullmatch(version):
            errors.append(f"{name}: isn't a release's notes ({RELEASE_NOTES.as_posix()}/X.Y.Z.md, from {FROZEN} to "
                          f"{FOLDED})")
        elif version_key(version) <= version_key(FROZEN):
            errors.append(f"{name}: {version}'s notes are in CHANGELOG.md, which is frozen at {FROZEN}; this folder "
                          "holds later releases'")
        elif version_key(version) > version_key(FOLDED):
            errors.append(f"{name}: {RELEASE_NOTES.as_posix()}/ is frozen at {FOLDED}, the last release whose fragments "
                          "were folded; a later release's notes are the changelog fragments it adds (docs/release.md)")
        else:
            try:
                found.append(parse_notes(path.read_bytes().decode("utf-8"), version, name))
            except UnicodeDecodeError:
                errors.append(f"{name}: isn't UTF-8")
            except ReleaseError as error:
                errors.append(str(error))
    return found, errors


def history_errors(root: Path) -> list[str]:
    changelog = read(root, CHANGELOG)
    if changelog is None:
        return [f"{CHANGELOG.as_posix()}: missing"]
    return frozen(changelog)[1] + released(root)[1]


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


def adopting_errors(text: str, name: Path = ADOPTING) -> list[str]:
    errors = [f"{name.as_posix()}:{number}: a snippet says {found[:40]!r}; fleet stamps no version, so a snippet names the "
              f"release {PLACEHOLDER} (docs/release.md)"
              for number, found in snippet_versions(text) if found != PLACEHOLDER]
    errors += [f"{name.as_posix()}:{number}: a snippet fetches fleet at {match.group('ref')[:40]!r}, a ref that can move; "
               "name the commit (`@<commit>`)"
               for number, line in fenced_lines(text) for match in FLEET_URL.finditer(line)
               if match.group("ref") != COMMIT_PLACEHOLDER and not agents_md.COMMIT.fullmatch(match.group("ref"))]
    return errors


def read(root: Path, path: Path) -> str | None:
    target = root / path
    return target.read_text(encoding="utf-8") if target.is_file() else None


def check(root: Path) -> list[str]:
    errors = []
    if (root / VERSION_FILE).exists() or (root / VERSION_FILE).is_symlink():
        errors.append(f"{VERSION_FILE.as_posix()}: fleet keeps no version file: a release is a tag on a commit merged to "
                      "main, and nothing in the tree names one (docs/release.md); delete it")
    errors.extend(history_errors(root))
    errors.extend(fragments(root)[1])
    plugin = read(root, PLUGIN)
    if plugin is None:
        errors.append(f"{PLUGIN.as_posix()}: missing")
    else:
        try:
            stamped = json.loads(plugin).get("version")
        except (json.JSONDecodeError, AttributeError):
            stamped = None
        if stamped != UNVERSIONED:
            errors.append(f"{PLUGIN.as_posix()}: version is {stamped!r}; fleet stamps no version, so it stays the constant "
                          f"{UNVERSIONED!r} (docs/release.md)")
    pyproject = read(root, PYPROJECT)
    if pyproject is None:
        errors.append(f"{PYPROJECT.as_posix()}: missing")
    else:
        try:
            project = tomllib.loads(pyproject).get("project", {})
        except tomllib.TOMLDecodeError:
            project = {}
        if project.get("version") != UNVERSIONED:
            errors.append(f"{PYPROJECT.as_posix()}: [project] version is {project.get('version')!r}; fleet stamps no "
                          f"version, so it stays the constant {UNVERSIONED!r} (docs/release.md)")
    if not (root / ADOPTING).is_file():
        errors.append(f"{ADOPTING.as_posix()}: missing")
    for doc in docs(root):
        errors.extend(adopting_errors((root / doc).read_text(encoding="utf-8"), doc))
    agents = read(root, AGENTS)
    if agents is None:
        errors.append(f"{AGENTS.as_posix()}: missing")
    else:
        errors.extend(agents_errors(root, agents))
    return errors


def agents_errors(root: Path, text: str) -> list[str]:
    try:
        found = agents_md.blocks(text, own=True)
        shipped = agents_md.sections(root)
    except agents_md.MarkerError as error:
        return [str(error)]
    except OSError as error:
        return [f"{agents_md.SECTIONS.as_posix()}: can't be read ({error.strerror or error})"]
    names = [block.name for block in found]
    errors = [f"{AGENTS.as_posix()}: the {name} block is missing; fleet ships it (agents-md/sections/{name}.md)"
              for name in agents_md.ORDER if name not in names]
    errors += [f"{AGENTS.as_posix()}: the {name} block isn't one fleet ships ({', '.join(agents_md.ORDER)})"
               for name in names if name not in agents_md.ORDER]
    errors += [f"{AGENTS.as_posix()}: the {block.name} block's marker carries {block.version}; fleet's own markers carry "
               "no version (`poetry run fleet sync --self` writes them)"
               for block in found if block.version]
    errors += [f"{AGENTS.as_posix()}: the {block.name} block differs from agents-md/sections/{block.name}.md; edit the "
               "section, never the block, then `poetry run fleet sync --self`"
               for block in found if block.name in shipped and text[block.body_start:block.body_end] != shipped[block.name].body]
    return errors


def docs(root: Path) -> list[Path]:
    return sorted(path.relative_to(root) for path in (root / ADOPTING.parent).glob("*.md") if path.is_file())


def notes(text: str, version: str, later: Sequence[Notes] = ()) -> str:
    for found in later:
        if found.version == version:
            return found.body
    for section in sections(text):
        heading = released_heading(section)
        if heading and heading.group("version") == version:
            body = text[section.body_start:section.end].strip("\n")
            if not body.strip():
                raise ReleaseError(f"{CHANGELOG.as_posix()}: the section for {version} is empty")
            return body + "\n"
    raise ReleaseError(f"no notes for {version}: neither a `## [{version}] - YYYY-MM-DD` section in {CHANGELOG.as_posix()} "
                       f"nor {RELEASE_NOTES.as_posix()}/{version}.md")


def entries(text: str) -> int:
    fenced = {number for number, _ in fenced_lines(text)}
    return sum(1 for number, line in enumerate(text.splitlines(), start=1) if number not in fenced and line.startswith("- "))


def consumers(root: Path) -> list[Path]:
    return sorted(path for path in (root / FIXTURES).glob("consumer-*") if path.is_dir())


class Snapshot:
    def __init__(self, root: Path, paths: list[Path]) -> None:
        self.root = root
        self.files: dict[Path, tuple[bytes, int]] = {}
        self.directories: set[Path] = set()
        self.absent: set[Path] = set()
        for path in paths:
            target = root / path
            if target.is_file() and not target.is_symlink():
                self.files[path] = (target.read_bytes(), target.stat().st_mode)
            elif target.is_dir():
                self.directories.add(path)
            elif not target.is_symlink():
                self.absent.add(path)
        self.consumers = [consumer.relative_to(root) for consumer in consumers(root)]

    def restore(self) -> None:
        for consumer in self.consumers:
            for target in sorted((self.root / consumer).rglob("*"), reverse=True):
                relative = target.relative_to(self.root)
                if target.is_file() and relative not in self.files:
                    target.unlink()
                elif target.is_dir() and relative not in self.directories and not any(target.iterdir()):
                    target.rmdir()
        for path in sorted(self.absent, key=lambda each: len(each.parts), reverse=True):
            target = self.root / path
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir() and not any(target.iterdir()):
                target.rmdir()
        for path, (data, mode) in self.files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or target.read_bytes() != data:
                target.write_bytes(data)
            if target.stat().st_mode != mode:
                target.chmod(mode)


def git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, encoding="utf-8", errors="replace",
                              timeout=60).stdout
    except subprocess.CalledProcessError as error:
        raise ReleaseError(f"`git {' '.join(args)}` failed: {(error.stderr or '').strip()[:300]}") from error
    except (subprocess.TimeoutExpired, OSError) as error:
        raise ReleaseError(f"`git {' '.join(args)}` failed: {error}") from error


def ref_argument(ref: str, flag: str) -> str:
    if not ref or ref.startswith("-") or any(char.isspace() or char == ":" for char in ref):
        raise ReleaseError(f"{flag} {ref[:40]!r} isn't a ref")
    return ref


def is_fragment_path(path: str) -> bool:
    posix = PurePosixPath(path)
    return posix.parent == PurePosixPath(FRAGMENTS) and FRAGMENT_NAME.fullmatch(posix.name) is not None


def added_paths(root: Path, old: str, new: str) -> list[str]:
    listing = git(root, "diff", "--diff-filter=A", "--no-renames", "--name-only", "-z", old, new, "--", FRAGMENTS.as_posix())
    return sorted(path for path in listing.split("\0") if path and is_fragment_path(path))


def shown(root: Path, ref: str, path: str) -> str:
    try:
        data = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=root, check=True, capture_output=True, timeout=60).stdout
    except subprocess.CalledProcessError as error:
        raise ReleaseError(f"`git show {ref}:{path}` failed: {error.stderr.decode('utf-8', 'replace').strip()[:300]}") from error
    except (subprocess.TimeoutExpired, OSError) as error:
        raise ReleaseError(f"`git show {ref}:{path}` failed: {error}") from error
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReleaseError(f"{path}: isn't UTF-8 at {ref}") from error


def added_fragments(root: Path, old: str, new: str) -> list[Fragment]:
    return [parse_fragment(Path(path), shown(root, new, path)) for path in added_paths(root, old, new)]


def without_fragment(root: Path, since: str, at: str) -> list[str]:
    subjects = []
    for line in git(root, "log", "--first-parent", "--reverse", "--format=%H %s", f"{since}..{at}", "--").splitlines():
        sha, _, subject = line.partition(" ")
        parents = git(root, "rev-list", "--parents", "-n", "1", sha, "--").split()[1:]
        if parents:
            fragments_added = added_paths(root, parents[0], sha)
        else:
            listing = git(root, "ls-tree", "-r", "--name-only", "-z", sha, "--", FRAGMENTS.as_posix())
            fragments_added = [path for path in listing.split("\0") if path and is_fragment_path(path)]
        if not fragments_added:
            subjects.append(subject.strip())
    return subjects


def notes_since(root: Path, since: str, at: str) -> str:
    since, at = ref_argument(since, "--since"), ref_argument(at, "--at")
    found = added_fragments(root, since, at)
    others = without_fragment(root, since, at)
    if not found and not others:
        raise ReleaseError(f"nothing to release between {since} and {at}: no commit, and no fragment added")
    return "\n".join(grouped(found, others)).strip("\n") + "\n"


def changes(root: Path, base: str) -> tuple[str, list[tuple[str, str]]]:
    if not base or base.startswith("-"):
        raise ReleaseError(f"--base {base[:40]!r} isn't a ref")
    start = git(root, "merge-base", base, "HEAD").strip()
    listing = git(root, "diff", "--name-status", "--no-renames", "-z", start, "HEAD", "--").split("\0")
    return start, list(zip(listing[0::2], listing[1::2], strict=False))


def claimed(title: str, body: str) -> str:
    from fleet_tools import next_version

    text = f"{title}\n\n{body}"
    if next_version.MAJOR.search(text):
        return "major"
    if next_version.MINOR.search(text):
        return "minor"
    return "patch"


def title_errors(title: str, body: str, added: Sequence[str], kinds: dict[str, str]) -> tuple[list[str], str]:
    kind = claimed(title, body)
    errors = []
    if TITLE_MARKER.search(title):
        errors.append(f"the title {title[:80]!r} ends with a `#major` or `#minor` marker, which never reaches main: GitHub "
                      "adds ` (#<number>)` after the title in the merge commit's subject. Say the kind of change with the "
                      "title's type (`feat: …`, `feat!: …`) instead")
    breaking = [path for path in added if kinds.get(path) == "breaking"]
    if breaking and kind != "major":
        errors.append(f"{', '.join(breaking)}: `kind: breaking`, but the title doesn't mark the pull request breaking: "
                      "`feat!: …` (a `!` before the colon), or a `BREAKING CHANGE:` line in the description; a breaking "
                      "change is a major release of its own (docs/release.md)")
    elif kind == "major" and added and not breaking:
        errors.append("the title or the description marks this pull request breaking (a `!` before the title's colon, a "
                      "`BREAKING CHANGE:` line or a `#major` marker), but it adds no `kind: breaking` fragment: a major "
                      "release's notes say what a consumer has to change (docs/release.md)")
    additions = [path for path in added if kinds.get(path) == "added"]
    if additions and claimed(TITLE_MARKER.sub("", title), "") == "patch":
        errors.append(f"{', '.join(additions)}: `kind: added`, but the title doesn't make the pull request a minor: "
                      "`feat: …`, in the title itself (the merge commit's subject), not the description, or `feat!: …` "
                      "with a `kind: breaking` fragment; an addition is a minor release at least (docs/release.md)")
    return errors, kind


def pull_request(root: Path, base: str, title: str | None = None, body: str = "") -> tuple[list[str], list[str], str | None]:
    start, changed = changes(root, base)
    added = [path for status, path in changed if status == "A" and is_fragment_path(path)]
    errors: list[str] = []
    if not added:
        errors.append(f"this pull request adds no changelog fragment: add {FRAGMENTS.as_posix()}/<name>.md, "
                      f"{FRAGMENT_FORMAT} (docs/release.md)")
    errors += [f"{path}: a merged fragment, which a pull request doesn't change or delete: each release's notes are the "
               "fragments its tree adds to the previous tag's, so the apps' sync bodies read it from there (docs/release.md); "
               "say a correction in a fragment of its own, and name a new one so it takes no merged fragment's name"
               for status, path in changed if status != "A" and PurePosixPath(path).parent == PurePosixPath(FRAGMENTS)]
    errors += [f"{path}: {RELEASE_NOTES.as_posix()}/ is frozen at {FOLDED}: a released version's notes, which a pull "
               "request doesn't change; a later release's notes are the fragments it adds (docs/release.md)"
               for _, path in changed if path.startswith(RELEASE_NOTES.as_posix() + "/")]
    status = {path: status for status, path in changed}.get(CHANGELOG.as_posix())
    if status == "D":
        errors.append(f"{CHANGELOG.as_posix()}: frozen at {FROZEN}, and this pull request deletes it; it keeps the "
                      "releases before the fragments")
    elif status is not None:
        before = "" if status == "A" else git(root, "show", f"{start}:{CHANGELOG.as_posix()}")
        if entries(git(root, "show", f"HEAD:{CHANGELOG.as_posix()}")) > entries(before):
            errors.append(f"{CHANGELOG.as_posix()}: frozen at {FROZEN}, but this pull request adds an entry to it: a "
                          f"change's entry is a fragment, {FRAGMENTS.as_posix()}/<name>.md (docs/release.md)")
    found, problems = fragments(root)
    errors += problems
    kinds = {fragment.path.as_posix(): fragment.kind for fragment in found}
    kind = None
    if title is not None:
        problems, kind = title_errors(title, body, added, kinds)
        errors += problems
    return errors, [f"{path} ({kinds[path]})" for path in added if path in kinds], kind


def run_check(root: Path, base: str, title: str | None = None, body: str = "") -> int:
    errors, added, kind = pull_request(root, base, title, body)
    for error in errors:
        message = f"fleet release check: {error}"
        print(f"::error::{message}" if "GITHUB_ACTIONS" in os.environ else message, file=sys.stderr)
    if errors:
        return 1
    print(f"fleet release check: ok, it adds {', '.join(added)}" + (f"; its title makes it a {kind} change" if kind else ""))
    return 0


def configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="action", metavar="<action>", required=True)
    notes_parser = actions.add_parser("notes", help="print the release notes of X.Y.Z: with --since, the notes of the "
                                      "release made from --at, as release.yml publishes it; without, an older release's "
                                      f"(changelog.d/released/X.Y.Z.md up to {FOLDED}, or CHANGELOG.md's section up to "
                                      f"{FROZEN})")
    notes_parser.add_argument("version", metavar="X.Y.Z")
    notes_parser.add_argument("--since", metavar="TAG", help="the previous release's tag: the notes are the changelog "
                              "fragments added between TAG's tree and --at's, then the subject of each first-parent "
                              "commit since TAG that added none (a Dependabot bump)")
    notes_parser.add_argument("--at", metavar="REF", default="HEAD", help="with --since, the commit being released "
                              "(default: HEAD)")
    notes_parser.add_argument("--root", type=Path, default=Path.cwd(), help="fleet's checkout (default: here)")
    check_parser = actions.add_parser("check", help="check that the commits since --base add a changelog fragment "
                                      "(changelog.d/<name>.md), that every fragment parses, and that they change no "
                                      "merged fragment, add no entry to CHANGELOG.md and change no release's notes. With "
                                      "--title, also that the title and the description mark the change breaking exactly "
                                      "when its fragment is `kind: breaking`, and that the title is a `feat:` one (or "
                                      "breaking) when its fragment is `kind: added`")
    check_parser.add_argument("--base", required=True, metavar="REF", help="the pull request's base (Release check "
                              "passes HEAD^1, the base side of the merge commit it checks out)")
    check_parser.add_argument("--title", metavar="TEXT", help="the pull request's title (Release check passes it from the "
                              "event)")
    check_parser.add_argument("--body", metavar="TEXT", default="", help="the pull request's description, with --title")
    check_parser.add_argument("--root", type=Path, default=Path.cwd(), help="fleet's checkout (default: here)")


def run(args: argparse.Namespace) -> int:
    root: Path = args.root
    try:
        if args.action == "check":
            return run_check(root, args.base, args.title, args.body)
        parse_version(args.version)
        if args.since is not None:
            sys.stdout.write(notes_since(root, args.since, args.at))
            return 0
        text = read(root, CHANGELOG)
        if text is None:
            raise ReleaseError(f"{CHANGELOG.as_posix()}: missing")
        sys.stdout.write(notes(text, args.version, released(root)[0]))
        return 0
    except (ReleaseError, agents_md.MarkerError) as error:
        print(f"fleet release: {error}", file=sys.stderr)
        return 1
