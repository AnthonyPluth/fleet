import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fleet_tools import agents_md, config, identity, release, sync
from fleet_tools.checks import runner
from fleet_tools.checks.context import Context

FIXTURES = Path(__file__).resolve().parent / "fixtures"
WAYPOINT = FIXTURES / "consumer-waypoint"
RUNWAY = FIXTURES / "consumer-runway"
AGENT = "Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\nClaude-Session: https://claude.ai/code/session_x\n"
FIXTURE_COMMIT = release.FIXTURE_COMMIT
FIXTURE_VERSION = agents_md.parse_version_file((WAYPOINT / agents_md.VERSION_FILE).read_text(encoding="utf-8")).version
OFFLINE_TAGS = mock.patch.object(sync, "ls_remote", side_effect=lambda version: FIXTURE_COMMIT)
AT_FIXTURE_COMMIT = mock.patch.multiple(identity, commit=mock.Mock(return_value=FIXTURE_COMMIT), tag=mock.Mock(return_value=None))
GIT_ENV = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
           "GIT_COMMITTER_EMAIL": "test@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout


def temporary(test: unittest.TestCase) -> Path:
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    return Path(directory.name)


def copy(test: unittest.TestCase, fixture: Path, repository: bool = True) -> Path:
    root = temporary(test) / fixture.name
    shutil.copytree(fixture, root)
    if repository:
        git(root, "init", "-q", "-b", "main")
        commit(root, "chore: the fixture consumer")
    return root


def commit(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD").strip()


def write(root: Path, path: str, text: str) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def tagged_fleet(test: unittest.TestCase, version: str = FIXTURE_VERSION) -> Path:
    root = temporary(test) / "fleet"
    for directory in ("agents-md", "plugins", "shim"):
        shutil.copytree(agents_md.ROOT / directory, root / directory, ignore=shutil.ignore_patterns("__pycache__"))
    git(root, "init", "-q", "-b", "main")
    commit(root, "chore: fleet at a release")
    git(root, "tag", version)
    return root


def checks(root: Path, commits: str | None = None) -> list[str]:
    found = runner.resolve(root, commits) if commits else None
    problems, _, _ = runner.check(Context(root, config.load(root), found))
    return problems


def ran(root: Path, commits: str | None = None) -> list[str]:
    found = runner.resolve(root, commits) if commits else None
    return [check.name for check in runner.CHECKS if check.applies(Context(root, config.load(root), found))]


def empty_consumer(test: unittest.TestCase) -> Path:
    root = temporary(test) / "consumer-empty"
    write(root, "pyproject.toml", '[project]\nname = "empty"\nversion = "1.0.0"\n')
    git(root, "init", "-q", "-b", "main")
    commit(root, "chore: an app with no [tool.fleet]")
    return root
