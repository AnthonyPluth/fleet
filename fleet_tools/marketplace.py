import json
import re
from pathlib import Path

SETTINGS = Path(".claude/settings.json")
REPOSITORY = "anthonypluth/fleet"
REF = re.compile(r'("ref"\s*:\s*)"(?:[^"\\]|\\.)*"')


def load(root: Path) -> tuple[str, object] | None:
    path = root / SETTINGS
    try:
        text = path.read_text(encoding="utf-8")
        return text, json.loads(text)
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def fleet_refs(settings: object) -> dict[str, object]:
    if not isinstance(settings, dict):
        return {}
    marketplaces = settings.get("extraKnownMarketplaces")
    if not isinstance(marketplaces, dict):
        return {}
    found = {}
    for name, entry in marketplaces.items():
        source = entry.get("source") if isinstance(entry, dict) else None
        if (isinstance(source, dict) and source.get("source") == "github"
                and isinstance(source.get("repo"), str) and source["repo"].lower() == REPOSITORY and "ref" in source):
            found[name] = source["ref"]
    return found


def stale(root: Path, version: str) -> dict[str, object]:
    loaded = load(root)
    if loaded is None:
        return {}
    return {name: ref for name, ref in fleet_refs(loaded[1]).items() if ref != version}


def moved(text: str, settings: object, name: str, version: str) -> str | None:
    assert isinstance(settings, dict)
    expected = json.loads(json.dumps(settings))
    expected["extraKnownMarketplaces"][name]["source"]["ref"] = version
    for match in REF.finditer(text):
        candidate = text[:match.start()] + match.group(1) + json.dumps(version) + text[match.end():]
        try:
            if json.loads(candidate) == expected:
                return candidate
        except ValueError:
            continue
    return None


def sync(root: Path, version: str) -> list[str]:
    loaded = load(root)
    if loaded is None:
        return []
    text, settings = loaded
    changes = []
    for name, ref in fleet_refs(settings).items():
        if ref == version:
            continue
        updated = moved(text, settings, name, version)
        if updated is None:
            changes.append(f"{SETTINGS.as_posix()}: can't move the {name} marketplace's ref from {ref!r} to {version}; "
                           "set it by hand")
            continue
        text, settings = updated, json.loads(updated)
        changes.append(f"{SETTINGS.as_posix()}: the {name} marketplace's ref {ref!r} -> {version!r}")
    if changes and text != loaded[0]:
        (root / SETTINGS).write_text(text, encoding="utf-8")
    return changes


def check(root: Path, version: str) -> list[str]:
    return [f"{SETTINGS.as_posix()}: fleet's marketplace `{name}` is at ref {ref!r}, but the fleet content is at {version}; "
            f"run `fleet sync --to {version}`, which moves it" for name, ref in stale(root, version).items()]
