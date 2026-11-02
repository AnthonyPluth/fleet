import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from fleet_tools import release

HELP = ("check fleet's own conventions (the FLEET_REF stamp on every reusable workflow, the self-test's calls, and "
        "VERSION agreeing with CHANGELOG.md, plugin.json, pyproject.toml and the snippets in docs/)")

WORKFLOWS = Path(".github/workflows")
SELF_TEST = "self-test.yml"
SELF_TEST_REF = "${{github.sha}}"
FLEET_REPOSITORY = "AnthonyPluth/fleet"
FLEET_CHECKOUT_REF = "${{inputs.fleet-ref||env.FLEET_REF}}"
VERSION_PATTERN = release.VERSION_PATTERN
KEY_PATTERN = re.compile(r"""^(?P<q>["']?)(?P<key>[^"':#][^"':]*)(?P=q)\s*:(?:\s+(?P<value>.*))?$""")

Node: TypeAlias = "dict[str, Node] | list[Node] | str"


@dataclass(frozen=True)
class Line:
    indent: int
    text: str


def strip_comment(text: str) -> str:
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'" and (index == 0 or text[index - 1] in " :[{,-"):
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
    return text.rstrip()


def unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def lines_of(source: str) -> list[Line]:
    lines = []
    raw = source.splitlines()
    index = 0
    while index < len(raw):
        line = raw[index]
        index += 1
        text = strip_comment(line)
        if not text.strip() or text.strip() == "---":
            continue
        indent = len(text) - len(text.lstrip(" "))
        lines.append(Line(indent, text.strip()))
        match = KEY_PATTERN.match(text.strip()) if not text.strip().startswith("- ") else None
        value = match.group("value") if match else None
        if value is None and text.strip().startswith("- "):
            item = KEY_PATTERN.match(text.strip()[2:].strip())
            value = item.group("value") if item else None
        if value is not None and value[:1] in "|>":
            while index < len(raw) and (not raw[index].strip() or _indent(raw[index]) > indent):
                index += 1
    return lines


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip(" "))


def parse(source: str) -> Node:
    lines = lines_of(source)
    if not lines:
        return {}
    node, _ = _block(lines, 0, lines[0].indent)
    return node


def _block(lines: list[Line], index: int, indent: int) -> tuple[Node, int]:
    if lines[index].text.startswith("-"):
        return _sequence(lines, index, indent)
    return _mapping(lines, index, indent)


def _mapping(lines: list[Line], index: int, indent: int) -> tuple[Node, int]:
    mapping: dict[str, Node] = {}
    while index < len(lines) and lines[index].indent == indent and not lines[index].text.startswith("- "):
        match = KEY_PATTERN.match(lines[index].text)
        index += 1
        if match is None:
            continue
        key, value = match.group("key").strip(), match.group("value")
        if value is not None and value[:1] not in "|>":
            mapping[key] = unquote(value)
            while index < len(lines) and lines[index].indent > indent:
                index += 1
        elif index < len(lines) and lines[index].indent > indent:
            mapping[key], index = _block(lines, index, lines[index].indent)
        elif index < len(lines) and lines[index].indent == indent and lines[index].text.startswith("- "):
            mapping[key], index = _sequence(lines, index, indent)
        else:
            mapping[key] = ""
    return mapping, index


def _sequence(lines: list[Line], index: int, indent: int) -> tuple[Node, int]:
    items: list[Node] = []
    while index < len(lines) and lines[index].indent == indent and lines[index].text.startswith("-"):
        rest = lines[index].text[1:].strip()
        index += 1
        if not rest:
            if index < len(lines) and lines[index].indent > indent:
                item, index = _block(lines, index, lines[index].indent)
                items.append(item)
            else:
                items.append("")
            continue
        if KEY_PATTERN.match(rest):
            inner = indent + 2
            nested = [Line(inner, rest)]
            while index < len(lines) and lines[index].indent > indent:
                nested.append(lines[index])
                index += 1
            item, _ = _mapping(nested, 0, inner)
            items.append(item)
        else:
            items.append(unquote(rest))
    return items, index


def is_reusable(workflow: Node) -> bool:
    if not isinstance(workflow, dict):
        return False
    trigger = workflow.get("on", "")
    if isinstance(trigger, dict):
        return "workflow_call" in trigger
    if isinstance(trigger, list):
        return "workflow_call" in trigger
    return "workflow_call" in re.findall(r"[A-Za-z_]+", trigger)


def expected_ref(version: str) -> str:
    return "main" if version.startswith("0.") else f"v{version}"


def check(root: Path) -> list[str]:
    errors: list[str] = []
    version_file = root / "VERSION"
    if not version_file.is_file():
        return ["VERSION: missing"]
    version = version_file.read_text(encoding="utf-8").strip()
    if not VERSION_PATTERN.fullmatch(version):
        return [f"VERSION: {version!r} isn't a version (MAJOR.MINOR.PATCH, ASCII digits, no leading zeros)"]
    expected = expected_ref(version)
    directory = root / WORKFLOWS
    files = sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")])
    for path in files:
        name = path.relative_to(root).as_posix()
        workflow = parse(path.read_text(encoding="utf-8"))
        if not is_reusable(workflow) or not isinstance(workflow, dict):
            continue
        env = workflow.get("env")
        ref = env.get("FLEET_REF") if isinstance(env, dict) else None
        if not isinstance(ref, str) or not ref:
            errors.append(f"{name}: a reusable workflow needs a top-level `env: FLEET_REF: {expected}`")
        elif ref != expected:
            errors.append(f"{name}: FLEET_REF is {ref!r}, but VERSION {version} needs {expected!r}")
        errors.extend(check_fleet_checkouts(name, workflow))
    self_test = directory / SELF_TEST
    if not self_test.is_file():
        errors.append(f"{(WORKFLOWS / SELF_TEST).as_posix()}: missing")
    else:
        errors.extend(check_self_test(self_test.relative_to(root).as_posix(), parse(self_test.read_text("utf-8"))))
    errors.extend(release.check(root, version))
    return errors


def check_fleet_checkouts(name: str, workflow: Node) -> list[str]:
    errors: list[str] = []
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    if not isinstance(jobs, dict):
        return errors
    for job_name, job in jobs.items():
        steps = job.get("steps") if isinstance(job, dict) else None
        for step in steps if isinstance(steps, list) else []:
            if not isinstance(step, dict) or not str(step.get("uses", "")).startswith("actions/checkout@"):
                continue
            inputs = step.get("with")
            if not isinstance(inputs, dict) or inputs.get("repository") != FLEET_REPOSITORY:
                continue
            ref = inputs.get("ref")
            if not isinstance(ref, str) or re.sub(r"\s+", "", ref) != FLEET_CHECKOUT_REF:
                errors.append(f"{name}: job {job_name} checks out {FLEET_REPOSITORY} at {ref!r}, not "
                              "`${{ inputs.fleet-ref || env.FLEET_REF }}`: fleet's own files come from the pinned ref only")
    return errors


def check_self_test(name: str, workflow: Node) -> list[str]:
    errors: list[str] = []
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    if not isinstance(jobs, dict):
        return [f"{name}: no jobs"]
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses")
        if not isinstance(uses, str) or not uses.startswith("./.github/workflows/"):
            continue
        inputs = job.get("with")
        ref = inputs.get("fleet-ref") if isinstance(inputs, dict) else None
        if not isinstance(ref, str) or re.sub(r"\s+", "", ref) != SELF_TEST_REF:
            errors.append(f"{name}: job {job_name} calls {uses} without `fleet-ref: ${{{{ github.sha }}}}`")
    return errors


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="fleet's checkout (default: here)")


def run(args: argparse.Namespace) -> int:
    root: Path = args.root
    errors = check(root)
    for error in errors:
        print(f"fleet self-check: {error}", file=sys.stderr)
    if errors:
        return 1
    print("fleet self-check: ok")
    return 0
