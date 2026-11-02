import base64
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from fleet_tools.agent_review import defuse

Json = dict[str, Any]

SCHEMA = 1
PREFIX = "<!-- loop-stats "
LINE = re.compile(r"<!-- loop-stats (?P<data>[A-Za-z0-9+/]{1,4096}={0,2}) -->")
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MAX_LINE = 2048
MAX_STATS = 1_000_000
MAX_COUNT = 10**9
MAX_MEAN = 10**6
MAX_DAYS = 36500
KEYS = {"schema", "date", "days", "values"}
COUNT = "count"
SHARE = "share"
MEAN = "mean"


@dataclass(frozen=True)
class Measure:
    key: str
    label: str
    kind: str
    path: tuple[str, ...]


HEADLINE = (
    Measure("merged", "Merged pull requests", COUNT, ("reviews", "merged")),
    Measure("agent_review", "… with an Agent review", COUNT, ("reviews", "with_agent_review")),
    Measure("second_review", "… with a Second review", COUNT, ("reviews", "with_second_review")),
    Measure("findings", "Findings raised on them", COUNT, ("reviews", "findings", "raised")),
    Measure("resolved_share", "… resolved", SHARE, ("reviews", "findings", "resolved_share")),
    Measure("dismissed_share", "… dismissed", SHARE, ("reviews", "findings", "dismissed_share")),
    Measure("open_at_merge_share", "… open at merge", SHARE, ("reviews", "findings", "open_at_merge_share")),
    Measure("records", "Merged pull requests with a verification record", COUNT, ("verification", "with_record")),
    Measure("mean_not_verified", "`not verified` per record (mean)", MEAN, ("verification", "mean_not_verified")),
    Measure("uncovered_paths", "Changed paths no verification guide covers", COUNT, ("verification", "uncovered_paths")),
    Measure("rows", "Rows in this report", COUNT, ("report", "rows")),
    Measure("ticked", "… ticked", COUNT, ("report", "ticked")),
    Measure("tasks_opened", "Issues opened from rows", COUNT, ("report", "tasks_opened")),
    Measure("tasks_closed", "… closed in the window", COUNT, ("report", "tasks_closed_in_window")),
    Measure("harness_commits", "Commits changing the harness", COUNT, ("harness", "commits", "total")),
    Measure("checked", "Mistakes items newly `checked:`", COUNT, ("harness", "checked_in_window")),
)
BY_KEY = {measure.key: measure for measure in HEADLINE}


def valid_value(kind: str, value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if kind == COUNT:
        return isinstance(value, int) and 0 <= value <= MAX_COUNT
    return math.isfinite(value) and 0 <= value <= (1 if kind == SHARE else MAX_MEAN)


def valid_day(text: object) -> bool:
    if not isinstance(text, str) or not DAY.fullmatch(text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def valid(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != KEYS:
        return False
    schema, days, values = value["schema"], value["days"], value["values"]
    if not isinstance(schema, int) or isinstance(schema, bool) or schema != SCHEMA:
        return False
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MAX_DAYS or not valid_day(value["date"]):
        return False
    if not isinstance(values, dict) or not set(values) <= set(BY_KEY):
        return False
    return all(valid_value(BY_KEY[key].kind, item) for key, item in values.items())


def pick(stats: Json, path: tuple[str, ...]) -> object:
    node: object = stats
    for name in path:
        if not isinstance(node, dict):
            return None
        node = node.get(name)
    return node


def headline(stats: Json) -> Json | None:
    generated = stats.get("generated")
    summary = {"schema": SCHEMA, "date": generated[:10] if isinstance(generated, str) else "", "days": stats.get("days"),
               "values": {measure.key: pick(stats, measure.path) for measure in HEADLINE}}
    return summary if valid(summary) else None


def encode(summary: Json) -> str:
    data = json.dumps(summary, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"{PREFIX}{base64.b64encode(data.encode('ascii')).decode('ascii')} -->"


def decode(line: str) -> Json | None:
    if len(line.encode("utf-8", "replace")) > MAX_LINE:
        return None
    found = LINE.fullmatch(line)
    if not found:
        return None
    try:
        value = json.loads(base64.b64decode(found.group("data"), validate=True))
    except (ValueError, TypeError, RecursionError):
        return None
    return value if valid(value) else None


def previous(body: str, marker: str) -> Json | None:
    lines = body.splitlines()
    if len(lines) < 2 or lines[0] != marker:
        return None
    return decode(lines[1])


def read(path: Path) -> tuple[Json | None, str]:
    try:
        if path.stat().st_size > MAX_STATS:
            return None, f"the file is over {MAX_STATS:,} bytes"
        stats = json.loads(path.read_bytes().decode("utf-8"))
    except OSError as error:
        return None, f"it can't be read ({type(error).__name__})"
    except (ValueError, RecursionError):
        return None, "it isn't UTF-8 JSON"
    if not isinstance(stats, dict) or stats.get("schema") != SCHEMA or isinstance(stats.get("schema"), bool):
        return None, f"it isn't a schema {SCHEMA} loop-stats file"
    summary = headline(stats)
    if summary is None:
        return None, "a headline number is missing or out of range"
    return summary, ""


def shown(kind: str, value: object) -> str:
    if value is None or not valid_value(kind, value):
        return "n/a"
    assert isinstance(value, (int, float))
    if kind == SHARE:
        return f"{round(value * 100)}%"
    if kind == MEAN:
        return f"{value:.2f}"
    return str(value)


def section(current: Json | None, earlier: Json | None, why: str = "") -> list[str]:
    other = earlier if earlier is not None and current is not None and earlier["days"] != current["days"] else None
    if other is not None:
        earlier = None
    days = (current or earlier or {}).get("days", "?")
    lines = ["### Loop health", "",
             f"Whether review, verification and this report are improving the harness, from fleet's `loop-stats` over the "
             f"last {days} days: the run's job summary and its `loop-stats.json` artifact have the full numbers. `n/a`: "
             "nothing to divide by, or not read.", ""]
    columns = [summary for summary in (earlier, current) if summary is not None]
    if not columns:
        return [*lines, f"This run's numbers couldn't be read ({defuse(why, 200).replace('<', '&lt;')}), and this issue holds no earlier ones."]
    header = ["Measure"]
    if earlier is not None:
        header.append(f"Last run ({earlier['date']})")
    if current is not None:
        header.append(f"This run ({current['date']})")
    lines += ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    for measure in HEADLINE:
        cells = [shown(measure.kind, summary["values"].get(measure.key)) for summary in columns]
        lines.append(f"| {measure.label} | " + " | ".join(cells) + " |")
    if current is None:
        lines += ["", f"This run's numbers couldn't be read ({defuse(why, 200).replace('<', '&lt;')}); the last run's are kept."]
    elif other is not None:
        lines += ["", f"The last run ({other['date']}) covered {other['days']} days, not {days}, so it isn't compared: the next run "
                      "with the same window shows these beside its own."]
    elif earlier is None:
        lines += ["", "No earlier numbers in this issue: the next run shows these beside its own."]
    return lines
