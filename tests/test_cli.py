import argparse
import contextlib
import io
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from fleet_tools import cli


class VersionTest(unittest.TestCase):
    def test_version_prints_the_version_file(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            cli.main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), (cli.ROOT / "VERSION").read_text(encoding="utf-8").strip())

    def test_version_falls_back_to_installed_metadata_without_a_version_file(self) -> None:
        with (
            tempfile.TemporaryDirectory() as empty,
            mock.patch.object(cli, "ROOT", Path(empty)),
            mock.patch("importlib.metadata.version", return_value="9.8.7") as metadata,
        ):
            self.assertEqual(cli.version(), "9.8.7")
        metadata.assert_called_once_with("fleet")

    def test_version_is_semver(self) -> None:
        self.assertRegex(cli.version(), r"^\d+\.\d+\.\d+$")

    def test_pyproject_version_matches_version_file(self) -> None:
        with (cli.ROOT / "pyproject.toml").open("rb") as file:
            pyproject = tomllib.load(file)
        self.assertEqual(pyproject["project"]["version"], cli.version())

    def test_runs_as_a_module(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "fleet_tools", "--version"],
            cwd=cli.ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), cli.version())


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
