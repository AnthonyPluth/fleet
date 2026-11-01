from pathlib import Path
from typing import Any


def parser_fixtures(context: Any) -> list[str]:
    root: Path = context.root
    parsers = root / "waypoint/domain/mail/parsers"
    problems = []
    for parser in sorted(parsers.glob("*.py")):
        if parser.stem.startswith("_"):
            continue
        if not (root / "tests/fixtures/mail" / parser.stem / "booking.eml").is_file():
            problems.append(f"waypoint/domain/mail/parsers/{parser.name}: no fixture tests/fixtures/mail/{parser.stem}/booking.eml")
    return problems


CHECKS = [parser_fixtures]
