import unittest

from consumers import RUNWAY, WAYPOINT, checks, copy, empty_consumer, ran, write

from fleet_tools import config
from fleet_tools.checks import skills

GOOD_SKILL = "---\nname: verify-waypoint\ndescription: Drive the app.\n---\n\n# Body\n\nSee [cli](references/cli.md).\n"
CLI = ".claude/skills/verify-waypoint/references/cli.md"


class Skill(unittest.TestCase):
    def test_a_well_formed_skill_passes(self) -> None:
        self.assertEqual(skills.check_skill("verify-waypoint", GOOD_SKILL), [])

    def test_frontmatter_that_is_missing_or_malformed_fails(self) -> None:
        self.assertIn("no frontmatter", skills.check_skill("verify-waypoint", "# Just a body\n")[0])
        self.assertIn("isn't `key: value`",
                      skills.check_skill("verify-waypoint", GOOD_SKILL.replace("description:", "description"))[0])

    def test_the_name_is_the_folder_s_and_the_description_is_there_and_short(self) -> None:
        def first(text: str) -> str:
            return skills.check_skill("verify-waypoint", text)[0]
        self.assertIn("`name` should be 'verify-waypoint'", first(GOOD_SKILL.replace("name: verify-waypoint", "name: other")))
        self.assertIn("no `description`", first(GOOD_SKILL.replace("Drive the app.", "")))
        self.assertIn("at most 1024", first(GOOD_SKILL.replace("Drive the app.", "x" * 1025)))
        self.assertIn("without < or >", first(GOOD_SKILL.replace("Drive the app.", "Use <this>")))

    def test_a_body_is_needed_and_its_links_must_lead_somewhere(self) -> None:
        self.assertIn("no body", skills.check_skill("verify-waypoint", "---\nname: verify-waypoint\ndescription: x\n---\n\n")[0])
        self.assertEqual(skills.check_skill("verify-waypoint", GOOD_SKILL, exists=lambda rel: False),
                         [".claude/skills/verify-waypoint/SKILL.md: links to references/cli.md, which isn't there"])
        self.assertEqual(skills.check_skill("verify-waypoint", GOOD_SKILL.replace("references/cli.md", "https://x.dev/a"),
                                            exists=lambda rel: False), [])


class Generated(unittest.TestCase):
    def test_a_current_file_passes(self) -> None:
        self.assertEqual(skills.check_generated(CLI, "same\n", "same\n", "make print"), [])

    def test_a_hand_edited_missing_or_ungenerated_file_fails(self) -> None:
        self.assertIn("run make verify-skill and commit the result",
                      skills.check_generated(CLI, "old\n", "new\n", "make print", "make verify-skill")[0])
        self.assertIn("write what `make print` prints to it", skills.check_generated(CLI, "old\n", "new\n", "make print")[0])
        self.assertIn("missing", skills.check_generated(CLI, None, "new\n", "make print")[0])
        self.assertIn("couldn't generate it to compare (`make print`: node: not found)",
                      skills.check_generated(CLI, "x", None, "make print", why="node: not found")[0])

    def test_a_failing_or_missing_generator_is_reported(self) -> None:
        root = empty_consumer(self)
        self.assertEqual(skills.generate(root, "python3 -c 'print(1)'"), ("1\n", ""))
        output, why = skills.generate(root, "python3 -c 'import sys; sys.exit(\"broken\")'")
        self.assertIsNone(output)
        self.assertEqual(why, "broken")
        self.assertIsNone(skills.generate(root, "no-such-generator-anywhere")[0])

    def test_an_entry_without_a_command_is_a_config_error(self) -> None:
        with self.assertRaises(config.ConfigError):
            skills.check_generated_entries(empty_consumer(self), [{"path": "x.md"}])


class FixtureConsumers(unittest.TestCase):
    def test_both_consumers_pass(self) -> None:
        for fixture in (WAYPOINT, RUNWAY):
            with self.subTest(fixture=fixture.name):
                root = copy(self, fixture, repository=False)
                self.assertIn("skills", ran(root))
                self.assertEqual(checks(root), [])

    def test_a_malformed_skill_fires_in_runway(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        write(root, ".claude/skills/Bad/SKILL.md", "---\nname: bad\n---\n\nBody.\n")
        found = checks(root)
        self.assertIn(".claude/skills/Bad/SKILL.md: `name` should be 'Bad', its folder's name (lowercase letters, digits "
                      "and -), not 'bad'", found)
        self.assertIn(".claude/skills/Bad/SKILL.md: no `description` (it is what makes an agent use the skill)", found)

    def test_a_stale_generated_reference_fires_in_each_consumer(self) -> None:
        for fixture, skill in ((WAYPOINT, "verify-waypoint"), (RUNWAY, "verify-runway")):
            with self.subTest(fixture=fixture.name):
                root = copy(self, fixture, repository=False)
                path = f".claude/skills/{skill}/references/cli.md"
                write(root, path, "# edited by hand\n")
                self.assertEqual(checks(root), [f"{path} isn't what `make --no-print-directory -s verify-skill-print` "
                                                "generates: run make verify-skill and commit the result (edit what it is "
                                                "generated from, never the file)"])

    def test_a_stale_feature_map_fires_in_waypoint(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        write(root, "frontend/verify/verify.mjs", 'export const PAGES = ["upcoming", "trips", "people"];\n')
        self.assertEqual(checks(root), [".claude/skills/verify-waypoint/references/feature-map.md isn't what "
                                        "tools/user_feature_map.py generates: run make user-feature-map and commit the "
                                        "result"])

    def test_a_feature_map_generator_that_isnt_there_is_reported(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        (root / "tools/user_feature_map.py").unlink()
        self.assertEqual(checks(root), ["tools/user_feature_map.py: [tool.fleet] feature-maps names a generator that "
                                        "isn't there"])

    def test_silent_where_not_configured(self) -> None:
        root = empty_consumer(self)
        write(root, ".claude/skills/x/SKILL.md", "no frontmatter\n")
        self.assertNotIn("skills", ran(root))
        self.assertEqual(checks(root), [])


if __name__ == "__main__":
    unittest.main()
