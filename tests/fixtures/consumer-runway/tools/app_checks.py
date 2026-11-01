from pathlib import Path
from typing import Any


def a11y_baseline(context: Any) -> list[str]:
    root: Path = context.root
    baseline = root / "frontend/verify/a11y-baseline.txt"
    problems = []
    for number, raw in enumerate(baseline.read_text().splitlines() if baseline.exists() else [], 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(" | ")]
        if len(parts) < 3 or not all(parts):
            problems.append(f"frontend/verify/a11y-baseline.txt line {number}: expected `<page or flow> @ <width> | <finding> | <reason>`")
    return problems


CHECKS = [a11y_baseline]
