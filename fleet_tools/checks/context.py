import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fleet_tools.config import Config


@dataclass(frozen=True)
class Commits:
    base: str
    head: str
    log: list[tuple[str, str]] = field(default_factory=list)

    @property
    def messages(self) -> list[str]:
        return [message for _, message in self.log]


@dataclass(frozen=True)
class Context:
    root: Path
    config: Config
    commits: Commits | None = None


@dataclass(frozen=True)
class Check:
    name: str
    passed: str
    applies: Callable[[Context], bool]
    run: Callable[[Context], list[str]]


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def show(root: Path, rev: str, path: str) -> str | None:
    try:
        return git(root, "show", f"{rev}:./{path}")
    except subprocess.CalledProcessError:
        return None


def relative(root: Path, path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
