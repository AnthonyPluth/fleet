import contextlib
import io
import json
import unittest
from pathlib import Path
from typing import Any

from consumers import RUNWAY, WAYPOINT, copy, temporary

from fleet_tools import cli, config, coverage_floor

FLOORS = coverage_floor.Floors(85.0, {"waypoint/domain/": 90.0, "waypoint/providers/": 90.0, "waypoint/server/api/": 90.0})


def report(total: float, files: dict[str, tuple[float, int]] | None = None) -> dict[str, Any]:
    return {"totals": {"percent_covered": total},
            "files": {name: {"summary": {"percent_covered": pct, "num_statements": n}}
                      for name, (pct, n) in (files or {}).items()}}


def run(test: unittest.TestCase, root: Path, data: dict[str, Any] | None) -> tuple[int, str, str]:
    path = temporary(test) / "coverage.json"
    if data is not None:
        path.write_text(json.dumps(data))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["coverage-floor", str(path), "--root", str(root)])
    return code, out.getvalue(), err.getvalue()


class Floors(unittest.TestCase):
    def test_at_the_floor_passes(self) -> None:
        self.assertEqual(coverage_floor.problems(report(85, {"waypoint/domain/trips.py": (90, 40)}), FLOORS), [])

    def test_too_little_overall(self) -> None:
        self.assertEqual(coverage_floor.problems(report(84.9), FLOORS), ["the tests cover 84.9% overall; the floor is 85%"])

    def test_one_module_where_features_live(self) -> None:
        found = coverage_floor.problems(report(99, {"waypoint/providers/gmail.py": (60, 100), "waypoint/oidc.py": (60, 100),
                                                    "waypoint/server/api/trips.py": (89, 10)}), FLOORS)
        self.assertEqual(len(found), 2)
        self.assertTrue(found[0].startswith("waypoint/providers/gmail.py: the tests cover 60.0%; modules in "
                                            "waypoint/providers/ need 90%"))
        self.assertTrue(found[1].startswith("waypoint/server/api/trips.py"))

    def test_an_empty_module_needs_nothing(self) -> None:
        self.assertEqual(coverage_floor.problems(report(99, {"waypoint/domain/__init__.py": (0, 0)}), FLOORS), [])

    def test_the_longest_prefix_wins(self) -> None:
        floors = coverage_floor.Floors(None, {"app/": 50.0, "app/core/": 95.0})
        self.assertEqual(len(coverage_floor.problems(report(10, {"app/core/x.py": (90, 10), "app/y.py": (60, 10)}), floors)), 1)

    def test_floors_are_read_from_config(self) -> None:
        self.assertEqual(coverage_floor.floors(config.load(WAYPOINT)), FLOORS)
        with self.assertRaisesRegex(config.ConfigError, r"no \[tool.fleet.coverage\]"):
            coverage_floor.floors(config.load(RUNWAY))
        bad = config.Config(Path("."), {"coverage": {"overall": "85"}})
        with self.assertRaisesRegex(config.ConfigError, "overall must be a number"):
            coverage_floor.floors(bad)
        with self.assertRaisesRegex(config.ConfigError, "modules must map path prefixes to numbers"):
            coverage_floor.floors(config.Config(Path("."), {"coverage": {"modules": {"a/": True}}}))


class Command(unittest.TestCase):
    def test_waypoint_at_its_floors_passes(self) -> None:
        code, out, _ = run(self, WAYPOINT, report(90, {"waypoint/domain/trips.py": (95, 10)}))
        self.assertEqual(code, 0)
        self.assertEqual(out, "Coverage floor met: 85% overall, 90% for each module in waypoint/domain/, 90% for each "
                              "module in waypoint/providers/, 90% for each module in waypoint/server/api/.\n")

    def test_waypoint_under_a_floor_fails(self) -> None:
        code, _, err = run(self, WAYPOINT, report(80))
        self.assertEqual(code, 1)
        self.assertEqual(err, "coverage: the tests cover 80.0% overall; the floor is 85%\n")

    def test_no_floors_or_no_report_is_an_error(self) -> None:
        self.assertEqual(run(self, copy(self, RUNWAY, repository=False), report(10))[0], 2)
        code, _, err = run(self, WAYPOINT, None)
        self.assertEqual(code, 2)
        self.assertIn("can't read", err)


if __name__ == "__main__":
    unittest.main()
