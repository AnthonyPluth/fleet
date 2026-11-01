import argparse
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from consumers import WAYPOINT, commit, git

from fleet_tools import agents_md, drift, vendor

ROOT = Path(__file__).resolve().parent.parent
KEY = re.compile(r"^(pins|content)=(v\d+\.\d+\.\d+)$")
SYNC_CALLER = """name: Sync fleet content

on:
  workflow_dispatch:

permissions: {}

defaults:
  run:
    shell: bash

jobs:
  sync:
    permissions:
      contents: read
      issues: write
    uses: AnthonyPluth/fleet/.github/workflows/sync.yml@0123456789abcdef0123456789abcdef01234567   # PINS
    with:
      app-id: ${{ vars.FLEET_BOT_APP_ID }}
    secrets:
      FLEET_BOT_PRIVATE_KEY: ${{ secrets.FLEET_BOT_PRIVATE_KEY }}
"""


def parse_spec(spec: str) -> dict[str, str]:
    found = {}
    for word in spec.split():
        match = KEY.fullmatch(word)
        if not match:
            raise SystemExit(f"sync_fixture: {word!r} isn't pins=vX.Y.Z or content=vX.Y.Z")
        found[match.group(1)] = match.group(2)
    missing = {"pins", "content"} - set(found)
    if missing:
        raise SystemExit(f"sync_fixture: the spec names no {', '.join(sorted(missing))}")
    return found


def expected() -> list[str]:
    shipped = vendor.shipped_at(agents_md.own_version()) or {}
    return sorted([agents_md.AGENTS, agents_md.VERSION_FILE.as_posix(), *(target.as_posix() for target in shipped)])


def build(out: Path, pins: str, content: str, source: Path = WAYPOINT) -> Path:
    own = agents_md.own_version()
    shutil.copytree(source, out)
    workflows = out / drift.WORKFLOWS
    for path in sorted(workflows.glob("*.y*ml")):
        path.write_text(path.read_text(encoding="utf-8").replace(f"# {own}", f"# {pins}"), encoding="utf-8")
    (workflows / "sync.yml").write_text(SYNC_CALLER.replace("# PINS", f"# {pins}"), encoding="utf-8")
    agents = out / agents_md.AGENTS
    agents.write_text(agents_md.sync(agents.read_text(encoding="utf-8"), agents_md.sections(), content)[0], encoding="utf-8")
    for target, vendored in vendor.render(vendor.working_tree(ROOT), content).items():
        path = out / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(vendored.text, encoding="utf-8")
        if vendored.executable:
            path.chmod(path.stat().st_mode | 0o755)
    (out / agents_md.VERSION_FILE).write_text(content + "\n", encoding="utf-8")
    git(out, "init", "-q", "-b", "main")
    commit(out, "chore: the fixture consumer")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="a fixture consumer with its workflow pins and its content at chosen fleet versions")
    parser.add_argument("--out", type=Path, help="where to build it (must not exist)")
    parser.add_argument("--spec", default="", help='"pins=vX.Y.Z content=vA.B.C"')
    parser.add_argument("--expected", action="store_true", help="print the files a sync changes, one per line, and exit")
    args = parser.parse_args(argv)
    if args.expected:
        print("\n".join(expected()))
        return 0
    if args.out is None:
        parser.error("--out is required")
    spec = parse_spec(args.spec)
    build(args.out, spec["pins"], spec["content"])
    print(f"sync_fixture: {args.out} with pins at {spec['pins']} and content at {spec['content']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
