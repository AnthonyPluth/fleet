import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fleet_tools import config

HELP = "fail when coverage.py's JSON report is under the floors in [tool.fleet.coverage] (overall, and per module prefix)"


@dataclass(frozen=True)
class Floors:
    overall: float | None = None
    modules: dict[str, float] = field(default_factory=dict)


def floors(conf: config.Config) -> Floors:
    if not conf.has("coverage"):
        raise config.ConfigError("no [tool.fleet.coverage] in pyproject.toml: set `overall` and/or `modules` floors")
    section = conf.section("coverage")
    overall = section.get("overall")
    modules = section.get("modules", {})
    if overall is not None and (isinstance(overall, bool) or not isinstance(overall, int | float)):
        raise config.ConfigError("[tool.fleet.coverage] overall must be a number (a percentage)")
    if not isinstance(modules, dict) or not all(
            isinstance(value, int | float) and not isinstance(value, bool) for value in modules.values()):
        raise config.ConfigError("[tool.fleet.coverage] modules must map path prefixes to numbers (percentages)")
    return Floors(None if overall is None else float(overall), {str(k): float(v) for k, v in modules.items()})


def floor_for(name: str, modules: dict[str, float]) -> tuple[str, float] | None:
    matches = [prefix for prefix in modules if name.startswith(prefix)]
    if not matches:
        return None
    prefix = max(matches, key=len)
    return prefix, modules[prefix]


def problems(report: dict[str, Any], limits: Floors) -> list[str]:
    out = []
    total = report["totals"]["percent_covered"]
    if limits.overall is not None and total < limits.overall:
        out.append(f"the tests cover {total:.1f}% overall; the floor is {limits.overall:.0f}%")
    for name, entry in sorted(report["files"].items()):
        found = floor_for(name, limits.modules)
        if found is None:
            continue
        prefix, floor = found
        pct, statements = entry["summary"]["percent_covered"], entry["summary"]["num_statements"]
        if statements and pct < floor:
            out.append(f"{name}: the tests cover {pct:.1f}%; modules in {prefix} need {floor:.0f}% (add tests that run it)")
    return out


def summary(limits: Floors) -> str:
    parts = [f"{limits.overall:.0f}% overall"] if limits.overall is not None else []
    parts += [f"{floor:.0f}% for each module in {prefix}" for prefix, floor in limits.modules.items()]
    return ", ".join(parts) or "no floors set"


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("report", nargs="?", type=Path, default=Path("coverage.json"),
                        help="coverage.py's JSON report (`coverage json`; default: coverage.json)")
    parser.add_argument("--root", type=Path, help="the consumer's checkout (default: the nearest pyproject.toml up from here)")


def run(args: argparse.Namespace) -> int:
    try:
        root = (args.root or config.find_root(Path.cwd())).resolve()
        limits = floors(config.load(root))
    except config.ConfigError as error:
        print(f"fleet coverage-floor: {error}", file=sys.stderr)
        return 2
    path: Path = args.report
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"fleet coverage-floor: can't read {path}: {error}", file=sys.stderr)
        return 2
    found = problems(report, limits)
    for problem in found:
        print(f"coverage: {problem}", file=sys.stderr)
    if not found:
        print(f"Coverage floor met: {summary(limits)}.")
    return 1 if found else 0
