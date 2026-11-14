import argparse
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fleet_tools import cli, identity

GIT_ENV = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
           "GIT_COMMITTER_EMAIL": "test@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


class VersionTest(unittest.TestCase):
    def version(self, root: Path | None = None) -> str:
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            if root is not None:
                stack.enter_context(mock.patch.object(cli, "ROOT", root))
            stack.enter_context(contextlib.redirect_stdout(out))
            raised = stack.enter_context(self.assertRaises(SystemExit))
            cli.main(["--version"])
        self.assertIn(raised.exception.code, (0, None))
        return out.getvalue()

    def checkout(self) -> tuple[Path, str]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "VERSION").write_text("9.8.7\n", encoding="utf-8")
        git(root, "init", "-q", "-b", "main")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "chore: a fleet")
        return root, git(root, "rev-parse", "HEAD")

    def test_version_prints_this_checkouts_commit(self) -> None:
        commit = identity.commit(cli.ROOT)
        assert commit is not None
        printed = self.version()
        self.assertEqual(printed, identity.describe(cli.ROOT) + "\n")
        self.assertIn(commit, printed)

    def test_version_prints_the_commit_and_never_the_version_file(self) -> None:
        root, commit = self.checkout()
        self.assertEqual(self.version(root), f"fleet at commit {commit}\n")

    def test_version_names_the_tag_on_the_commit(self) -> None:
        root, commit = self.checkout()
        git(root, "tag", "v1.2.3")
        self.assertEqual(self.version(root), f"fleet v1.2.3 (commit {commit})\n")
        git(root, "tag", "-d", "v1.2.3")
        git(root, "tag", "-a", "-m", "release", "v1.2.4")
        self.assertEqual(self.version(root), f"fleet v1.2.4 (commit {commit})\n")
        (root / "more").write_text("x\n", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "chore: after the release")
        self.assertEqual(self.version(root), f"fleet at commit {git(root, 'rev-parse', 'HEAD')}\n")

    def test_version_of_a_fleet_that_cant_name_its_commit_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(self.version(Path(empty)), identity.describe(Path(empty)) + "\n")
            self.assertTrue(self.version(Path(empty)).startswith("fleet at an unknown commit"))

    def test_the_parser_asks_for_no_commit_until_version_is_asked(self) -> None:
        with mock.patch.object(identity, "describe", side_effect=AssertionError("asked")), \
                mock.patch.object(identity, "commit", side_effect=AssertionError("asked")):
            args = cli.build_parser().parse_args(["self-check"])
        self.assertEqual(args.command, "self-check")

    def test_runs_as_a_module(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "fleet_tools", "--version"],
            cwd=cli.ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, identity.describe(cli.ROOT) + "\n")


class RegistryTest(unittest.TestCase):
    def test_self_check_is_registered(self) -> None:
        self.assertIn("self-check", cli.COMMANDS)

    def test_registering_a_name_twice_fails(self) -> None:
        command = cli.Command("again", lambda parser: None, lambda args: 0)
        with self.assertRaises(ValueError):
            cli.register("self-check", command)

    def test_a_registered_command_gets_its_arguments_and_exit_code(self) -> None:
        seen: list[argparse.Namespace] = []

        def configure(parser: argparse.ArgumentParser) -> None:
            parser.add_argument("--flag")

        def run(args: argparse.Namespace) -> int:
            seen.append(args)
            return 3

        cli.register("test-only", cli.Command("a test command", configure, run))
        self.addCleanup(cli.COMMANDS.pop, "test-only")
        self.assertEqual(cli.main(["test-only", "--flag", "x"]), 3)
        self.assertEqual(seen[0].flag, "x")

    def test_no_command_prints_help_and_fails(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main([]), 2)
        self.assertIn("self-check", err.getvalue())

    def test_an_unknown_command_fails(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(["no-such-command"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
