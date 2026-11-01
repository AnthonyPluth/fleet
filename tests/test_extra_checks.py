import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from consumers import RUNWAY, WAYPOINT, checks, copy, empty_consumer, ran, write

from fleet_tools import cli
from fleet_tools.checks import extra

CONFIG = '[tool.fleet]\nextra-checks = "{spec}"\n'


def consumer_with(test: unittest.TestCase, spec: str, files: dict[str, str]) -> Path:
    root = empty_consumer(test)
    write(root, "pyproject.toml", CONFIG.format(spec=spec))
    for path, text in files.items():
        write(root, path, text)
    return root


class Loading(unittest.TestCase):
    def test_the_spec_is_module_colon_name(self) -> None:
        self.assertEqual(extra.parse("tools.app_checks:CHECKS"), ("tools.app_checks", "CHECKS"))
        for bad in ("tools.app_checks", "tools/app_checks.py:CHECKS", ":CHECKS", "tools.app_checks:", "a-b:C"):
            with self.subTest(bad=bad), self.assertRaisesRegex(extra.ExtraChecksError, "should be `package.module:NAME`"):
                extra.parse(bad)

    def test_a_package_s_init_is_a_module_too(self) -> None:
        root = consumer_with(self, "checks:CHECKS", {"checks/__init__.py": "CHECKS = [lambda context: ['from a package']]\n"})
        self.assertEqual(checks(root), ["from a package"])

    def test_the_consumer_s_own_modules_import_and_are_dropped_after(self) -> None:
        root = consumer_with(self, "tools.app_checks:CHECKS", {
            "tools/__init__.py": "",
            "tools/helpers.py": "def problem():\n    return 'from a helper'\n",
            "tools/app_checks.py": "from tools.helpers import problem\n\nCHECKS = [lambda context: [problem()]]\n"})
        before = set(sys.modules)
        self.assertEqual(checks(root), ["from a helper"])
        self.assertEqual({name for name in set(sys.modules) - before if name.startswith("tools")}, set())
        self.assertNotIn(str(root), sys.path)


class Reporting(unittest.TestCase):
    def problems(self, source: str) -> list[str]:
        return checks(consumer_with(self, "app:CHECKS", {"app.py": source}))

    def test_each_callable_s_problems_are_reported(self) -> None:
        source = ("def one(context):\n    return ['first', 'second']\n\n"
                  "def two(context):\n    return iter([str(context.root.name)])\n\nCHECKS = [one, two]\n")
        self.assertEqual(self.problems(source), ["first", "second", "consumer-empty"])

    def test_a_missing_module_is_a_clear_error(self) -> None:
        root = empty_consumer(self)
        write(root, "pyproject.toml", CONFIG.format(spec="tools.app_checks:CHECKS"))
        [problem] = checks(root)
        self.assertIn("extra-checks: no module tools.app_checks in", problem)
        self.assertIn("looked for tools/app_checks.py and tools/app_checks/__init__.py", problem)

    def test_a_missing_attribute_or_a_wrong_shape_is_a_clear_error(self) -> None:
        self.assertEqual(self.problems("OTHER = []\n"), ["extra-checks: app has no CHECKS"])
        self.assertEqual(self.problems("CHECKS = 'nope'\n"), ["extra-checks: app.CHECKS should be a list of callables"])
        self.assertEqual(self.problems("CHECKS = [1]\n"), ["extra-checks: app.CHECKS should be a list of callables"])

    def test_an_import_that_fails_is_reported(self) -> None:
        [missing] = self.problems("import no_such_module_anywhere\nCHECKS = []\n")
        self.assertIn("failed: ModuleNotFoundError: No module named 'no_such_module_anywhere'", missing)
        [problem] = self.problems("raise RuntimeError('boom')\n")
        self.assertIn("extra-checks: importing app from", problem)
        self.assertIn("failed: RuntimeError: boom", problem)

    def test_a_check_that_raises_or_returns_the_wrong_thing_is_reported(self) -> None:
        source = ("def broken(context):\n    raise ValueError('bad data')\n\n"
                  "def stringy(context):\n    return 'one problem'\n\n"
                  "def nothing(context):\n    return None\n\nCHECKS = [broken, stringy, nothing]\n")
        self.assertEqual(self.problems(source), [
            "extra check broken failed: ValueError: bad data",
            "extra check stringy should return a list of problems (strings), not str",
            "extra check nothing should return a list of problems (strings), not NoneType"])


class FixtureConsumers(unittest.TestCase):
    def test_both_consumers_run_their_own_checks_and_pass(self) -> None:
        for fixture in (WAYPOINT, RUNWAY):
            with self.subTest(fixture=fixture.name):
                root = copy(self, fixture, repository=False)
                self.assertIn("extra-checks", ran(root))
                self.assertEqual(checks(root), [])

    def test_waypoint_s_parser_fixtures_fire(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        write(root, "waypoint/domain/mail/parsers/globex.py", "def parse(message):\n    return None\n")
        self.assertEqual(checks(root), ["waypoint/domain/mail/parsers/globex.py: no fixture "
                                        "tests/fixtures/mail/globex/booking.eml"])

    def test_runway_s_a11y_baseline_fires(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        write(root, "frontend/verify/a11y-baseline.txt", "budget @ phone | color-contrast\n")
        self.assertEqual(checks(root), ["frontend/verify/a11y-baseline.txt line 1: expected "
                                        "`<page or flow> @ <width> | <finding> | <reason>`"])

    def test_the_cli_reports_extra_findings_in_the_same_format(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        write(root, "frontend/verify/a11y-baseline.txt", "budget @ phone | color-contrast\n")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["checks", "--root", str(root)])
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "::error::frontend/verify/a11y-baseline.txt line 1: expected "
                                         "`<page or flow> @ <width> | <finding> | <reason>`\n")

    def test_silent_where_not_configured(self) -> None:
        root = empty_consumer(self)
        write(root, "tools/app_checks.py", "CHECKS = [lambda context: ['should not run']]\n")
        self.assertNotIn("extra-checks", ran(root))
        self.assertEqual(checks(root), [])


if __name__ == "__main__":
    unittest.main()
