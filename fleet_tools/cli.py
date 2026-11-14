import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from fleet_tools import (
    agent_review,
    coverage_floor,
    drift,
    identity,
    loop_stats,
    mining_tasks,
    next_version,
    no_comments,
    release,
    review_mine,
    review_outcomes,
    screenshot_diff,
    self_check,
    sync,
    sync_workflow,
    test_timings,
    verification,
)
from fleet_tools.checks import runner

ROOT = Path(__file__).resolve().parent.parent
VERSION_HELP = ("print which fleet this is: its commit (git rev-parse HEAD in a checkout, direct_url.json in an install "
                "from git), and its tag when git describe --exact-match finds one")


@dataclass(frozen=True)
class Command:
    help: str
    configure: Callable[[argparse.ArgumentParser], None]
    run: Callable[[argparse.Namespace], int]


COMMANDS: dict[str, Command] = {}


def register(name: str, command: Command) -> None:
    if name in COMMANDS:
        raise ValueError(f"fleet: the command {name!r} is already registered")
    COMMANDS[name] = command


register("self-check", Command(self_check.HELP, self_check.configure, self_check.run))
register("checks", Command(runner.HELP, runner.configure, runner.run))
register("no-comments", Command(no_comments.HELP, no_comments.configure, no_comments.run))
register("coverage-floor", Command(coverage_floor.HELP, coverage_floor.configure, coverage_floor.run))
register("next-version", Command(next_version.HELP, next_version.configure, next_version.run))
register("test-timings", Command(test_timings.HELP, test_timings.configure, test_timings.run))
register("sync", Command(sync.HELP, sync.configure, sync.run))
register("drift", Command(drift.HELP, drift.configure, drift.run))
register("sync-plan", Command(sync_workflow.HELP, sync_workflow.configure, sync_workflow.run))
register("agent-review", Command(agent_review.HELP, agent_review.configure, agent_review.run))
register("screenshot-diff", Command(screenshot_diff.HELP, screenshot_diff.configure, screenshot_diff.run))
register("review-outcomes", Command(review_outcomes.HELP, review_outcomes.configure, review_outcomes.run))
register("review-mine", Command(review_mine.HELP, review_mine.configure, review_mine.run))
register("mining-tasks", Command(mining_tasks.HELP, mining_tasks.configure, mining_tasks.run))
register("loop-stats", Command(loop_stats.HELP, loop_stats.configure, loop_stats.run))
register("verification", Command(verification.HELP, verification.configure, verification.run))
register("release", Command(release.HELP, release.configure, release.run))


class Version(argparse.Action):
    def __init__(self, option_strings: Sequence[str], dest: str = argparse.SUPPRESS, default: str = argparse.SUPPRESS,
                 help: str | None = VERSION_HELP) -> None:
        super().__init__(option_strings=option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace, values: object,
                 option_string: str | None = None) -> None:
        sys.stdout.write(identity.describe(ROOT) + "\n")
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet", description="fleet's shared checks and tools")
    parser.add_argument("--version", action=Version)
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    for name, command in COMMANDS.items():
        sub = commands.add_parser(name, help=command.help, description=command.help)
        command.configure(sub)
        sub.set_defaults(run=command.run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    run: Callable[[argparse.Namespace], int] = args.run
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
