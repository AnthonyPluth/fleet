import tempfile
import unittest
from pathlib import Path

from fleet_tools import config

FIXTURES = Path(__file__).resolve().parent / "fixtures"
WAYPOINT = FIXTURES / "consumer-waypoint"
RUNWAY = FIXTURES / "consumer-runway"


class FixtureConsumersTest(unittest.TestCase):
    def test_waypoint(self) -> None:
        conf = config.load(WAYPOINT)
        self.assertEqual(conf.root, WAYPOINT)
        self.assertEqual(conf.path("migrations"), WAYPOINT / "waypoint/storage/migrations/versions")
        self.assertEqual(conf.string("tested-from"), "0001")
        self.assertEqual(conf.strings("image-files"), ["docs/src/assets/logo.png"])
        self.assertEqual(conf.string("extra-checks"), "tools.app_checks:CHECKS")
        self.assertEqual(conf.section("screenshots")["names"], "pages")
        self.assertEqual(conf.section("no-comments")["paths"], ["waypoint", "tests", "tools", "frontend/src"])
        coverage = conf.section("coverage")
        self.assertEqual(coverage["overall"], 85)
        self.assertEqual(coverage["modules"]["waypoint/providers/"], 90)
        self.assertEqual(
            conf.tables("generated"),
            [{"path": ".claude/skills/verify-waypoint/references/cli.md",
              "command": "make --no-print-directory -s verify-skill-print", "fix": "make verify-skill"}],
        )

    def test_runway(self) -> None:
        conf = config.load(RUNWAY)
        self.assertEqual(conf.path("migrations"), RUNWAY / "runway/storage/migrations/versions")
        self.assertEqual(conf.section("screenshots")["names"], "pages-and-flows")
        self.assertFalse(conf.has("no-comments"))
        self.assertEqual(conf.section("no-comments"), {})
        self.assertFalse(conf.has("coverage"))
        self.assertEqual(conf.strings("image-files"), ["docs/src/assets/logo.png"])
        self.assertEqual(len(conf.tables("generated")), 1)

    def test_each_fixture_has_the_layout_its_config_names(self) -> None:
        for root in (WAYPOINT, RUNWAY):
            conf = config.load(root)
            directories = [conf.path("migrations"), conf.path("skills"), *conf.paths("image-homes")]
            directories += [root / conf.section("screenshots")["dir"], root / conf.section("screenshots")["flows"]]
            for directory in directories:
                with self.subTest(root=root.name, directory=directory):
                    self.assertIsNotNone(directory)
                    assert directory is not None
                    self.assertTrue(directory.is_dir())

    def test_find_root_walks_up_to_the_pyproject(self) -> None:
        self.assertEqual(config.find_root(WAYPOINT / "waypoint/storage/migrations/versions"), WAYPOINT)
        self.assertEqual(config.find_root(RUNWAY), RUNWAY)


class ConfigErrorsTest(unittest.TestCase):
    def write(self, text: str) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        (directory / "pyproject.toml").write_text(text, encoding="utf-8")
        return directory

    def test_no_table_is_empty_config(self) -> None:
        conf = config.load(self.write('[project]\nname = "app"\n'))
        self.assertEqual(conf.table, {})
        self.assertIsNone(conf.path("migrations"))
        self.assertEqual(conf.tables("generated"), [])

    def test_missing_pyproject(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(config.ConfigError):
            config.load(Path(directory))

    def test_invalid_toml(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load(self.write("[tool.fleet\n"))

    def test_fleet_not_a_table(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load(self.write('[tool]\nfleet = "yes"\n'))

    def test_wrong_types(self) -> None:
        conf = config.load(self.write('[tool.fleet]\nmigrations = 3\nimage-homes = [1]\nscreenshots = "x"\n'
                                      'generated = "x"\n'))
        with self.assertRaises(config.ConfigError):
            conf.string("migrations")
        with self.assertRaises(config.ConfigError):
            conf.strings("image-homes")
        with self.assertRaises(config.ConfigError):
            conf.section("screenshots")
        with self.assertRaises(config.ConfigError):
            conf.tables("generated")

    def test_a_single_string_is_a_list_of_one(self) -> None:
        conf = config.load(self.write('[tool.fleet]\nimage-homes = "docs/"\n'))
        self.assertEqual(conf.strings("image-homes"), ["docs/"])

    def test_find_root_without_a_pyproject(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(config.ConfigError):
            config.find_root(Path(directory) / "missing")


if __name__ == "__main__":
    unittest.main()
