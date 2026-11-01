import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".claude/skills/verify-waypoint/references/feature-map.md"


def generate(root: Path = ROOT) -> tuple[str, list[str]]:
    verify = (root / "frontend/verify/verify.mjs").read_text()
    found = re.search(r"export const PAGES = \[([^\]]*)\]", verify)
    pages = re.findall(r'"([\w-]+)"', found.group(1)) if found else []
    problems = [] if pages else ["no PAGES found in frontend/verify/verify.mjs"]
    return "# Feature map\n\n" + "".join(f"- {page}\n" for page in pages), problems


def check(committed: str | None, generated: str) -> list[str]:
    name = OUT.relative_to(ROOT).as_posix()
    if committed is None:
        return [f"{name}: missing; run make user-feature-map and commit it"]
    if committed != generated:
        return [f"{name} isn't what tools/user_feature_map.py generates: run make user-feature-map and commit the result"]
    return []
