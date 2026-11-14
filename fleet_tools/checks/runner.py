import argparse
import json
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
    verification,
    verification_guides,
    workflows,
)
from fleet_tools.checks.context import (
    Advisory,
    Check,
    Commits,
    Context,
    PullRequest,
    git,
)

HELP = ("the checks that replaced instructions: migrations, commit trailers, removed or skipped tests, workflow "
        "conventions, images, skills and generated files, the verification record in a pull request's description, the "
        "verification guides and the app's own extra-checks")

CHECKS: list[Check] = [
    migrations.CHECK,
    workflows.CHECK,
    skills.CHECK,
    skills.HOMES,
    commits.CHECK,
    tests_removed.CHECK,
    images.CHECK,
    verification.CHECK,
    verification_guides.CHECK,
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


def pull_request_of(path: Path) -> PullRequest | None:
    try:
        event = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise config.ConfigError(f"--pr-event {path}: isn't a readable JSON event payload ({type(error).__name__})") from None
    found = event.get("pull_request") if isinstance(event, dict) else None
    if not isinstance(found, dict):
        return None
    user = found.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    body = found.get("body")
    return PullRequest(body if isinstance(body, str) else "", login if isinstance(login, str) else "",
                       found.get("draft") is True)


def check(context: Context, checks: list[Check] | None = None) -> tuple[list[str], list[str], list[str]]:
    problems: list[str] = []
    warnings: list[str] = []
    ran: list[str] = []
    for each in CHECKS if checks is None else checks:
        if not each.applies(context):
            continue
        ran.append(each.passed)
        for found in each.run(context):
            (warnings if isinstance(found, Advisory) else problems).append(str(found))
    return problems, warnings, ran


def report(problems: list[str], ran: list[str], warnings: list[str] | None = None) -> int:
    actions = "GITHUB_ACTIONS" in os.environ
    for warning in warnings or []:
        print(f"::warning::{warning}" if actions else f"warning: {warning}", file=sys.stderr)
    for problem in problems:
        print(f"::error::{problem}" if actions else problem, file=sys.stderr)
    if problems:
        return 1
    print(f"Fleet checks passed: {', '.join(ran)}." if ran else "Fleet checks passed: nothing is configured to check.")
    return 0


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--commits", metavar="BASE..HEAD", help="also check these commits (a pull request's): their "
                        "trailers, the tests they remove or skip and the images they add")
    parser.add_argument("--pr-event", metavar="PATH", type=Path, help="a pull request event payload ($GITHUB_EVENT_PATH on "
                        "Actions): its description's verification record is checked where [tool.fleet.verification] is set")
    parser.add_argument("--root", type=Path, help="the consumer's checkout (default: the nearest pyproject.toml up from here)")
    parser.add_argument("--only", action="append", metavar="CHECK", choices=[each.name for each in CHECKS],
                        help="run only this check (repeatable): the merge gate runs `--only verification` on a draft's "
                        "description as a ready pull request's before marking it ready")


def run(args: argparse.Namespace) -> int:
    try:
        root = (args.root or config.find_root(Path.cwd())).resolve()
        conf = config.load(root)
        found = resolve(root, args.commits) if args.commits else None
        pull_request = pull_request_of(args.pr_event) if args.pr_event else None
        only = [each for each in CHECKS if each.name in args.only] if args.only else None
        problems, warnings, ran = check(Context(root, conf, found, pull_request), only)
    except config.ConfigError as error:
        print(f"fleet checks: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print(f"fleet checks: `{' '.join(map(str, error.cmd))}` failed: {(error.stderr or '').strip()[:300]}",
              file=sys.stderr)
        return 2
    return report(problems, ran, warnings)
