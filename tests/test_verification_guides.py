import contextlib
import io
import json
import random
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from consumers import RUNWAY, WAYPOINT, copy, write

from fleet_tools import cli, config
from fleet_tools.verification_guides import (
    Guide,
    GuideError,
    areas,
    directory,
    load,
    parse_guide,
)

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "guides/area.md"
VALID = """# Settings

The settings pages.

## Paths

- `src/settings/**`
- tests/test_settings.py
- `**/settings_*.json`

## How to verify

- `make verify`, flow `settings-toggle`: the page at both widths,
  with screenshots.
- `tests/test_settings.py`

## Not verifiable yet

- The email on a change: no fake mail server; a fake would make it testable.
"""


def guide(text: str = VALID, name: str = "area") -> Guide:
    return parse_guide(text, SOURCE, name)


def error_of(text: str) -> GuideError:
    with unittest.TestCase().assertRaises(GuideError) as caught:
        guide(text)
    return caught.exception


def run(*args: str, stdin: bytes = b"") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    piped = io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", piped):
        code = cli.main(["verification", *args])
    return code, out.getvalue(), err.getvalue()


class Parse(unittest.TestCase):
    def test_a_valid_guide(self) -> None:
        found = guide()
        self.assertEqual((found.name, found.area, found.source), ("area", "Settings", SOURCE))
        self.assertEqual([glob.text for glob in found.paths], ["src/settings/**", "tests/test_settings.py", "**/settings_*.json"])
        self.assertEqual([glob.line for glob in found.paths], [7, 8, 9])
        self.assertEqual(found.steps, ("`make verify`, flow `settings-toggle`: the page at both widths, with screenshots.",
                                       "`tests/test_settings.py`"))
        self.assertEqual(len(found.gaps), 1)

    def test_nothing_is_accepted_alone(self) -> None:
        text = VALID.split("## Not verifiable yet")[0] + "## Not verifiable yet\n\n- Nothing\n"
        self.assertEqual(guide(text).gaps, ())
        self.assertEqual(guide(text.replace("- Nothing", "* Nothing.")).gaps, ())
        error = error_of(text + "- The email: no fake.\n")
        self.assertIn("`Nothing` stands alone", error.rule)

    def test_a_missing_section(self) -> None:
        for name in ("Paths", "How to verify", "Not verifiable yet"):
            with self.subTest(name=name):
                text = VALID.replace(f"## {name}\n", "")
                error = error_of(text)
                self.assertTrue(f"no `## {name}` section" in error.rule or "isn't a list item" in error.rule
                                or "comes before" in error.rule, error.rule)
        self.assertIn("no `## Not verifiable yet` section", error_of(VALID.split("## Not verifiable yet")[0]).rule)

    def test_empty_sections(self) -> None:
        paths = VALID.replace("- `src/settings/**`\n- tests/test_settings.py\n- `**/settings_*.json`\n", "")
        self.assertIn("`## Paths` lists no globs", error_of(paths).rule)
        steps = VALID.replace("- `make verify`, flow `settings-toggle`: the page at both widths,\n  with screenshots.\n"
                              "- `tests/test_settings.py`\n", "")
        self.assertIn("`## How to verify` lists no steps", error_of(steps).rule)
        gaps = VALID.split("## Not verifiable yet")[0] + "## Not verifiable yet\n"
        self.assertIn("or the single item `Nothing`", error_of(gaps).rule)

    def test_the_title_and_the_order(self) -> None:
        self.assertEqual(error_of("Settings\n" + VALID.split("\n", 1)[1]).line, 1)
        self.assertEqual(error_of("#Settings\n" + VALID.split("\n", 1)[1]).line, 1)
        swapped = VALID.replace("## How to verify", "## TEMP").replace("## Paths", "## How to verify").replace("## TEMP", "## Paths")
        self.assertIn("`## How to verify` comes before `## Paths`", error_of(swapped).rule)
        self.assertIn("a second `## Paths`", error_of(VALID + "\n## Paths\n").rule)
        self.assertIn("an unknown heading", error_of(VALID + "\n## Notes\n").rule)
        self.assertIn("an unknown heading", error_of(VALID.replace("## Paths", "### Paths")).rule)

    def test_lines_in_a_section_are_list_items(self) -> None:
        error = error_of(VALID.replace("- tests/test_settings.py", "tests/test_settings.py"))
        self.assertEqual((error.line, "isn't a list item" in error.rule), (8, True))
        self.assertIn("a code fence", error_of(VALID.replace("- `tests/test_settings.py`", "```\nmake test\n```")).rule)
        self.assertEqual(guide(VALID.replace("\n", "\r\n")).area, "Settings")

    def test_bad_globs(self) -> None:
        for glob in ("/src/**", "./src/**", "!src/**", "src/", "src/../x", "src//x", "src/**.py", "a**/b", "src/[ab].py",
                     "src/{a,b}", "two words", "back\\slash", "``"):
            with self.subTest(glob=glob):
                error = error_of(VALID.replace("- tests/test_settings.py", f"- {glob}"))
                self.assertEqual(error.line, 8)

    def test_size_limits(self) -> None:
        self.assertIn("over 65,536 characters", error_of(VALID + "x" * 65536).rule)
        many = VALID.replace("- tests/test_settings.py\n", "".join(f"- a/{index}\n" for index in range(201)))
        self.assertIn("over 200 items", error_of(many).rule)

    def test_the_skill_s_example_parses(self) -> None:
        text = (ROOT / "plugins" / "fleet" / "skills" / "verification-guide" / "SKILL.md").read_text(encoding="utf-8")
        example = text.split("```md\n", 1)[1].split("```", 1)[0]
        found = guide(example)
        self.assertEqual(found.area, "Settings")
        self.assertEqual(len(found.gaps), 1)


class Matching(unittest.TestCase):
    def test_globs(self) -> None:
        found = guide()
        for path, covered in (("src/settings/a.py", True), ("src/settings/deep/er/b.py", True), ("src/settings", False),
                              ("src/settingsx/a.py", False), ("tests/test_settings.py", True), ("tests/test_settings.pyc", False),
                              ("settings_a.json", True), ("x/y/settings_b.json", True), ("x/settings_b.jsonx", False)):
            with self.subTest(path=path):
                self.assertEqual(found.covers(path), covered)

    def test_star_and_question_mark_stay_in_one_directory(self) -> None:
        text = VALID.replace("- tests/test_settings.py", "- tests/test_?.py\n- src/*.py")
        found = guide(text)
        self.assertTrue(found.covers("tests/test_a.py"))
        self.assertFalse(found.covers("tests/test_ab.py"))
        self.assertTrue(found.covers("src/x.py"))
        self.assertFalse(found.covers("src/x/y.py"))
        self.assertTrue(found.covers("src/.hidden.py"))

    def test_alternating_wildcards_dont_backtrack_exponentially(self) -> None:
        found = guide(VALID.replace("- tests/test_settings.py", "- `src/*?*?*?*?*?*?*?*?*?*?*?*?a`\n- `**/*?*?*?*?*?*?*?*?b`"))
        started = time.monotonic()
        self.assertFalse(found.covers("src/" + "x" * 10000))
        self.assertFalse(found.covers("/".join(["y" * 200] * 60) + "/" + "x" * 10000))
        self.assertTrue(found.covers("src/" + "x" * 10000 + "a"))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_double_star_across_many_segments(self) -> None:
        found = guide(VALID.replace("- tests/test_settings.py", "- `a/**/b/**/c/*.py`\n- `**/x/**`"))
        deep = "/".join(f"d{index}" for index in range(60))
        for path, covered in ((f"a/{deep}/b/{deep}/c/m.py", True), ("a/b/c/m.py", True), (f"a/{deep}/c/m.py", False),
                              (f"a/{deep}/b/c/sub/m.py", False), (f"{deep}/x/{deep}", True), ("x/y", True), ("x", False),
                              ("q/x", False), ("q/x/", False)):
            with self.subTest(path=path[:40]):
                self.assertEqual(found.covers(path), covered)

    def test_the_matcher_agrees_with_a_reference_regex(self) -> None:
        def reference(glob: str) -> re.Pattern[str]:
            parts = glob.split("/")
            out = []
            for index, part in enumerate(parts):
                last = index == len(parts) - 1
                if part == "**":
                    out.append(".+" if last else "(?:[^/]+/)*")
                    continue
                piece = "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in part)
                out.append(piece if last else piece + "/")
            return re.compile("".join(out), re.DOTALL)

        chooser = random.Random(19)
        pieces = ["a", "b", "*", "?", "ab", "*a", "a*", "?b*"]
        for _ in range(3000):
            segments = ["**" if chooser.random() < 0.25 else "".join(chooser.choices(pieces, k=chooser.randint(1, 3)))
                        for _ in range(chooser.randint(1, 4))]
            if any("**" in segment and segment != "**" for segment in segments):
                continue
            glob = "/".join(segments)
            path = "/".join("".join(chooser.choices("ab\n", k=chooser.randint(1, 4))) for _ in range(chooser.randint(1, 5)))
            found = guide(VALID.replace("- tests/test_settings.py", f"- `{glob}`"))
            with self.subTest(glob=glob, path=path):
                self.assertEqual(found.paths[1].matches(path), reference(glob).fullmatch(path) is not None)

    def test_long_globs_are_refused(self) -> None:
        self.assertIn("over 1000 characters or 64 segments", error_of(VALID.replace("- tests/test_settings.py", "- " + "a" * 1001)).rule)
        self.assertIn("over 1000 characters", error_of(VALID.replace("- tests/test_settings.py", "- " + "/".join(["a"] * 65))).rule)

    def test_areas_returns_every_matching_area_and_the_uncovered(self) -> None:
        other = parse_guide(VALID.replace("# Settings", "# Tests").replace("src/settings/**", "tests/**"), "g/tests.md", "tests")
        found, uncovered = areas([guide(), other], ["./tests/test_settings.py", "README.md", "src/settings/x.py", "README.md"])
        self.assertEqual([(each.name, paths) for each, paths in found],
                         [("area", ["tests/test_settings.py", "src/settings/x.py"]), ("tests", ["tests/test_settings.py"])])
        self.assertEqual(uncovered, ["README.md"])

    def test_both_names_of_a_renamed_path_count(self) -> None:
        old = parse_guide(VALID.replace("src/settings/**", "old/**"), "g/old.md", "old")
        new = parse_guide(VALID.replace("src/settings/**", "new/**"), "g/new.md", "new")
        found, uncovered = areas([old, new], ["old/module.py", "new/module.py"])
        self.assertEqual([each.name for each, _ in found], ["old", "new"])
        self.assertEqual(uncovered, [])


class Loading(unittest.TestCase):
    def test_the_fixture_s_guides(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        relative = directory(config.load(root))
        self.assertEqual(relative, "docs/verification")
        found, errors = load(root, "docs/verification")
        self.assertEqual(errors, [])
        self.assertEqual([(each.name, each.area) for each in found], [("migrations", "Migrations"), ("screens", "Screens and flows")])
        self.assertEqual(found[0].gaps, ())

    def test_bad_files_are_errors_and_the_rest_still_load(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        write(root, "docs/verification/Bad_Name.md", VALID)
        write(root, "docs/verification/broken.md", "# Broken\n")
        (root / "docs/verification/latin.md").write_bytes(b"# Caf\xe9\n")
        found, errors = load(root, "docs/verification")
        self.assertEqual(len(found), 2)
        self.assertEqual(sorted(error.source for error in errors),
                         ["docs/verification/Bad_Name.md", "docs/verification/broken.md", "docs/verification/latin.md"])

    def test_a_linked_guide_is_refused(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        write(root, "elsewhere.md", VALID)
        (root / "docs/verification/linked.md").symlink_to(root / "elsewhere.md")
        _, errors = load(root, "docs/verification")
        self.assertEqual([error.source for error in errors], ["docs/verification/linked.md"])
        self.assertIn("not a link", errors[0].rule)

    def test_a_missing_empty_or_outside_directory(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        self.assertIn("doesn't exist", load(root, "nowhere")[1][0].rule)
        (root / "empty").mkdir()
        self.assertIn("holds no guides", load(root, "empty")[1][0].rule)
        (root / "out").symlink_to(root.parent)
        self.assertIn("doesn't exist", load(root, "out")[1][0].rule)

    def test_the_key(self) -> None:
        for value in ('"."', '"./"', '"/abs"', '"../up"', '"a\\\\b"', '""', "3", "[\"a\"]"):
            with self.subTest(value=value):
                root = copy(self, RUNWAY, repository=False)
                pyproject = root / "pyproject.toml"
                pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('"docs/verification"', value), encoding="utf-8")
                with self.assertRaises(config.ConfigError):
                    directory(config.load(root))
        self.assertIsNone(directory(config.load(WAYPOINT)))


class Commands(unittest.TestCase):
    def test_guides_lists_the_areas_and_their_globs(self) -> None:
        code, out, err = run("guides", "--root", str(RUNWAY))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("migrations  Migrations  (docs/verification/migrations.md)\n    runway/storage/migrations/**\n", out)
        code, out, _ = run("guides", "--root", str(RUNWAY), "--json")
        data = json.loads(out)
        self.assertEqual(data["guides"], "docs/verification")
        self.assertEqual([each["name"] for each in data["areas"]], ["migrations", "screens"])
        self.assertEqual(data["areas"][0]["not_verifiable_yet"], [])
        self.assertEqual(data["areas"][1]["paths"], ["frontend/verify/**", "runway/static/**", "screenshots/**"])

    def test_areas_names_the_matching_areas(self) -> None:
        code, out, err = run("areas", "--root", str(RUNWAY), "--json", "runway/storage/migrations/versions/0002_new.py",
                             "frontend/verify/flows/lock.json", "README.md")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual([(each["name"], each["matched"]) for each in data["areas"]],
                         [("migrations", ["runway/storage/migrations/versions/0002_new.py"]),
                          ("screens", ["frontend/verify/flows/lock.json"])])
        self.assertEqual(data["uncovered"], ["README.md"])
        code, out, _ = run("areas", "--root", str(RUNWAY), "tests/test_migrations.py", "Makefile")
        self.assertEqual(out, "migrations  Migrations  (docs/verification/migrations.md)\n    tests/test_migrations.py\n"
                              "no guide covers:\n    Makefile\n")

    def test_paths_after_a_double_dash_are_paths_even_when_they_look_like_options(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        guide_file = root / "docs/verification/migrations.md"
        guide_file.write_text(guide_file.read_text(encoding="utf-8").replace("- `tests/test_migrations.py`",
                                                                              "- `tests/test_migrations.py`\n- `--json`"), encoding="utf-8")
        code, out, err = run("areas", "--root", str(root), "--json", "--", "--root", "-h", "--json", "tests/test_migrations.py")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual([(each["name"], each["matched"]) for each in data["areas"]],
                         [("migrations", ["--json", "tests/test_migrations.py"])])
        self.assertEqual(data["uncovered"], ["--root", "-h"])
        code, out, _ = run("areas", "--root", str(root), "--", "-h")
        self.assertEqual((code, out), (0, "no guide covers:\n    -h\n"))

    def test_stdin0_reads_any_number_of_paths_in_one_run(self) -> None:
        paths = [f"runway/storage/migrations/versions/m{index}.py" for index in range(25000)]
        paths += [f"src/x{index}.py" for index in range(25000)]
        code, out, err = run("areas", "--root", str(RUNWAY), "--json", "--stdin0", stdin="\0".join(paths).encode() + b"\0")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual([(each["name"], len(each["matched"])) for each in data["areas"]], [("migrations", 25000)])
        self.assertEqual(len(data["uncovered"]), 25000)
        self.assertEqual(data["uncovered"][-1], "src/x24999.py")

    def test_stdin0_keeps_odd_paths_whole(self) -> None:
        odd = ["runway/storage/migrations/versions/new\nline.py", "with space.py", "--root", "-h", "--json", "tests/test_migrations.py"]
        code, out, err = run("areas", "--root", str(RUNWAY), "--json", "--stdin0", "Makefile",
                             stdin="\0".join(odd).encode())
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual([(each["name"], each["matched"]) for each in data["areas"]],
                         [("migrations", [odd[0], "tests/test_migrations.py"])])
        self.assertEqual(data["uncovered"], ["Makefile", "with space.py", "--root", "-h", "--json"])

    def test_stdin0_with_empty_input_and_no_paths_at_all(self) -> None:
        code, out, err = run("areas", "--root", str(RUNWAY), "--json", "--stdin0")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"areas": [], "uncovered": []})
        self.assertEqual(run("areas", "--root", str(RUNWAY), "--stdin0"), (0, "", ""))
        code, out, err = run("areas", "--root", str(RUNWAY))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("areas needs a PATH, or --stdin0", err)

    def test_stdin0_over_the_limit_is_an_error(self) -> None:
        code, out, err = run("areas", "--root", str(RUNWAY), "--stdin0", stdin=b"a" * (16 * 1024 * 1024 + 1))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("over 16,777,216 bytes", err)

    def test_without_the_key_or_with_a_broken_guide(self) -> None:
        code, _, err = run("guides", "--root", str(WAYPOINT))
        self.assertEqual(code, 2)
        self.assertIn("guides isn't set", err)
        root = copy(self, RUNWAY, repository=False)
        write(root, "docs/verification/broken.md", VALID.replace("# Settings", "# \x1b[31m").replace("- tests/test_settings.py", "- \x1b[31m"))
        code, out, err = run("areas", "--root", str(root), "x")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("docs/verification/broken.md, line 8:", err)
        self.assertNotIn("\x1b", err)

    def test_show_still_works(self) -> None:
        code, _, err = run("show", "--body-file", str(ROOT / "nowhere.md"))
        self.assertEqual(code, 1)
        self.assertIn("can't read the description", err)


class Skills(unittest.TestCase):
    def test_the_guide_skill_names_no_app_and_holds_the_rules(self) -> None:
        text = (ROOT / "plugins" / "fleet" / "skills" / "verification-guide" / "SKILL.md").read_text(encoding="utf-8")
        for phrase in ("name: verification-guide", "[tool.fleet.verification]", "## Paths", "## How to verify",
                       "## Not verifiable yet", "`Nothing`", "run once in this session", "feature map", "git ls-files"):
            self.assertIn(phrase, text)
        for word in ("Waypoint", "Runway", "Darkroom", "AnthonyPluth", "http"):
            self.assertNotIn(word, text)

    def test_verify_change_says_how_it_uses_the_guides(self) -> None:
        text = (ROOT / "plugins" / "fleet" / "skills" / "verify-change" / "SKILL.md").read_text(encoding="utf-8")
        for phrase in ("## Read the area guides first", "git diff -z --name-only --no-renames origin/main...HEAD | fleet verification areas --json --stdin0`", "`## How to verify`",
                       "`## Not verifiable yet`", "says why in its How column", "fleet-verification-guide"):
            self.assertIn(phrase, text)

    def test_the_fixture_consumers_carry_it_vendored_and_the_docs_describe_it(self) -> None:
        for consumer in ("consumer-runway", "consumer-waypoint"):
            path = ROOT / "tests" / "fixtures" / consumer / ".claude" / "skills" / "fleet-verification-guide" / "SKILL.md"
            self.assertIn("name: fleet-verification-guide", path.read_text(encoding="utf-8"))
        text = (ROOT / "docs" / "adopting.md").read_text(encoding="utf-8")
        for phrase in ('guides = "docs/verification"', "fleet verification guides [--json]", "fleet verification areas [PATH...] [--stdin0] [--json]", "fleet verification areas --json --stdin0",
                       "verification-guides", "`Nothing`"):
            self.assertIn(phrase, text)
        example = text.split("### Verification guides", 1)[1].split("```md\n", 1)[1].split("```", 1)[0]
        self.assertEqual(guide(example).area, "Settings")


if __name__ == "__main__":
    unittest.main()
