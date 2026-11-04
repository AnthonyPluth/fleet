import argparse
import json
import sys
from typing import Any

LEVELS = ("error", "warning", "note", "none")
NOT_FINDINGS = {"pass", "notApplicable", "informational"}
SHOWN = 50


def escaped(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def rule_level(run: dict[str, Any], result: dict[str, Any]) -> str:
    rules = ((run.get("tool") or {}).get("driver") or {}).get("rules") or []
    index = result.get("ruleIndex")
    rule: Any = None
    if isinstance(index, int) and 0 <= index < len(rules):
        rule = rules[index]
    else:
        rule = next((r for r in rules if isinstance(r, dict) and r.get("id") == result.get("ruleId")), None)
    if isinstance(rule, dict):
        level = (rule.get("defaultConfiguration") or {}).get("level")
        if isinstance(level, str):
            return level
    return "warning"


def where(result: dict[str, Any]) -> str:
    for location in result.get("locations") or []:
        physical = (location or {}).get("physicalLocation") or {}
        uri = (physical.get("artifactLocation") or {}).get("uri")
        line = (physical.get("region") or {}).get("startLine")
        if uri:
            return f"{uri}:{line}" if line else str(uri)
    return "(no location)"


def findings(sarif: Any, levels: set[str]) -> list[str]:
    found = []
    runs = sarif.get("runs") if isinstance(sarif, dict) else None
    if not isinstance(runs, list):
        raise ValueError("no runs")
    for run in runs:
        if not isinstance(run, dict):
            raise ValueError("a run that isn't an object")
        for result in run.get("results") or []:
            if not isinstance(result, dict):
                raise ValueError("a result that isn't an object")
            if result.get("suppressions") or result.get("kind", "fail") in NOT_FINDINGS:
                continue
            level = result.get("level") or rule_level(run, result)
            if level in levels:
                found.append(f"{result.get('ruleId') or '(no rule)'} ({level}) at {where(result)}")
    return found


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sarif_findings.py")
    parser.add_argument("--level", action="append", choices=LEVELS, dest="levels")
    parser.add_argument("sarif")
    arguments = parser.parse_args(argv)
    levels = set(arguments.levels or LEVELS)
    at = "any level" if levels == set(LEVELS) else "level " + ", ".join(level for level in LEVELS if level in levels)
    name = escaped(arguments.sarif)
    try:
        with open(arguments.sarif, encoding="utf-8") as file:
            found = findings(json.load(file), levels)
    except (OSError, ValueError, AttributeError, TypeError) as error:
        print(f"::error::Couldn't read the findings in {name} ({escaped(type(error).__name__)}), so the job fails.")
        return 1
    if not found:
        print(f"No findings in {name} at {at}.")
        return 0
    for line in found[:SHOWN]:
        print(f"  - {escaped(line)}")
    if len(found) > SHOWN:
        print(f"  ... and {len(found) - SHOWN} more")
    print(f"::error::{len(found)} finding{'' if len(found) == 1 else 's'} in {name} at {at} (fail-on-findings).")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
