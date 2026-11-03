import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from fleet_tools import agents_md, config, vendor

HELP = ("check that the consumer's fleet content is fleet's: every AGENTS.md block byte-equal to fleet's at the version "
        "in its marker, every vendored plugin file byte-equal to fleet's at .fleet/version, one version across the markers and "
        ".fleet/version, that version within the workflow pins, and .fleet/version's commit (line 2, which tools/fleet "
        "runs) the one the pins at that version name")

WORKFLOWS = ".github/workflows"
PIN = re.compile(r"^[ \t]*(?:-[ \t]*)?uses:[ \t]*AnthonyPluth/fleet/[^@\s]+@(?P<ref>\S+)(?P<rest>.*)$",
                 re.MULTILINE | re.IGNORECASE)
PIN_VERSION = re.compile(r"#\s*(v\d+\.\d+\.\d+)\b")


@dataclass(frozen=True)
class Pin:
    file: str
    ref: str
    version: str | None


def find_pins(root: Path) -> list[Pin]:
    found = []
    for path in sorted((root / WORKFLOWS).glob("*.y*ml")):
        name = path.relative_to(root).as_posix()
        for pin in PIN.finditer(path.read_text(encoding="utf-8")):
            version = PIN_VERSION.search(pin.group("rest"))
            found.append(Pin(name, pin.group("ref"), version.group(1) if version else None))
    return found


def pins(root: Path) -> tuple[dict[str, list[str]], list[str]]:
    found: dict[str, list[str]] = {}
    problems = []
    for pin in find_pins(root):
        if pin.version:
            found.setdefault(pin.version, []).append(pin.file)
        else:
            problems.append(f"{pin.file}: the fleet pin @{pin.ref} has no version in a comment (`# vX.Y.Z`)")
    return found, problems


def pinned_commit(root: Path, version: str) -> tuple[str | None, list[str]]:
    commits: dict[str, list[str]] = {}
    for pin in find_pins(root):
        if pin.version == version and agents_md.COMMIT.fullmatch(pin.ref):
            commits.setdefault(pin.ref, []).append(pin.file)
    if len(commits) > 1:
        listing = "; ".join(f"{commit} in {', '.join(files)}" for commit, files in sorted(commits.items()))
        return None, [f"the fleet pins at {version} name more than one commit: {listing}; bump them together "
                      "(Dependabot's fleet group does)"]
    return next(iter(commits), None), []


def check_blocks(text: str) -> tuple[list[agents_md.Block], list[str]]:
    found = agents_md.blocks(text)
    problems = []
    cache: dict[str, dict[str, agents_md.Section] | None] = {}
    for block in found:
        if block.version not in cache:
            cache[block.version] = agents_md.sections_at(block.version)
        shipped = cache[block.version]
        if shipped is None:
            problems.append(f"{agents_md.AGENTS}: can't read fleet's {block.name} block at {block.version} from fleet "
                            f"{agents_md.own_version()}; run fleet drift from fleet at {block.version}")
        elif block.name not in shipped:
            problems.append(f"{agents_md.AGENTS}: fleet {block.version} has no {block.name} block")
        elif text[block.body_start:block.body_end] != shipped[block.name].body:
            problems.append(f"{agents_md.AGENTS}: the {block.name} block differs from fleet's at {block.version}; edit "
                            f"it in fleet (agents-md/sections/{block.name}.md), not here, or run `fleet sync`")
    present = {block.name for block in found}
    versions = {block.version for block in found}
    for version in sorted(versions, key=agents_md.version_key):
        shipped = cache.get(version)
        for name in [] if shipped is None else shipped:
            if name not in present:
                problems.append(f"{agents_md.AGENTS}: no {name} block (fleet sync --to {version} inserts it)")
    return found, problems


def drift(root: Path) -> tuple[list[str], str]:
    problems = []
    path = root / agents_md.AGENTS
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    try:
        found, block_problems = check_blocks(text)
    except agents_md.MarkerError as error:
        return [str(error)], ""
    problems += block_problems
    if not path.is_file():
        problems.append(f"{agents_md.AGENTS} doesn't exist (fleet sync writes its blocks)")
    elif not found:
        problems.append(f"{agents_md.AGENTS} has no fleet block (fleet sync inserts them)")
    version_file = root / agents_md.VERSION_FILE
    content: str | None = None
    commit: str | None = None
    if not version_file.is_file():
        problems.append(f"{agents_md.VERSION_FILE.as_posix()} doesn't exist (fleet sync writes it)")
    else:
        try:
            parsed = agents_md.parse_version_file(version_file.read_text(encoding="utf-8"))
            content, commit = parsed.version, parsed.commit
        except ValueError as error:
            problems.append(f"{error}; run `fleet sync`")
    if content and commit is None and agents_md.needs_commit(content):
        problems.append(f"{agents_md.VERSION_FILE.as_posix()} names no commit for {content} on its line 2, which "
                        f"tools/fleet runs fleet at; run `fleet sync --to {content}` from fleet at {content}, which writes it")
    versions = {block.version for block in found} | ({content} if content else set())
    if len(versions) > 1:
        where = [f"{block.name} {block.version}" for block in found if block.version != content]
        problems.append(f"fleet content is at more than one version: {agents_md.VERSION_FILE.as_posix()} {content}, "
                        f"{agents_md.AGENTS} {', '.join(where)}; run `fleet sync` at one version")
    vendored = 0
    if content:
        vendored, vendor_problems = vendor.check(root, content)
        problems += vendor_problems
    pinned, pin_problems = pins(root)
    problems += pin_problems
    if content:
        pinned_at, commit_problems = pinned_commit(root, content)
        problems += commit_problems
        if commit and pinned_at and commit != pinned_at:
            problems.append(f"{agents_md.VERSION_FILE.as_posix()} says fleet {content} is commit {commit}, but the "
                            f"workflow pins at {content} in {', '.join(pinned[content])} name {pinned_at}; run "
                            f"`fleet sync --to {content}`, which takes the pins' commit")
    summary = ""
    if content and len(versions) == 1:
        newer = [pin for pin in pinned if agents_md.version_key(content) > agents_md.version_key(pin)]
        major = [pin for pin in pinned if agents_md.version_key(content)[0] != agents_md.version_key(pin)[0]]
        for pin in sorted(set(newer) | set(major), key=agents_md.version_key):
            why = "is newer than" if pin in newer else "is another major version than"
            problems.append(f"fleet content {content} {why} the workflow pin {pin} in {', '.join(pinned[pin])}")
        pins_text = ", ".join(sorted(pinned, key=agents_md.version_key)) if pinned else "none"
        at = f"{content} (commit {commit})" if commit else f"{content} (no commit: fleet {content} wrote none)"
        summary = (f"{len(found)} AGENTS.md blocks, {vendored} vendored files and {agents_md.VERSION_FILE.as_posix()} at "
                   f"{at}; "
                   f"fleet workflow pins: {pins_text}")
    return problems, summary


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, help="the consumer's checkout (default: the nearest pyproject.toml up from here)")


def run(args: argparse.Namespace) -> int:
    try:
        root = (args.root or config.find_root(Path.cwd())).resolve()
    except config.ConfigError as error:
        print(f"fleet drift: {error}", file=sys.stderr)
        return 2
    problems, summary = drift(root)
    for problem in problems:
        print(f"::error::{problem}" if "GITHUB_ACTIONS" in os.environ else problem, file=sys.stderr)
    if problems:
        return 1
    print(f"fleet drift: no drift: {summary}.")
    return 0
