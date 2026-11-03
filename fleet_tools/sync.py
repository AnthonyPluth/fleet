import argparse
import subprocess
import sys
from pathlib import Path

from fleet_tools import agents_md, config, drift, marketplace, vendor

HELP = ("sync fleet's content into the consumer at this fleet's version: AGENTS.md's fleet blocks (replaced, or "
        "inserted under their heading; nothing outside the markers changes), the vendored plugin files (.claude/skills/"
        "fleet-*, .claude/agents/fleet-reviewer.md, .claude/hooks/fleet-session-start.sh, tools/fleet), the ref of fleet's "
        "marketplace in .claude/settings.json (when it declares one) and .fleet/version "
        "(the version, and the commit its tag points at, which tools/fleet runs fleet at: taken from the workflow pins at "
        "that version, else from `git ls-remote` of the tag, or given as --commit)")
REPOSITORY = "https://github.com/AnthonyPluth/fleet"


class SyncError(Exception):
    pass


def ls_remote(version: str) -> str:
    try:
        listing = subprocess.run(["git", "ls-remote", "--tags", "--", REPOSITORY, f"refs/tags/{version}", f"refs/tags/{version}^{{}}"],
                                 check=True, capture_output=True, text=True, timeout=60).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        raise SyncError(f"can't read the tag {version} from {REPOSITORY} ({error}); pass --commit <sha>, the commit the tag "
                        "points at") from error
    found = {}
    for line in listing.splitlines():
        sha, _, ref = line.partition("\t")
        if agents_md.COMMIT.fullmatch(sha):
            found[ref] = sha
    commit = found.get(f"refs/tags/{version}^{{}}") or found.get(f"refs/tags/{version}")
    if commit is None:
        raise SyncError(f"{REPOSITORY} has no tag {version}; a consumer syncs to a released version")
    return commit


def resolve_commit(root: Path, version: str, commit: str | None = None) -> str:
    pinned, problems = drift.pinned_commit(root, version)
    if problems:
        raise SyncError(problems[0])
    if commit is not None:
        if not agents_md.COMMIT.fullmatch(commit):
            raise SyncError(f"--commit {commit[:40]!r} isn't a commit (40 hex digits)")
        if pinned and pinned != commit:
            raise SyncError(f"--commit {commit} isn't the commit the workflow pins at {version} name ({pinned})")
        return commit
    return pinned or ls_remote(version)


def sync(root: Path, version: str, fleet: Path | None = None, commit: str | None = None) -> list[str]:
    path = root / agents_md.AGENTS
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    synced, changes = agents_md.sync(text, agents_md.sections(fleet), version)
    commit = resolve_commit(root, version, commit)
    settings, moved = marketplace.plan(root, version)
    vendored = vendor.sync(root, version, None if fleet is None else vendor.render(vendor.working_tree(fleet), version))
    if synced != text:
        path.write_text(synced, encoding="utf-8")
    report = [f"{agents_md.AGENTS}: {change}" for change in changes] + vendored
    marketplace.write(root, settings)
    report += moved
    version_file = root / agents_md.VERSION_FILE
    current = version_file.read_text(encoding="utf-8") if version_file.is_file() else None
    rendered = agents_md.render_version_file(version, commit)
    if current != rendered:
        version_file.parent.mkdir(parents=True, exist_ok=True)
        version_file.write_text(rendered, encoding="utf-8")
        report.append(f"{agents_md.VERSION_FILE.as_posix()}: {version} at {commit}")
    return report


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--to", required=True, metavar="vX.Y.Z", help="the version to sync to: this fleet's own")
    parser.add_argument("--root", type=Path, help="the consumer's checkout (default: the nearest pyproject.toml up from here)")
    parser.add_argument("--commit", metavar="SHA", help="the commit the tag --to points at, for .fleet/version (default: the "
                        "workflow pins' at that version, else the tag's from `git ls-remote`)")


def run(args: argparse.Namespace) -> int:
    own = agents_md.own_version()
    if args.to != own:
        print(f"fleet sync: this is fleet {own}; run `fleet sync --to {args.to}` from fleet at {args.to}, whose "
              "sections it ships", file=sys.stderr)
        return 2
    try:
        root = (args.root or config.find_root(Path.cwd())).resolve()
        changes = sync(root, own, commit=args.commit)
    except (config.ConfigError, agents_md.MarkerError, vendor.VendorError, marketplace.MarketplaceError,
            SyncError) as error:
        print(f"fleet sync: {error}", file=sys.stderr)
        return 2
    for change in changes:
        print(change)
    print(f"fleet sync: {root.name} is at fleet {own}" + ("" if changes else " already; nothing changed") + ".")
    return 0
