import re
from pathlib import Path

from fleet_tools.checks.context import Check, Context

WORKFLOWS = ".github/workflows"
ACTIONS = ".github/actions"
USES = re.compile(r"^[ \t]*(?:-[ \t]*)?uses:[ \t]*([^\s#]+)(.*)$", re.MULTILINE)
PINNED = re.compile(r"@[0-9a-f]{40}$")
BASH_DEFAULT = re.compile(r"^defaults:[ \t]*\n[ \t]+run:[ \t]*\n(?:[ \t]{4,}\S.*\n)*?[ \t]{4,}shell:[ \t]*bash[ \t]*(?:#.*)?$",
                          re.MULTILINE)
EARLY_ENV = re.compile(r"^( {0}| {4})env:[ \t]*\n((?:\1[ ]{2,}\S.*\n)+)", re.MULTILINE)
STEP_ONLY_CONTEXT = re.compile(r"\$\{\{.*(?<![\w.])(runner|steps|job|env)\.")
GH_API_LIST = re.compile(r"\bgh api\b[^\n]*per_page=")
RUNS_ON = ("runs-on: ${{ fromJSON(vars.RUNS_ON != '' && github.actor != 'dependabot[bot]' && "
           "(!github.event.pull_request || github.event.pull_request.head.repo.full_name == github.repository && "
           "github.event.pull_request.user.login != 'dependabot[bot]') && vars.RUNS_ON || '\"ubuntu-latest\"') }}")
RUNS_ON_LINE = re.compile(r"^[ \t]*runs-on:.*$", re.MULTILINE)
HOSTED = re.compile(r"^runs-on: ubuntu-latest[ \t]+# hosted: \S")


def check_workflow(name: str, text: str, workflow: bool = True) -> list[str]:
    problems = []
    if workflow and not BASH_DEFAULT.search(text):
        problems.append(f"{name}: no top-level `defaults: run: shell: bash` (steps then run without -o pipefail)")
    for found in USES.finditer(text):
        target, rest = found.group(1), found.group(2)
        if target.startswith(("./", "docker://")):
            continue
        if not PINNED.search(target):
            problems.append(f"{name}: {target} isn't pinned by its commit SHA")
        elif not re.search(r"#\s*v?\d", rest):
            problems.append(f"{name}: {target} has no version in a comment (`# v1.2.3`)")
    for block in EARLY_ENV.finditer(text if workflow else ""):
        for line in block.group(2).splitlines():
            if context := STEP_ONLY_CONTEXT.search(line):
                problems.append(f"{name}: `{context.group(1)}` isn't available in a workflow's or job's `env:` (the file "
                                f"would be invalid and never run); set it in a step's `env:`: {line.strip()[:80]}")
    for line in text.splitlines():
        if GH_API_LIST.search(line) and "--paginate" not in line:
            problems.append(f"{name}: `gh api ... per_page=` without --paginate reads only the first page: {line.strip()[:80]}")
    for found in RUNS_ON_LINE.finditer(text):
        line = found.group(0).strip()
        if line != RUNS_ON and not HOSTED.match(line):
            problems.append(f"{name}: `{line[:60]}` should be fleet's RUNS_ON line (fleet_tools/checks/workflows.py), "
                            "or `runs-on: ubuntu-latest   # hosted: <why it needs GitHub's runners>`")
    return problems


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
