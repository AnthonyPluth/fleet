import unittest
from pathlib import Path

from consumers import (
    RUNWAY,
    WAYPOINT,
    checks,
    copy,
    empty_consumer,
    ran,
    temporary,
    write,
)

from fleet_tools.checks import migrations

MIGRATION = "revision = {rev!r}\ndown_revision = {down!r}\n\ndef upgrade():\n    pass\n"
TESTS = "def test_0036_b(self): ...\ndef test_0037_c(self): ...\ndef test_0038_d(self): ...\n"


class Migrations(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temporary(self)
        self.write("0035_a.py", "0035", None)
        self.write("0036_b.py", "0036", "0035")

    def write(self, name: str, rev: str, down: str | None) -> None:
        (self.dir / name).write_text(MIGRATION.format(rev=rev, down=down))

    def problems(self, tests: str = TESTS) -> list[str]:
        return migrations.check_migrations(migrations.migrations(self.dir), tests, tested_from="0036")

    def test_a_chain_with_tests_passes(self) -> None:
        self.write("0037_c.py", "0037", "0036")
        self.assertEqual(self.problems(), [])

    def test_two_branches_each_adding_the_next_number_leave_two_heads(self) -> None:
        self.write("0037_c.py", "0037", "0036")
        self.write("0038_d.py", "0038", "0036")
        problems = self.problems()
        self.assertEqual(len(problems), 1)
        self.assertIn("more than one Alembic head (0037, 0038)", problems[0])

    def test_the_same_number_twice_is_caught(self) -> None:
        self.write("0037_c.py", "0037", "0036")
        self.write("0037_d.py", "0037", "0036")
        self.assertTrue(any("'0037' is used more than once (0037_c.py, 0037_d.py)" in p for p in self.problems()))

    def test_a_new_migration_without_its_own_test_fails(self) -> None:
        self.write("0037_c.py", "0037", "0036")
        problems = self.problems(tests="def test_0036_b(self): ...\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("0037_c.py: no test of its own in tests/test_migrations.py", problems[0])

    def test_migrations_before_the_rule_need_no_test(self) -> None:
        self.assertEqual(self.problems(tests="def test_0036_b(self): ...\n"), [])

    def test_a_parent_that_isnt_there_and_a_misnamed_file_fail(self) -> None:
        self.write("0037_c.py", "0037", "0099")
        self.write("c_0038.py", "0038", "0037")
        problems = self.problems()
        self.assertTrue(any("down_revision '0099' is no migration here" in p for p in problems))
        self.assertTrue(any("c_0038.py: the file name should start with its revision" in p for p in problems))

    def test_a_merge_migration_follows_both_heads(self) -> None:
        self.write("0037_c.py", "0037", "0036")
        self.write("0038_d.py", "0038", "0036")
        (self.dir / "0039_merge.py").write_text("revision = '0039'\ndown_revision = ('0037', '0038')\n")
        self.assertEqual(self.problems(tests=TESTS + "def test_0039_m(self): ...\n"), [])

    def test_a_revision_that_isnt_four_digits_fails(self) -> None:
        (self.dir / "0037_c.py").write_text("revision = 'abc'\ndown_revision = '0036'\n")
        self.assertTrue(any("revision should be four digits" in p for p in self.problems()))

    def test_a_loop_has_no_head(self) -> None:
        directory = temporary(self)
        (directory / "0001_a.py").write_text("revision = '0001'\ndown_revision = '0002'\n")
        (directory / "0002_b.py").write_text("revision = '0002'\ndown_revision = '0001'\n")
        found = migrations.check_migrations(migrations.migrations(directory), "", tested_from="9999")
        self.assertEqual(found, ["no Alembic head: the migrations go round in a loop"])


class FixtureConsumers(unittest.TestCase):
    def test_both_consumers_pass(self) -> None:
        for fixture in (WAYPOINT, RUNWAY):
            with self.subTest(fixture=fixture.name):
                root = copy(self, fixture, repository=False)
                self.assertIn("migrations", ran(root))
                self.assertEqual([p for p in checks(root) if "migration" in p], [])

    def test_a_migration_without_a_test_fires_in_waypoint(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        write(root, "waypoint/storage/migrations/versions/0003_stays.py", MIGRATION.format(rev="0003", down="0002"))
        self.assertEqual(checks(root), ["0003_stays.py: no test of its own in tests/test_migrations.py (a method named "
                                        "test_0003_...); downgrade to the revision before it, write the data it changes, "
                                        "upgrade and check the result"])

    def test_two_heads_fire_in_runway(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        for rev in ("0002", "0003"):
            write(root, f"runway/storage/migrations/versions/{rev}_x.py", MIGRATION.format(rev=rev, down="0001"))
        tests = root / "tests/test_migrations.py"
        tests.write_text(tests.read_text() + "\n    def test_0002_x(self) -> None:\n        pass\n\n"
                         "    def test_0003_x(self) -> None:\n        pass\n")
        self.assertEqual(len([p for p in checks(root) if "more than one Alembic head (0002, 0003)" in p]), 1)

    def test_tested_from_skips_older_migrations(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        write(root, "tests/test_migrations.py", "")
        self.assertEqual(len(checks(root)), 1)
        pyproject = root / "pyproject.toml"
        pyproject.write_text(pyproject.read_text().replace('tested-from = "0001"', 'tested-from = "0002"'))
        self.assertEqual(checks(root), [])

    def test_a_missing_directory_is_reported(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        pyproject = root / "pyproject.toml"
        pyproject.write_text(pyproject.read_text().replace('"runway/storage/migrations/versions"', '"nowhere"'))
        self.assertIn("nowhere: [tool.fleet] migrations names a directory that isn't there", checks(root))

    def test_silent_where_not_configured(self) -> None:
        root = empty_consumer(self)
        write(root, "migrations/0001_x.py", "revision = 'x'\n")
        self.assertNotIn("migrations", ran(root))
        self.assertEqual(checks(root), [])


class Discovery(unittest.TestCase):
    def test_dunder_files_are_skipped_and_an_annotated_revision_counts(self) -> None:
        directory = temporary(self)
        (directory / "__init__.py").write_text("")
        (directory / "0001_a.py").write_text("revision: str = '0001'\ndown_revision: str | None = None\n")
        self.assertEqual(migrations.migrations(Path(directory)), [migrations.Migration("0001_a.py", "0001", ())])


if __name__ == "__main__":
    unittest.main()
