import argparse
import os
import subprocess
import sys
from pathlib import Path

from fleet_tools import config
from fleet_tools.checks import (
    commits,
    extra,
    images,
    migrations,
    skills,
    tests_removed,
    workflows,
)
from fleet_tools.checks.context import Check, Commits, Context, git

HELP = ("the checks that replaced instructions: migrations, commit trailers, removed or skipped tests, workflow "
        "conventions, images, skills and generated files, and the app's own extra-checks")

CHECKS: list[Check] = [
    migrations.CHECK,
    workflows.CHECK,
    skills.CHECK,
    commits.CHECK,
    tests_removed.CHECK,
    images.CHECK,
    extra.CHECK,
]


def resolve(root: Path, revision_range: str) -> Commits:
    if ".." in revision_range:
        base_ref, head_ref = revision_range.split("..", 1)
        head = head_ref or "HEAD"
        base = git(root, "merge-base", base_ref or "HEAD", head).strip()
        log_range = f"{base_ref or 'HEAD'}..{head}"
    else:
        base, head = revision_range, "HEAD"
        log_range = f"{revision_range}..HEAD"
    return Commits(base, head, commits.commits(root, log_range))


def check(context: Context, checks: list[Check] | None = None) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    ran: list[str] = []
    for each in CHECKS if checks is None else checks:
        if not each.applies(context):
            continue
        ran.append(each.passed)
        problems += each.run(context)
    return problems, ran


def report(problems: list[str], ran: list[str]) -> int:
    for problem in problems:
        print(f"::error::{problem}" if "GITHUB_ACTIONS" in os.environ else problem, file=sys.stderr)
    if problems:
        return 1
    print(f"Fleet checks passed: {', '.join(ran)}." if ran else "Fleet checks passed: nothing is configured to check.")
    return 0


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--commits", metavar="BASE..HEAD", help="also check these commits (a pull request's): their "
                        "trailers, the tests they remove or skip and the images they add")
    parser.add_argument("--root", type=Path, help="the consumer's checkout (default: the nearest pyproject.toml up from here)")


def run(args: argparse.Namespace) -> int:
    try:
        root = (args.root or config.find_root(Path.cwd())).resolve()
        conf = config.load(root)
        found = resolve(root, args.commits) if args.commits else None
        problems, ran = check(Context(root, conf, found))
    except config.ConfigError as error:
        print(f"fleet checks: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print(f"fleet checks: `{' '.join(map(str, error.cmd))}` failed: {(error.stderr or '').strip()[:300]}",
              file=sys.stderr)
        return 2
    return report(problems, ran)
