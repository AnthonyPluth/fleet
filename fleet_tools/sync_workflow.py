import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from fleet_tools import agents_md, config, drift

HELP = ("plan the consumer's content sync from its workflow pins, for sync.yml: nothing when the pins and .fleet/version "
        "agree, a `chore: fleet content vX.Y.Z` pull request when the pins are newer in the same major, a migration issue "
        "when they are a newer major; pins that disagree fail naming the files")

BRANCH = "fleet/sync"
NONE = "none"
SYNC = "sync"
MIGRATION = "migration"
PR_TITLE = "chore: fleet content {pin}"
ISSUE_TITLE = "fleet v{major} needs a migration: see CHANGELOG"
RELEASE_HEADING = re.compile(r"^## \[(?P<version>\d+\.\d+\.\d+)\]", re.MULTILINE)
ANY_HEADING = re.compile(r"^## ", re.MULTILINE)


class PlanError(Exception):
    pass


@dataclass(frozen=True)
class Plan:
    action: str
    content: str
    pin: str
    files: tuple[str, ...]
    reason: str

    @property
    def title(self) -> str:
        if self.action == SYNC:
            return PR_TITLE.format(pin=self.pin)
        if self.action == MIGRATION:
            return ISSUE_TITLE.format(major=agents_md.version_key(self.pin)[0])
        return ""


def content_version(root: Path) -> str:
    path = root / agents_md.VERSION_FILE
    if not path.is_file():
        raise PlanError(f"{agents_md.VERSION_FILE.as_posix()} doesn't exist: adopt fleet's content by hand first "
                        "(`fleet sync --to <the pins' version>` from fleet at that version; docs/adopting.md)")
    text = path.read_text(encoding="utf-8").strip()
    if not agents_md.VERSION.fullmatch(text):
        raise PlanError(f"{agents_md.VERSION_FILE.as_posix()} holds {text[:40]!r}, not a version (vX.Y.Z)")
    return text


def plan(root: Path) -> Plan:
    pinned, problems = drift.pins(root)
    if problems:
        raise PlanError("; ".join(problems))
    if len(pinned) > 1:
        listing = "; ".join(f"{version} in {', '.join(files)}"
                            for version, files in sorted(pinned.items(), key=lambda item: agents_md.version_key(item[0])))
        raise PlanError(f"the fleet pins disagree: {listing}; bump them together (Dependabot's fleet group does)")
    if not pinned:
        return Plan(NONE, "", "", (), f"no fleet pin in {drift.WORKFLOWS}; nothing to sync to")
    content = content_version(root)
    pin, files = next(iter(pinned.items()))
    where = ", ".join(files)
    have, want = agents_md.version_key(content), agents_md.version_key(pin)
    if have == want:
        return Plan(NONE, content, pin, tuple(files), f"the content and the workflow pins agree at {pin}")
    if have > want:
        raise PlanError(f"fleet content {content} is newer than the workflow pin {pin} in {where}; bump the pins "
                        "(Dependabot) rather than the content")
    if have[0] < want[0]:
        return Plan(MIGRATION, content, pin, tuple(files),
                    f"the pins ({where}) are at {pin}, a newer major than the content's {content}")
    return Plan(SYNC, content, pin, tuple(files), f"the pins ({where}) are at {pin}; the content is at {content}")


def changelog_between(text: str, after: str, upto: str) -> str:
    low, high = agents_md.version_key(after), agents_md.version_key(upto)
    sections = []
    for found in RELEASE_HEADING.finditer(text):
        version = agents_md.version_key("v" + found.group("version"))
        if not low < version <= high:
            continue
        following = ANY_HEADING.search(text, found.end())
        sections.append(text[found.start():following.start() if following else len(text)].rstrip() + "\n")
    if not sections:
        return f"The CHANGELOG at {upto} has no entry between {after} and {upto}.\n"
    return "\n".join(sections)


def pull_request_body(found: Plan, changelog: str) -> str:
    where = ", ".join(f"`{name}`" for name in found.files)
    return (f"Syncs fleet's content from {found.content} to {found.pin}, the version the workflow pins carry ({where}): "
            "the `AGENTS.md` blocks between the fleet markers, the vendored `.claude/` files and `tools/fleet`, and "
            "`.fleet/version`. Nothing outside the markers and nothing under `.github/workflows/` changes: Dependabot "
            "bumps the pins, and this pull request follows each bump.\n\n"
            "Opened by fleet's sync workflow with the fleet-bot App, so the pull request's checks run; the branch "
            f"`{BRANCH}` is rewritten by each run until this merges. `fleet drift` lets the content trail the pins "
            "while it is open.\n\n"
            f"## CHANGELOG {found.content} → {found.pin}\n\n{changelog_between(changelog, found.content, found.pin)}")


def issue_body(found: Plan, changelog: str) -> str:
    where = ", ".join(f"`{name}`" for name in found.files)
    return (f"The workflow pins ({where}) are at {found.pin} and the content (`.fleet/version`) at {found.content}: "
            "another major version. fleet's sync workflow doesn't cross a major, because a major release changes an "
            "input, a status name or an `AGENTS.md` block in a way the app has to adapt to first.\n\n"
            "Read the CHANGELOG entries below, make the changes they ask for, then sync the content by hand from fleet "
            f"at the pins' version: `uvx --from \"git+https://github.com/AnthonyPluth/fleet@{found.pin}\" fleet sync "
            f"--to {found.pin}`, and commit `AGENTS.md`, `.fleet/version` and the vendored files together. "
            "`fleet drift` fails until they match; this issue is done when it passes.\n\n"
            f"## CHANGELOG {found.content} → {found.pin}\n\n{changelog_between(changelog, found.content, found.pin)}")


def body(found: Plan, changelog: str) -> str:
    if found.action == SYNC:
        return pull_request_body(found, changelog)
    if found.action == MIGRATION:
        return issue_body(found, changelog)
    return ""


def outputs(found: Plan) -> list[str]:
    return [f"action={found.action}", f"content={found.content}", f"pin={found.pin}", f"files={', '.join(found.files)}",
            f"title={found.title}", f"branch={BRANCH}", f"reason={found.reason}"]


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, help="the consumer's checkout (default: the nearest pyproject.toml up from here)")
    parser.add_argument("--changelog", type=Path, metavar="CHANGELOG.md", help="fleet's CHANGELOG at the pins' version, for the body")
    parser.add_argument("--body", type=Path, metavar="FILE", help="write the pull request's or the issue's body here (needs --changelog)")


def run(args: argparse.Namespace) -> int:
    try:
        root = (args.root or config.find_root(Path.cwd())).resolve()
        found = plan(root)
    except (config.ConfigError, PlanError) as error:
        message = f"fleet sync-plan: {error}"
        print(f"::error::{message}" if "GITHUB_ACTIONS" in os.environ else message, file=sys.stderr)
        return 2 if isinstance(error, config.ConfigError) else 1
    if args.body is not None:
        if args.changelog is None:
            print("fleet sync-plan: --body needs --changelog", file=sys.stderr)
            return 2
        args.body.write_text(body(found, args.changelog.read_text(encoding="utf-8")), encoding="utf-8")
    for line in outputs(found):
        print(line)
    return 0
