import argparse
import sys
from pathlib import Path

from fleet_tools import agents_md, config, vendor

HELP = ("sync fleet's content into the consumer at this fleet's version: AGENTS.md's fleet blocks (replaced, or "
        "inserted under their heading; nothing outside the markers changes), the vendored plugin files (.claude/skills/"
        "fleet-*, .claude/agents/fleet-reviewer.md, .claude/hooks/fleet-session-start.sh, tools/fleet) and .fleet/version")


def sync(root: Path, version: str) -> list[str]:
    path = root / agents_md.AGENTS
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    synced, changes = agents_md.sync(text, agents_md.sections(), version)
    vendored = vendor.sync(root, version)
    if synced != text:
        path.write_text(synced, encoding="utf-8")
    report = [f"{agents_md.AGENTS}: {change}" for change in changes] + vendored
    version_file = root / agents_md.VERSION_FILE
    current = version_file.read_text(encoding="utf-8") if version_file.is_file() else None
    if current != version + "\n":
        version_file.parent.mkdir(parents=True, exist_ok=True)
        version_file.write_text(version + "\n", encoding="utf-8")
        report.append(f"{agents_md.VERSION_FILE.as_posix()}: {version}")
    return report


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--to", required=True, metavar="vX.Y.Z", help="the version to sync to: this fleet's own")
    parser.add_argument("--root", type=Path, help="the consumer's checkout (default: the nearest pyproject.toml up from here)")


def run(args: argparse.Namespace) -> int:
    own = agents_md.own_version()
    if args.to != own:
        print(f"fleet sync: this is fleet {own}; run `fleet sync --to {args.to}` from fleet at {args.to}, whose "
              "sections it ships", file=sys.stderr)
        return 2
    try:
        root = (args.root or config.find_root(Path.cwd())).resolve()
        changes = sync(root, own)
    except (config.ConfigError, agents_md.MarkerError, vendor.VendorError) as error:
        print(f"fleet sync: {error}", file=sys.stderr)
        return 2
    for change in changes:
        print(change)
    print(f"fleet sync: {root.name} is at fleet {own}" + ("" if changes else " already; nothing changed") + ".")
    return 0
