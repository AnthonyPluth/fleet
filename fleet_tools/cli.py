import argparse
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from fleet_tools import self_check

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


def version() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


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
