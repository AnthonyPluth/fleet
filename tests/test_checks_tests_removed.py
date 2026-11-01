import unittest
from pathlib import Path

from consumers import (
    AGENT,
    RUNWAY,
    WAYPOINT,
    checks,
    commit,
    copy,
    empty_consumer,
    git,
    ran,
    write,
)

from fleet_tools.checks import tests_removed as tr

TESTS = "class T(unittest.TestCase):\n    def test_one(self): ...\n    def test_two(self): ...\n"


class Names(unittest.TestCase):
    def test_names_python_and_vitest(self) -> None:
        py = "class A(unittest.TestCase):\n    def test_one(self):\n        pass\n\n\ndef test_free():\n    pass\n"
        self.assertEqual(tr.test_names("tests/test_x.py", py), {"tests/test_x.py::A.test_one", "tests/test_x.py::test_free"})
        ts = 'describe("x", () => {\n  it("shows a trip", () => {});\n  test(`keeps ${a}`, () => {});\n});\n'
        self.assertEqual(tr.test_names("frontend/src/a.test.ts", ts),
                         {"frontend/src/a.test.ts::shows a trip", "frontend/src/a.test.ts::keeps ${a}"})

    def test_which_files_hold_tests(self) -> None:
        self.assertTrue(tr.is_test_file("tests/test_backup.py"))
        self.assertTrue(tr.is_test_file("frontend/src/lib/api.test.ts"))
        self.assertFalse(tr.is_test_file("tests/shared.py"))
        self.assertFalse(tr.is_test_file("waypoint/server/handler.py"))
        self.assertFalse(tr.is_test_file("other/test_x.py"))
        self.assertTrue(tr.is_test_file("other/test_x.py", ("other",)))

    def test_skips_are_recognised(self) -> None:
        for line in ("@unittest.skip('later')", "self.skipTest('flaky')", "it.skip('x', () => {})", "describe.only('x')",
                     "xit('x')", "@unittest.skipUnless(db.using_postgres(), 'Postgres only')"):
            with self.subTest(line=line):
                self.assertTrue(tr.SKIP.search(line))
        for line in ("def test_skips_nothing(self):", "it('skips a cancelled leg', () => {})", "only = 1"):
            with self.subTest(line=line):
                self.assertFalse(tr.SKIP.search(line))

    def test_which_test_a_skip_belongs_to(self) -> None:
        py = ["class T(unittest.TestCase):", "    @unittest.skip('later')", "    def test_decorated(self):",
              "        pass", "", "    def test_inside(self):", "        self.skipTest('x')"]
        self.assertEqual(tr.skipped_name("tests/test_t.py", py, 1), "test_decorated")
        self.assertEqual(tr.skipped_name("tests/test_t.py", py, 6), "test_inside")
        self.assertEqual(tr.skipped_name("frontend/src/a.test.ts", ['  it.skip("shows a trip", () => {});'], 0), "shows a trip")


class Trailers(unittest.TestCase):
    def test_nothing_removed_passes(self) -> None:
        self.assertEqual(tr.check_tests([], [], []), [])

    def test_a_removed_test_needs_a_trailer_naming_it(self) -> None:
        gone = "tests/test_trips.py::TripTests.test_hidden_trip_is_404"
        self.assertIn("Removes-Test: TripTests.test_hidden_trip_is_404", tr.check_tests([gone], [], [])[0])
        for trailer in ("TripTests.test_hidden_trip_is_404 — covered by test_every_route now",
                        "test_hidden_trip_is_404 — covered by test_every_route now", f"{gone} — moved"):
            with self.subTest(trailer=trailer):
                self.assertEqual(tr.check_tests([gone], [], [f"refactor: x\n\nRemoves-Test: {trailer}\n"]), [])
        other = "refactor: x\n\nRemoves-Test: test_hidden_trip — a different test whose name is part of this one's\n"
        self.assertTrue(tr.check_tests([gone], [], [other]))

    def test_a_vitest_title_is_named_up_to_the_dash(self) -> None:
        gone = "frontend/src/lib/app.test.ts::shows a trip, then its legs"
        self.assertEqual(tr.check_tests([gone], [], ["x\n\nRemoves-Test: shows a trip, then its legs — merged into one\n"]), [])
        self.assertTrue(tr.check_tests([gone], [], ["x\n\nRemoves-Test: shows — merged\n"]))

    def test_each_skip_needs_its_own_trailer(self) -> None:
        added = [("tests/test_trips.py", "test_a", "@unittest.skip('later')"),
                 ("tests/test_trips.py", "test_b", "self.skipTest('flaky')")]
        found = tr.check_tests([], added, ["x\n\nSkips-Test: test_a — needs Postgres\n"])
        self.assertEqual(len(found), 1)
        self.assertIn("Skips-Test: test_b", found[0])
        both = "x\n\nSkips-Test: test_a — needs Postgres\nSkips-Test: test_b \u2013 a reason\n"
        self.assertEqual(tr.check_tests([], added, [both]), [])


class FromGit(unittest.TestCase):
    def setUp(self) -> None:
        self.root = copy(self, RUNWAY)
        write(self.root, "tests/test_a.py", TESTS)
        self.base = commit(self.root, "test: a")

    def test_a_test_removed_on_main_after_the_branch_was_cut_is_not_this_branch_s(self) -> None:
        git(self.root, "checkout", "-q", "-b", "branch")
        write(self.root, "README.md", "x\n")
        head = commit(self.root, "docs: x")
        git(self.root, "checkout", "-q", "main")
        write(self.root, "tests/test_a.py", "class T(unittest.TestCase):\n    def test_one(self): ...\n")
        commit(self.root, "test: drop two\n\nRemoves-Test: test_two — gone")
        self.assertEqual(checks(self.root, f"main..{head}"), [])

    def test_a_test_the_branch_removes_is_found_from_its_own_head(self) -> None:
        write(self.root, "tests/test_a.py", "class T(unittest.TestCase):\n    def test_one(self): ...\n")
        head = commit(self.root, "test: drop two")
        self.assertEqual(tr.removed_tests(self.root, self.base, head), ["tests/test_a.py::T.test_two"])

    def test_a_test_file_the_branch_deletes_counts_as_all_removed(self) -> None:
        (self.root / "tests/test_a.py").unlink()
        head = commit(self.root, "test: drop a")
        self.assertEqual(tr.removed_tests(self.root, self.base, head), ["tests/test_a.py::T.test_one", "tests/test_a.py::T.test_two"])

    def test_an_added_skip_is_found_with_its_test(self) -> None:
        write(self.root, "tests/test_a.py", TESTS.replace("    def test_two", "    @unittest.skip('later')\n    def test_two"))
        head = commit(self.root, "test: skip two")
        self.assertEqual(tr.added_skips(self.root, self.base, head), [("tests/test_a.py", "test_two", "@unittest.skip('later')")])


class FixtureConsumers(unittest.TestCase):
    def test_a_removed_test_fires_in_each_consumer_and_its_trailer_answers_it(self) -> None:
        for fixture, path, name in ((WAYPOINT, "tests/test_trips.py", "TripTests.test_a_hidden_trip_is_404"),
                                    (RUNWAY, "tests/test_budget.py", "BudgetTests.test_a_budget_rolls_over")):
            with self.subTest(fixture=fixture.name):
                root = copy(self, fixture)
                test = name.split(".")[1]
                target = root / path
                target.write_text(target.read_text().replace(f"def {test}(", "def helper("))
                commit(root, "refactor: x\n\n" + AGENT)
                [problem] = checks(root, "HEAD~1..HEAD")
                self.assertEqual(problem, f"the test {path}::{name} is gone: keep it, or say why on a commit with a "
                                          f"'Removes-Test: {name} — <why>' trailer")
                git(root, "commit", "-q", "--amend", "-m", f"refactor: x\n\nRemoves-Test: {test} — merged\n\n{AGENT}")
                self.assertEqual(checks(root, "HEAD~1..HEAD"), [])

    def test_an_added_skip_fires(self) -> None:
        root = copy(self, WAYPOINT)
        target = root / "tests/test_trips.py"
        target.write_text(target.read_text().replace("        pass\n", "        self.skipTest('flaky')\n", 1))
        commit(root, "test: skip")
        [problem] = checks(root, "HEAD~1..HEAD")
        self.assertIn("a test is skipped or narrowed (tests/test_trips.py: self.skipTest('flaky'))", problem)
        self.assertIn("'Skips-Test: test_a_trip_has_a_name — <why>'", problem)

    def test_test_paths_choose_where_tests_live(self) -> None:
        root = copy(self, RUNWAY)
        pyproject = root / "pyproject.toml"
        pyproject.write_text(pyproject.read_text().replace("[tool.fleet]\n", '[tool.fleet]\ntest-paths = ["spec/"]\n'))
        (root / "tests/test_budget.py").unlink()
        commit(root, "test: drop the budget tests")
        self.assertEqual(checks(root, "HEAD~1..HEAD"), [])
        pyproject.write_text(pyproject.read_text().replace('test-paths = ["spec/"]\n', "test-paths = []\n"))
        commit(root, "chore: no tests anywhere")
        self.assertNotIn("tests-removed", ran(root, "HEAD~1..HEAD"))

    def test_the_default_applies_in_an_app_without_config(self) -> None:
        root = empty_consumer(self)
        write(root, "tests/test_x.py", "def test_x(): ...\n")
        commit(root, "test: x")
        Path(root / "tests/test_x.py").unlink()
        commit(root, "test: drop x")
        self.assertEqual(len(checks(root, "HEAD~1..HEAD")), 1)


if __name__ == "__main__":
    unittest.main()
