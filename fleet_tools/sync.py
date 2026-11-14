import argparse
import subprocess
import sys
from pathlib import Path

from fleet_tools import agents_md, config, drift, identity, marketplace, vendor

HELP = ("sync fleet's content into the consumer at this fleet's commit: AGENTS.md's fleet blocks (replaced, or "
        "inserted under their heading; nothing outside the markers changes), the vendored plugin files (.claude/skills/"
        "fleet-*, .claude/agents/fleet-reviewer.md, .claude/hooks/fleet-session-start.sh, tools/fleet), the ref of fleet's "
        "marketplace in .claude/settings.json (when it declares one) and .fleet/version "
        "(the version, and the commit its tag points at, which tools/fleet runs fleet at: taken from the workflow pins at "
        "that version, else from `git ls-remote` of the tag, or given as --commit). That commit must be this fleet's "
        "own; --to gives the version the markers, the notes and .fleet/version carry. --self syncs fleet's own "
        "checkout instead")
REPOSITORY = "https://github.com/AnthonyPluth/fleet"
FIXTURES = Path("tests/fixtures")
INSTALL = 'uvx --from "git+https://github.com/AnthonyPluth/fleet@<commit>" fleet'


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


def resolve(root: Path, version: str, commit: str | None = None) -> tuple[str, str]:
    pinned, problems = drift.pinned_commit(root, version)
    if problems:
        raise SyncError(problems[0])
    if commit is not None:
        if not agents_md.COMMIT.fullmatch(commit):
            raise SyncError(f"--commit {commit[:40]!r} isn't a commit (40 hex digits)")
        if pinned and pinned != commit:
            raise SyncError(f"--commit {commit} isn't the commit the workflow pins at {version} name ({pinned})")
        return commit, "--commit"
    if pinned:
        return pinned, f"the commit the workflow pins at {version} name"
    return ls_remote(version), f"the commit the tag {version} points at"


def resolve_commit(root: Path, version: str, commit: str | None = None) -> str:
    return resolve(root, version, commit)[0]


def check_own(version: str, commit: str, source: str) -> None:
    own = identity.commit()
    if own is None:
        raise SyncError(f"this is {identity.describe()}, so it can't tell whether {version} ({source}, {commit}) is "
                        f"this fleet; run fleet from a checkout, or by commit: `{INSTALL} sync --to {version}`")
    if own != commit:
        raise SyncError(f"this is {identity.describe()}, but {version} is {commit} ({source}); run `fleet sync --to "
                        f"{version}` from fleet at {commit}, whose content it ships")


def sync(root: Path, version: str, fleet: Path | None = None, commit: str | None = None) -> list[str]:
    fleet = fleet or agents_md.ROOT
    path = root / agents_md.AGENTS
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    synced, changes = agents_md.sync(text, agents_md.sections(fleet), version)
    commit = resolve_commit(root, version, commit)
    settings, moved = marketplace.plan(root, version)
    vendored = vendor.sync(root, version, vendor.render(vendor.working_tree(fleet), version))
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


def marker_version(text: str) -> str:
    versions = sorted({block.version for block in agents_md.blocks(text)}, key=agents_md.version_key)
    if len(versions) != 1:
        raise SyncError(f"{agents_md.AGENTS}: its fleet markers carry {', '.join(versions) or 'no version'}, not one "
                        "version (fleet release prepare stamps them; fleet self-check names the one)")
    return versions[0]


def fixture_consumers(root: Path) -> list[Path]:
    return sorted(path for path in (root / FIXTURES).glob("consumer-*") if path.is_dir())


def sync_self(root: Path) -> list[str]:
    from fleet_tools import release

    path = root / agents_md.AGENTS
    if not path.is_file() or not (root / agents_md.SECTIONS).is_dir():
        raise SyncError(f"{root} isn't fleet's checkout (it needs {agents_md.AGENTS} and {agents_md.SECTIONS.as_posix()}/)")
    text = path.read_text(encoding="utf-8")
    synced, changes = agents_md.sync(text, agents_md.sections(root), marker_version(text))
    report = [f"{agents_md.AGENTS}: {change}" for change in changes]
    consumers = fixture_consumers(root)
    labels = {}
    for consumer in consumers:
        parsed, problem = drift.read_version_file(consumer)
        if parsed is None:
            raise SyncError(f"{consumer.relative_to(root).as_posix()}: {problem}")
        labels[consumer] = parsed.version
    snapshot = release.Snapshot(root, [Path(agents_md.AGENTS), *(each.relative_to(root) for consumer in consumers
                                                                 for each in consumer.rglob("*"))])
    try:
        if synced != text:
            path.write_text(synced, encoding="utf-8")
        for consumer in consumers:
            where = consumer.relative_to(root).as_posix()
            try:
                changes = sync(consumer, labels[consumer], root)
            except (agents_md.MarkerError, vendor.VendorError, marketplace.MarketplaceError, SyncError) as error:
                raise SyncError(f"{where}: {error}") from error
            report += [f"{where}/{change}" for change in changes]
    except BaseException:
        snapshot.restore()
        raise
    return report


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--to", metavar="vX.Y.Z", help="the version to sync to, whose commit (the workflow pins', "
                        "--commit, or the tag's) must be this fleet's own")
    parser.add_argument("--root", type=Path, help="the consumer's checkout (default: the nearest pyproject.toml up from "
                        "here; with --self, this fleet's own checkout)")
    parser.add_argument("--commit", metavar="SHA", help="the commit the tag --to points at, for .fleet/version (default: the "
                        "workflow pins' at that version, else the tag's from `git ls-remote`)")
    parser.add_argument("--self", action="store_true", help="fleet's own checkout instead of a consumer: its AGENTS.md "
                        "blocks and its fixture consumers (tests/fixtures/consumer-*) from its own tree, each at the "
                        "version its markers and .fleet/version already carry (fleet release prepare moves those)")


def run(args: argparse.Namespace) -> int:
    if args.self:
        return run_self(args)
    if args.to is None:
        print("fleet sync: say which version (--to vX.Y.Z), or --self for fleet's own checkout", file=sys.stderr)
        return 2
    if not agents_md.VERSION.fullmatch(args.to):
        print(f"fleet sync: --to {args.to[:40]!r} isn't a version (vX.Y.Z)", file=sys.stderr)
        return 2
    try:
        root = (args.root or config.find_root(Path.cwd())).resolve()
        commit, source = resolve(root, args.to, args.commit)
        check_own(args.to, commit, source)
        changes = sync(root, args.to, commit=commit)
    except (config.ConfigError, agents_md.MarkerError, vendor.VendorError, marketplace.MarketplaceError,
            SyncError) as error:
        print(f"fleet sync: {error}", file=sys.stderr)
        return 2
    for change in changes:
        print(change)
    print(f"fleet sync: {root.name} is at fleet {args.to}" + ("" if changes else " already; nothing changed") + ".")
    return 0


def run_self(args: argparse.Namespace) -> int:
    if args.to is not None or args.commit is not None:
        print("fleet sync: --self takes neither --to nor --commit: fleet's own checkout keeps the versions it carries",
              file=sys.stderr)
        return 2
    root = (args.root or agents_md.ROOT).resolve()
    try:
        changes = sync_self(root)
    except (agents_md.MarkerError, vendor.VendorError, marketplace.MarketplaceError, SyncError, OSError) as error:
        print(f"fleet sync: {error}", file=sys.stderr)
        return 2
    for change in changes:
        print(change)
    print(f"fleet sync: {root.name}'s AGENTS.md and fixture consumers match its tree" + ("" if changes else " already; "
          "nothing changed") + ".")
    return 0
