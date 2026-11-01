import json
import re
from pathlib import Path
from typing import Any

from fleet_tools.checks.context import Check, Context, git, show
from fleet_tools.config import ConfigError

IMAGE = re.compile(r"\.(png|jpe?g|gif|webp|avif|ico|heic|bmp|tiff?)$", re.IGNORECASE)
NAMES = ("pages", "pages-and-flows")
DEFAULT_WIDTHS = ("phone", "desktop")
DEFAULT_VERIFY = "frontend/verify/verify.mjs"
DEFAULT_FLOWS = "frontend/verify/flows"


def verify_list(text: str, name: str) -> list[str]:
    found = re.search(r"export const " + name + r" = \[([^\]]*)\]", text)
    return re.findall(r"""["']([\w-]+)["']""", found.group(1)) if found else []


def widths_of(text: str) -> list[str]:
    return verify_list(text, "DEFAULT_VIEWPORTS") or list(DEFAULT_WIDTHS)


def page_images(text: str) -> set[str]:
    return {f"{page}-{width}{kind}.png" for page in verify_list(text, "PAGES") for width in widths_of(text)
            for kind in ("", "-hero")}


def page_and_flow_images(text: str, flows: list[dict[str, Any]]) -> set[str]:
    widths = widths_of(text)
    top = re.search(r"""export const TOP = ["'](\w+)["']""", text)
    suffixes = ("", f"-{top.group(1) if top else 'top'}")
    names = {f"{page}{suffix}-{w}.png" for page in verify_list(text, "PAGES") for w in widths for suffix in suffixes}
    for flow in flows:
        name = flow.get("name")
        if not isinstance(name, str):
            continue
        steps = [s["screenshot"] for s in flow.get("steps", []) if isinstance(s, dict) and isinstance(s.get("screenshot"), str)]
        for w in flow.get("viewports") or widths:
            names |= {f"flow-{name}-{w}.png", *(f"flow-{name}-{step}-{w}.png" for step in steps)}
    return names


def flows_at(root: Path, rev: str, directory: str) -> list[dict[str, Any]]:
    flows = []
    for path in sorted(git(root, "ls-tree", "-r", "--name-only", rev, "--", directory).splitlines()):
        if not path.endswith(".json"):
            continue
        text = show(root, rev, path)
        if text is None:
            continue
        try:
            flow = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(flow, dict):
            flows.append(flow)
    return flows


def added_images(root: Path, base: str, head: str = "HEAD") -> list[str]:
    out = git(root, "diff", "--relative", "--name-only", "-z", "--diff-filter=AMR", base, head)
    return [path for path in out.split("\0") if IMAGE.search(path)]


def describe(names: str) -> str:
    if names == "pages":
        return ("<page>-<width>.png and its first screen <page>-<width>-hero.png, for each page in verify.mjs's PAGES at its "
                "DEFAULT_VIEWPORTS")
    return ("<page>-<width>.png and its first screen <page>-top-<width>.png for each page, and flow-<name>-<width>.png and "
            "flow-<name>-<step>-<width>.png for each flow and its screenshot steps")


def check_images(paths: list[str], made: set[str], screenshots: str | None, homes: tuple[str, ...],
                 files: tuple[str, ...], names: str = "pages") -> list[str]:
    allowed = [f"{screenshots} ({describe(names)})"] if screenshots else []
    allowed += [*(f"anything in {home}" for home in homes), *files]
    where = "; ".join(allowed) if allowed else "nowhere"
    problems = []
    for path in paths:
        if path in files or path.startswith(homes):
            continue
        if screenshots and path.startswith(screenshots) and path[len(screenshots):] in made:
            continue
        problems.append(f"{path}: the only images a change commits are the ones make verify writes and those that belong "
                        f"where they are ({where}); they are the visual regression baselines, so show a before and after "
                        "through those images' diff, not a hand-made file")
    return problems


def directory(value: str) -> str:
    return value.rstrip("/") + "/"


def applies(context: Context) -> bool:
    conf = context.config
    return context.commits is not None and any(conf.has(key) for key in ("image-homes", "image-files", "screenshots"))


def run(context: Context) -> list[str]:
    commits = context.commits
    assert commits is not None
    conf = context.config
    homes = tuple(directory(home) for home in conf.strings("image-homes"))
    files = tuple(conf.strings("image-files"))
    shots = conf.section("screenshots")
    screenshots = None
    made: set[str] = set()
    names = "pages"
    if shots:
        if not isinstance(shots.get("dir"), str):
            raise ConfigError("[tool.fleet.screenshots] dir must be a string")
        names = shots.get("names", "pages")
        if names not in NAMES:
            raise ConfigError(f"[tool.fleet.screenshots] names must be one of {', '.join(NAMES)} (it is {names!r})")
        screenshots = directory(shots["dir"])
        verify_text = show(context.root, commits.head, str(shots.get("verify", DEFAULT_VERIFY))) or ""
        if names == "pages":
            made = page_images(verify_text)
        else:
            made = page_and_flow_images(verify_text, flows_at(context.root, commits.head,
                                                              str(shots.get("flows", DEFAULT_FLOWS))))
    return check_images(added_images(context.root, commits.base, commits.head), made, screenshots, homes, files, names)


CHECK = Check("images", "images only where make verify puts them", applies, run)
