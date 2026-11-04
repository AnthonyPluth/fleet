import re
from pathlib import Path

from fleet_tools.checks.context import Check, Context
from fleet_tools.self_check import (
    BlockScalar,
    Node,
    Unreadable,
    Where,
    flow,
    lines_of,
    parse,
    strip_comment,
    unreadable,
)

WORKFLOWS = ".github/workflows"
ACTIONS = ".github/actions"
USES = re.compile(r"^[ \t]*(?:-[ \t]*)?uses:[ \t]*([^\s#]+)(.*)$", re.MULTILINE)
PINNED = re.compile(r"@[0-9a-f]{40}$")
BASH_DEFAULT = re.compile(r"^defaults:[ \t]*\n[ \t]+run:[ \t]*\n(?:[ \t]{4,}\S.*\n)*?[ \t]{4,}shell:[ \t]*bash[ \t]*(?:#.*)?$",
                          re.MULTILINE)
EARLY_ENV = re.compile(r"^( {0}| {4})env:[ \t]*\n((?:\1[ ]{2,}\S.*\n)+)", re.MULTILINE)
STEP_ONLY_CONTEXT = re.compile(r"\$\{\{.*(?<![\w.])(runner|steps|job|env)\.")
BLOCK_HEADER = re.compile(r"[|>][-+]?$")
GH_API_LIST = re.compile(r"\bgh api\b[^\n]*per_page=")
RUNS_ON = ("runs-on: ${{ fromJSON(vars.RUNS_ON != '' && github.actor != 'dependabot[bot]' && "
           "(!github.event.pull_request || github.event.pull_request.head.repo.full_name == github.repository && "
           "github.event.pull_request.user.login != 'dependabot[bot]') && vars.RUNS_ON || '\"ubuntu-latest\"') }}")
HOSTED = re.compile(r"^runs-on: (?:ubuntu-latest|macos-\d+)[ \t]+# hosted: \S")
EVENTS = frozenset({
    "branch_protection_rule", "check_run", "check_suite", "create", "delete", "deployment", "deployment_status",
    "discussion", "discussion_comment", "fork", "gollum", "image_version", "issue_comment", "issues", "label",
    "merge_group", "milestone", "page_build", "public", "pull_request", "pull_request_review",
    "pull_request_review_comment", "pull_request_target", "push", "registry_package", "release", "repository_dispatch",
    "schedule", "status", "watch", "workflow_call", "workflow_dispatch", "workflow_run",
})


def check_workflow(name: str, text: str, workflow: bool = True) -> list[str]:
    problems = []
    where: Where = {}
    try:
        tree = parse(text, where)
    except Unreadable as error:
        return [unreadable(name, error)]
    structure = without_block_scalars(text, tree, where)
    if workflow and not only_calls(tree) and not BASH_DEFAULT.search(structure):
        problems.append(f"{name}: no top-level `defaults: run: shell: bash` (steps then run without -o pipefail)")
    for found in USES.finditer(structure):
        target, rest = found.group(1), found.group(2)
        if target.startswith(("./", "docker://")):
            continue
        if target[:1] in ("|", ">"):
            problems.append(f"{name}: a `uses:` written as a block scalar can't be read (write it on one line)")
            continue
        if not PINNED.search(target):
            problems.append(f"{name}: {target} isn't pinned by its commit SHA")
        elif not re.search(r"#\s*v?\d", rest):
            problems.append(f"{name}: {target} has no version in a comment (`# v1.2.3`)")
    for block in EARLY_ENV.finditer(structure if workflow else ""):
        for line in block.group(2).splitlines():
            if context := STEP_ONLY_CONTEXT.search(line):
                problems.append(f"{name}: `{context.group(1)}` isn't available in a workflow's or job's `env:` (the file "
                                f"would be invalid and never run); set it in a step's `env:`: {line.strip()[:80]}")
    for line in text.splitlines():
        if GH_API_LIST.search(line) and "--paginate" not in line:
            problems.append(f"{name}: `gh api ... per_page=` without --paginate reads only the first page: {line.strip()[:80]}")
    named = events(tree) if workflow else []
    if named is None:
        problems.append(f"{name}: the events under `on:` can't be read (write them as a block mapping or a list)")
    for event in named or []:
        if event not in EVENTS:
            problems.append(f"{name}: `{event}` isn't a GitHub Actions event (the file would be invalid and never run)")
    problems += check_runners(name, text, tree, where)
    return problems


def without_block_scalars(text: str, tree: Node, where: Where) -> str:
    source = text.splitlines(keepends=True)
    placed = {line.number for line in lines_of(text)}
    for trail, start in where.items():
        if not isinstance(at(tree, trail), BlockScalar) or trail[:-1] == ("env",) or (
                len(trail) == 4 and trail[0] == "jobs" and trail[2] == "env"):
            continue
        header = source[start]
        if not BLOCK_HEADER.search(strip_comment(header)):
            continue
        key = header.lstrip(" ")
        column = len(header) - len(key)
        dash = column
        while key.startswith("- "):
            dash = column
            column += len(key) - len(key[1:].lstrip(" "))
            key = key[1:].lstrip(" ")
        if isinstance(trail[-1], int):
            column = dash
        index = start + 1
        while index < len(source) and not source[index].strip():
            index += 1
        content = len(source[index]) - len(source[index].lstrip(" ")) if index < len(source) else 0
        if content <= column:
            continue
        index = start + 1
        while index < len(source) and index not in placed:
            line = source[index]
            if line.strip():
                if len(line) - len(line.lstrip(" ")) < content:
                    break
                source[index] = line[:content] + "#" + line[len(line.rstrip("\r\n")):]
            index += 1
    return "".join(source)


def at(node: Node, trail: tuple[str | int, ...]) -> "Node | None":
    current: Node | None = node
    for step in trail:
        if isinstance(current, dict) and isinstance(step, str):
            current = current.get(step)
        elif isinstance(current, list) and isinstance(step, int):
            current = current[step] if step < len(current) else None
        else:
            return None
    return current


def check_runners(name: str, text: str, tree: Node, where: Where) -> list[str]:
    jobs = tree.get("jobs") if isinstance(tree, dict) else None
    if isinstance(jobs, str) and (jobs or isinstance(jobs, BlockScalar)):
        return [f"{name}: the jobs under `jobs:` can't be read (write them as a block mapping)"]
    if not isinstance(jobs, dict):
        return []
    source = text.splitlines()
    problems = []
    for job_id, job in jobs.items():
        if not isinstance(job, dict) or "runs-on" not in job:
            continue
        raw = source[where[("jobs", job_id, "runs-on")]].strip()
        runner = job["runs-on"]
        line = raw
        if isinstance(runner, str) and not isinstance(runner, BlockScalar):
            comment = raw[len(strip_comment(raw)):]
            line = f'runs-on: "{runner}"{comment}' if re.search(r"\s#", runner) else f"runs-on: {runner}{comment}"
        if line != RUNS_ON and not HOSTED.match(line):
            problems.append(f"{name}: `{line[:60]}` should be fleet's RUNS_ON line (fleet_tools/checks/workflows.py), "
                            "or `runs-on: ubuntu-latest   # hosted: <why it needs GitHub's runners>` (a pinned `macos-<number>` runner may "
                            "take the same comment, for what only macOS can build)")
    return problems


def only_calls(workflow: Node) -> bool:
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    if not isinstance(jobs, dict) or not jobs:
        return False
    return all(isinstance(job, dict) and isinstance(job.get("uses"), str) and job["uses"] and "steps" not in job
               for job in jobs.values())


def events(workflow: Node) -> list[str] | None:
    trigger = workflow.get("on", "") if isinstance(workflow, dict) else ""
    if isinstance(trigger, BlockScalar):
        return None
    parsed = flow(trigger) if isinstance(trigger, str) and trigger[:1] in ("{", "[") else trigger
    if isinstance(parsed, dict):
        return list(parsed) or None
    if isinstance(parsed, list):
        names = [item for item in parsed if isinstance(item, str)]
        return names if names and len(names) == len(parsed) else None
    return None if parsed is None else [parsed] if parsed else []


def check_workflows(root: Path) -> list[str]:
    problems = []
    for path in sorted((root / WORKFLOWS).glob("*.y*ml")):
        problems += check_workflow(path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"))
    for path in sorted((root / ACTIONS).glob("*/action.y*ml")):
        problems += check_workflow(path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"), workflow=False)
    return problems


def applies(context: Context) -> bool:
    return (context.root / WORKFLOWS).is_dir() or (context.root / ACTIONS).is_dir()


def run(context: Context) -> list[str]:
    return check_workflows(context.root)


CHECK = Check("workflows", "workflow conventions", applies, run)
