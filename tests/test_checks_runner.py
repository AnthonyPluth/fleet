import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from consumers import AGENT, RUNWAY, WAYPOINT, commit, copy, empty_consumer, write

from fleet_tools import cli

ROOT = Path(__file__).resolve().parent.parent


def run(*args: str, actions: bool = False) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    env = {"GITHUB_ACTIONS": "true"} if actions else {}
    with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        if not actions:
            os.environ.pop("GITHUB_ACTIONS", None)
        code = cli.main(["checks", *args])
    return code, out.getvalue(), err.getvalue()


class Reporting(unittest.TestCase):
    def test_a_passing_consumer_lists_what_ran_and_exits_0(self) -> None:
        code, out, err = run("--root", str(copy(self, WAYPOINT, repository=False)))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "Fleet checks passed: one migration head, migrations tested, workflow conventions, skills "
                              "well formed, generated references and feature maps current, the app's own checks.\n")

    def test_commits_add_the_checks_that_read_them(self) -> None:
        root = copy(self, RUNWAY)
        write(root, "README.md", "x\n")
        commit(root, "docs: readme\n\n" + AGENT)
        code, out, _ = run("--root", str(root), "--commits", "HEAD~1..HEAD")
        self.assertEqual(code, 0)
        for name in ("commit trailers", "no test removed or skipped without a reason",
                     "images only where make verify puts them"):
            self.assertIn(name, out)

    def test_each_problem_is_a_line_on_stderr_and_exits_1(self) -> None:
        root = copy(self, RUNWAY)
        commit(root, "fix: x\n\nClaude-Session: https://claude.ai/code/session_x")
        code, out, err = run("--root", str(root), "--commits", "HEAD~1..HEAD")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("needs a 'Co-Authored-By: Claude <Model> <version> <noreply@anthropic.com>' trailer", err)

    def test_on_github_actions_each_problem_is_an_error_annotation(self) -> None:
        root = copy(self, RUNWAY)
        commit(root, "fix: x\n\nClaude-Session: https://claude.ai/code/session_x")
        code, _, err = run("--root", str(root), "--commits", "HEAD~1..HEAD", actions=True)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("::error::commit "))

    def test_an_app_with_nothing_configured_passes_with_nothing_checked(self) -> None:
        code, out, _ = run("--root", str(empty_consumer(self)))
        self.assertEqual((code, out), (0, "Fleet checks passed: nothing is configured to check.\n"))

    def test_a_config_error_or_a_bad_range_exits_2(self) -> None:
        root = empty_consumer(self)
        write(root, "pyproject.toml", "[tool.fleet\n")
        code, _, err = run("--root", str(root))
        self.assertEqual(code, 2)
        self.assertIn("fleet checks: ", err)
        self.assertIn("isn't valid TOML", err)
        code, _, err = run("--root", str(copy(self, RUNWAY)), "--commits", "nowhere..HEAD")
        self.assertEqual(code, 2)
        self.assertIn("fleet checks: `git merge-base nowhere HEAD` failed", err)

    def test_the_root_defaults_to_the_nearest_pyproject(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        here = Path.cwd()
        os.chdir(root / "runway/storage")
        self.addCleanup(os.chdir, here)
        self.assertEqual(run()[0], 0)


class SystemPython(unittest.TestCase):
    def test_the_checks_run_with_the_standard_library_alone(self) -> None:
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
        for fixture in (WAYPOINT, RUNWAY):
            with self.subTest(fixture=fixture.name):
                root = copy(self, fixture)
                done = subprocess.run([sys.executable, "-E", "-s", "-m", "fleet_tools.cli", "checks", "--root", str(root),
                                       "--commits", "HEAD..HEAD"], cwd=ROOT, env=env, capture_output=True, text=True,
                                      check=False)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertTrue(done.stdout.startswith("Fleet checks passed: "))


if __name__ == "__main__":
    unittest.main()
