import argparse
import os
import re
import sys
from pathlib import Path

from fleet_tools import agents_md, config, vendor

HELP = ("check that the consumer's fleet content is fleet's: every AGENTS.md block byte-equal to fleet's at the version "
        "in its marker, every vendored plugin file byte-equal to fleet's at .fleet/version, one version across the markers and "
        ".fleet/version, and that version within the workflow pins")

WORKFLOWS = ".github/workflows"
PIN = re.compile(r"^[ \t]*(?:-[ \t]*)?uses:[ \t]*AnthonyPluth/fleet/[^@\s]+@(?P<ref>\S+)(?P<rest>.*)$",
                 re.MULTILINE | re.IGNORECASE)
PIN_VERSION = re.compile(r"#\s*(v\d+\.\d+\.\d+)\b")


def pins(root: Path) -> tuple[dict[str, list[str]], list[str]]:
    found: dict[str, list[str]] = {}
    problems = []
    for path in sorted((root / WORKFLOWS).glob("*.y*ml")):
        name = path.relative_to(root).as_posix()
        for pin in PIN.finditer(path.read_text(encoding="utf-8")):
            version = PIN_VERSION.search(pin.group("rest"))
            if version:
                found.setdefault(version.group(1), []).append(name)
            else:
                problems.append(f"{name}: the fleet pin @{pin.group('ref')} has no version in a comment (`# vX.Y.Z`)")
    return found, problems


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
    content = version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else None
    if content is None:
        problems.append(f"{agents_md.VERSION_FILE.as_posix()} doesn't exist (fleet sync writes it)")
    elif not agents_md.VERSION.fullmatch(content):
        problems.append(f"{agents_md.VERSION_FILE.as_posix()} holds {content[:40]!r}, not a version (vX.Y.Z)")
        content = None
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
    summary = ""
    if content and len(versions) == 1:
        newer = [pin for pin in pinned if agents_md.version_key(content) > agents_md.version_key(pin)]
        major = [pin for pin in pinned if agents_md.version_key(content)[0] != agents_md.version_key(pin)[0]]
        for pin in sorted(set(newer) | set(major), key=agents_md.version_key):
            why = "is newer than" if pin in newer else "is another major version than"
            problems.append(f"fleet content {content} {why} the workflow pin {pin} in {', '.join(pinned[pin])}")
        pins_text = ", ".join(sorted(pinned, key=agents_md.version_key)) if pinned else "none"
        summary = (f"{len(found)} AGENTS.md blocks, {vendored} vendored files and {agents_md.VERSION_FILE.as_posix()} at "
                   f"{content}; "
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
