import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from fleet_tools import release

HELP = ("check fleet's own conventions (the FLEET_REF stamp on every reusable workflow, the self-test's calls, "
        "VERSION agreeing with CHANGELOG.md, plugin.json, pyproject.toml and the snippets in docs/, and no doc or "
        "workflow naming an event Actions doesn't have)")

WORKFLOWS = Path(".github/workflows")
SELF_TEST = "self-test.yml"
SELF_TEST_REF = "${{github.sha}}"
FLEET_REPOSITORY = "AnthonyPluth/fleet"
FLEET_CHECKOUT_REF = "${{inputs.fleet-ref||env.FLEET_REF}}"
VERSION_PATTERN = release.VERSION_PATTERN
NOT_ACTIONS_EVENTS = ("pull_request_review_thread",)
EVENT_NAME_FILES = ("README.md", "docs/*.md", ".github/workflows/*.y*ml", "plugins/**/*.md")
KEY_PATTERN = re.compile(r"""^(?P<q>["']?)(?P<key>[^"':#][^"':]*)(?P=q)\s*:(?:\s+(?P<value>.*))?$""")

Node: TypeAlias = "dict[str, Node] | list[Node] | str"
Trail: TypeAlias = tuple[str | int, ...]
Where: TypeAlias = dict[Trail, int]


PROPERTIES = re.compile(r"^(?:[&!]\S*[ \t]*)+")


class BlockScalar(str):
    pass


class Alias(str):
    pass


def without_properties(text: str) -> str:
    return PROPERTIES.sub("", text, count=1)


@dataclass(frozen=True)
class Line:
    indent: int
    text: str
    number: int = 0


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
        lines.append(Line(indent, text.strip(), index - 1))
        match = KEY_PATTERN.match(text.strip()) if not text.strip().startswith("- ") else None
        value = match.group("value") if match else None
        if value is None and text.strip().startswith("- "):
            item = KEY_PATTERN.match(without_properties(text.strip()[2:].strip()))
            value = item.group("value") if item else None
        if value is not None:
            value = without_properties(value)
        if value is not None and value[:1] in ("|", ">"):
            while index < len(raw) and (not raw[index].strip() or _indent(raw[index]) > indent):
                index += 1
    return lines


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip(" "))


def parse(source: str, where: Where | None = None) -> Node:
    lines = lines_of(source)
    if not lines:
        return {}
    node, _ = _block(lines, 0, lines[0].indent, (), where)
    return node


def _block(lines: list[Line], index: int, indent: int, path: Trail, where: Where | None) -> tuple[Node, int]:
    if lines[index].text.startswith("-"):
        return _sequence(lines, index, indent, path, where)
    return _mapping(lines, index, indent, path, where)


def _mapping(lines: list[Line], index: int, indent: int, path: Trail, where: Where | None) -> tuple[Node, int]:
    mapping: dict[str, Node] = {}
    while index < len(lines) and lines[index].indent == indent and not lines[index].text.startswith("- "):
        match = KEY_PATTERN.match(lines[index].text)
        explicit = lines[index].text == "?" or lines[index].text.startswith("? ")
        index += 1
        if explicit:
            mapping["?"] = ""
            while index < len(lines) and lines[index].indent > indent:
                index += 1
        if match is None:
            continue
        key, value = match.group("key").strip(), match.group("value")
        if value is not None:
            value = without_properties(value)
        here = (*path, key)
        if where is not None:
            where[here] = lines[index - 1].number
        if value is not None and value[:1] in ("|", ">"):
            mapping[key] = BlockScalar()
        elif value:
            mapping[key] = Alias(value) if value.startswith("*") else unquote(value)
            while index < len(lines) and lines[index].indent > indent:
                index += 1
        elif index < len(lines) and lines[index].indent > indent:
            mapping[key], index = _block(lines, index, lines[index].indent, here, where)
        elif index < len(lines) and lines[index].indent == indent and lines[index].text.startswith("- "):
            mapping[key], index = _sequence(lines, index, indent, here, where)
        else:
            mapping[key] = ""
    return mapping, index


def _sequence(lines: list[Line], index: int, indent: int, path: Trail, where: Where | None) -> tuple[Node, int]:
    items: list[Node] = []
    while index < len(lines) and lines[index].indent == indent and lines[index].text.startswith("-"):
        rest = without_properties(lines[index].text[1:].strip())
        index += 1
        here = (*path, len(items))
        if not rest:
            if index < len(lines) and lines[index].indent > indent:
                item, index = _block(lines, index, lines[index].indent, here, where)
                items.append(item)
            else:
                items.append("")
            continue
        if KEY_PATTERN.match(rest):
            inner = indent + 2
            nested = [Line(inner, rest, lines[index - 1].number)]
            while index < len(lines) and lines[index].indent > indent:
                nested.append(lines[index])
                index += 1
            item, _ = _mapping(nested, 0, inner, here, where)
            items.append(item)
        else:
            items.append(Alias(rest) if rest.startswith("*") else unquote(rest))
    return items, index


def flow(text: str) -> "Node | None":
    source = text.strip()
    found = _flow_value(source, 0)
    if found is None or source[found[1]:].strip():
        return None
    return found[0]


def _skip(source: str, index: int) -> int:
    while index < len(source) and source[index] in " \t":
        index += 1
    return index


def _flow_value(source: str, index: int) -> "tuple[Node, int] | None":
    index = _skip(source, index)
    if source[index:index + 1] == "{":
        return _flow_mapping(source, index + 1)
    if source[index:index + 1] == "[":
        return _flow_sequence(source, index + 1)
    return _flow_scalar(source, index)


def _flow_scalar(source: str, index: int) -> tuple[str, int] | None:
    index = _skip(source, index)
    quote = source[index:index + 1]
    if quote in ("'", '"'):
        end = index + 1
        while end < len(source):
            if source[end] == quote and not (quote == "'" and source[end + 1:end + 2] == "'"):
                return source[index + 1:end].replace("''", "'") if quote == "'" else source[index + 1:end], end + 1
            end += 2 if (source[end] == "\\" and quote == '"') or source[end] == quote else 1
        return None
    if quote in ("{", "["):
        return None
    end = index
    while end < len(source) and source[end] not in ",]}":
        if source[end] == ":" and source[end + 1:end + 2] in ("", " ", ",", "]", "}"):
            break
        end += 1
    return source[index:end].strip(), end


def _flow_mapping(source: str, index: int) -> "tuple[Node, int] | None":
    mapping: dict[str, Node] = {}
    while True:
        index = _skip(source, index)
        if source[index:index + 1] == "}":
            return mapping, index + 1
        key = _flow_scalar(source, index)
        if key is None or not key[0]:
            return None
        name, index = key
        index = _skip(source, index)
        value: Node = ""
        if source[index:index + 1] == ":":
            index = _skip(source, index + 1)
            if source[index:index + 1] not in (",", "}"):
                found = _flow_value(source, index)
                if found is None:
                    return None
                value, index = found
            index = _skip(source, index)
        mapping[name] = value
        if source[index:index + 1] == ",":
            index += 1
        elif source[index:index + 1] != "}":
            return None


def _flow_sequence(source: str, index: int) -> "tuple[Node, int] | None":
    items: list[Node] = []
    while True:
        index = _skip(source, index)
        if source[index:index + 1] == "]":
            return items, index + 1
        found = _flow_value(source, index)
        if found is None or found[0] == "":
            return None
        item, index = found
        items.append(item)
        index = _skip(source, index)
        if source[index:index + 1] == ",":
            index += 1
        elif source[index:index + 1] != "]":
            return None


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
    try:
        version = release.read_version(root)
    except release.ReleaseError as error:
        return [str(error)]
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
    errors.extend(check_event_names(root))
    errors.extend(release.check(root, version))
    return errors


def check_event_names(root: Path) -> list[str]:
    errors: list[str] = []
    for pattern in EVENT_NAME_FILES:
        for path in sorted(root.glob(pattern)):
            text = path.read_text(encoding="utf-8")
            for event in NOT_ACTIONS_EVENTS:
                if re.search(rf"(?<![\w-]){event}(?![\w-])", text):
                    errors.append(f"{path.relative_to(root).as_posix()}: names `{event}`, a webhook GitHub Actions can't "
                                  "subscribe to (a workflow naming it is invalid and never runs)")
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
