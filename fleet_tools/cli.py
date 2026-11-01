import argparse
import importlib.metadata
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from fleet_tools import (
    coverage_floor,
    next_version,
    no_comments,
    screenshot_diff,
    self_check,
    test_timings,
)
from fleet_tools.checks import runner

ROOT = Path(__file__).resolve().parent.parent


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
register("screenshot-diff", Command(screenshot_diff.HELP, screenshot_diff.configure, screenshot_diff.run))


def version() -> str:
    version_file = ROOT / "VERSION"
    if version_file.is_file():
        return version_file.read_text(encoding="utf-8").strip()
    return importlib.metadata.version("fleet")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet", description="fleet's shared checks and tools")
    parser.add_argument("--version", action="version", version=version())
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
