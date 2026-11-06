import contextlib
import io
import os
import random
import unittest
from pathlib import Path
from unittest import mock

from consumers import RUNWAY, WAYPOINT, commit, copy, git, temporary, write

from fleet_tools import agents_md, cli, drift, marketplace, vendor
from fleet_tools.release import FIXTURE_COMMIT

VERSION = agents_md.own_version()
PIN_FILE = ".github/workflows/merge-gate.yml"


def run(root: Path, actions: bool = False) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    env = {"GITHUB_ACTIONS": "true"} if actions else {}
    with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        if not actions:
            os.environ.pop("GITHUB_ACTIONS", None)
        code = cli.main(["drift", "--root", str(root)])
    return code, out.getvalue(), err.getvalue()


def older(version: str) -> str:
    x, y, z = agents_md.version_key(version)
    if z:
        return f"v{x}.{y}.{z - 1}"
    return f"v{x}.{y - 1}.9" if y else f"v{x - 1}.9.9"


def pin(root: Path, version: str) -> None:
    path = root / PIN_FILE
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(f"# {VERSION}", f"# {version}"), encoding="utf-8")


class Clean(unittest.TestCase):
    def test_each_fixture_consumer_has_no_drift(self) -> None:
        for fixture in (WAYPOINT, RUNWAY):
            with self.subTest(fixture=fixture.name):
                code, out, err = run(copy(self, fixture, repository=False))
                self.assertEqual((code, err), (0, ""))
                vendored = len(vendor.shipped_at(VERSION) or {})
                self.assertEqual(out, f"fleet drift: no drift: 4 AGENTS.md blocks, {vendored} vendored files and "
                                      f".fleet/version at {VERSION} (commit {FIXTURE_COMMIT}); "
                                      f"fleet workflow pins: {VERSION}.\n")

    def test_content_from_before_the_commit_line_needs_none(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        write(root, ".fleet/version", f"{VERSION}\n")
        problems, summary = drift.drift(root)
        if agents_md.needs_commit(VERSION):
            self.assertEqual(problems, [f".fleet/version names no commit for {VERSION} on its line 2, which tools/fleet runs "
                                        f"fleet at; run `fleet sync --to {VERSION}` from fleet at {VERSION}, which writes it"])
        else:
            self.assertEqual(problems, [])
            self.assertIn(f".fleet/version at {VERSION} (no commit: fleet {VERSION} wrote none)", summary)
        self.assertFalse(agents_md.needs_commit("v1.0.0"))
        self.assertTrue(agents_md.needs_commit("v1.0.1"))
        self.assertTrue(agents_md.needs_commit("v1.1.0"))

    def test_only_content_after_v1_0_0_needs_a_commit_whatever_this_version(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        for content, needs in (("v1.0.0", False), ("v1.1.0", True), ("v2.0.0", True)):
            with self.subTest(content=content):
                write(root, ".fleet/version", f"{content}\n")
                problems, _ = drift.drift(root)
                missing = f".fleet/version names no commit for {content} on its line 2"
                self.assertEqual(any(problem.startswith(missing) for problem in problems), needs, problems)

    def test_any_edit_outside_the_blocks_passes(self) -> None:
        rng = random.Random(4)
        for case in range(40):
            root = copy(self, rng.choice((WAYPOINT, RUNWAY)), repository=False)
            path = root / "AGENTS.md"
            text = path.read_text(encoding="utf-8")
            spans = [(block.start, block.end) for block in agents_md.blocks(text)]
            lines = [offset for offset, _ in agents_md.lines_with_offsets(text)] + [len(text)]
            free = [offset for offset in lines if not any(start < offset <= end for start, end in spans)]
            at = rng.choice(free)
            path.write_text(text[:at] + rng.choice(("An app line.\n", "\n", "## New heading\n", "- x\n")) + text[at:],
                            encoding="utf-8")
            with self.subTest(case=case):
                self.assertEqual(run(root)[0], 0, run(root)[2])

    def test_content_may_trail_the_pins_within_the_major(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        x, y, _ = agents_md.version_key(VERSION)
        pin(root, f"v{x}.{y + 1}.0")
        self.assertEqual(run(root)[0], 0)

    def test_no_fleet_pins_is_no_pin_check(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        (root / PIN_FILE).unlink()
        code, out, _ = run(root)
        self.assertEqual(code, 0)
        self.assertIn("fleet workflow pins: none.", out)


class Drifted(unittest.TestCase):
    def problems(self, root: Path) -> list[str]:
        return drift.drift(root)[0]

    def test_a_one_character_edit_inside_a_block_fails(self) -> None:
        rng = random.Random(7)
        for case in range(20):
            root = copy(self, rng.choice((WAYPOINT, RUNWAY)), repository=False)
            path = root / "AGENTS.md"
            text = path.read_text(encoding="utf-8")
            block = rng.choice(agents_md.blocks(text))
            at = rng.randrange(block.body_start, block.body_end)
            changed = text[:at] + ("x" if text[at] != "x" else "y") + text[at + 1:]
            path.write_text(changed, encoding="utf-8")
            with self.subTest(case=case):
                code, _, err = run(root)
                self.assertEqual(code, 1)
                self.assertIn(f"AGENTS.md: the {block.name} block differs from fleet's at {VERSION}", err)

    def test_an_added_line_inside_a_block_fails(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        path = root / "AGENTS.md"
        path.write_text(path.read_text(encoding="utf-8").replace("<!-- fleet:end mistakes -->",
                                                                 "- **Mine**.\n<!-- fleet:end mistakes -->"),
                        encoding="utf-8")
        self.assertEqual(self.problems(root), [f"AGENTS.md: the mistakes block differs from fleet's at {VERSION}; edit it "
                                               "in fleet (agents-md/sections/mistakes.md), not here, or run `fleet sync`"])

    def test_a_marker_version_that_differs_from_the_version_file_fails(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        write(root, ".fleet/version", f"{older(VERSION)}\n{FIXTURE_COMMIT}\n")
        problems = self.problems(root)
        self.assertIn(f"fleet content is at more than one version: .fleet/version {older(VERSION)}, AGENTS.md "
                      f"starting-work {VERSION}, working-in-a-container {VERSION}, mistakes {VERSION}, git-and-prs "
                      f"{VERSION}; run `fleet sync` at one version", problems)

    def test_markers_at_two_versions_fail(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        path = root / "AGENTS.md"
        path.write_text(path.read_text(encoding="utf-8").replace(f"mistakes {VERSION} -->", "mistakes v0.0.1 -->"),
                        encoding="utf-8")
        problems = self.problems(root)
        self.assertTrue(any("more than one version" in problem and "mistakes v0.0.1" in problem for problem in problems))
        self.assertIn(f"AGENTS.md: can't read fleet's mistakes block at v0.0.1 from fleet {VERSION}; run fleet drift "
                      "from fleet at v0.0.1", problems)

    def test_content_ahead_of_the_workflow_pins_fails(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        pin(root, older(VERSION))
        self.assertEqual(self.problems(root), [f"fleet content {VERSION} is newer than the workflow pin {older(VERSION)} "
                                               f"in {PIN_FILE}"])

    def test_pins_in_another_major_fail(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        x, _, _ = agents_md.version_key(VERSION)
        pin(root, f"v{x + 1}.0.0")
        self.assertEqual(self.problems(root), [f"fleet content {VERSION} is another major version than the workflow pin "
                                               f"v{x + 1}.0.0 in {PIN_FILE}"])

    def test_a_pin_without_a_version_comment_fails(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        path = root / PIN_FILE
        path.write_text(path.read_text(encoding="utf-8").replace(f"   # {VERSION}", ""), encoding="utf-8")
        self.assertEqual(self.problems(root), [f"{PIN_FILE}: the fleet pin @0123456789abcdef0123456789abcdef01234567 has "
                                               "no version in a comment (`# vX.Y.Z`)"])

    def test_a_commit_that_differs_from_the_pins_fails(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        write(root, ".fleet/version", f"{VERSION}\n{'f' * 40}\n")
        self.assertEqual(self.problems(root), [
            f".fleet/version says fleet {VERSION} is commit {'f' * 40}, but the workflow pins at {VERSION} in {PIN_FILE} name "
            f"{FIXTURE_COMMIT}; run `fleet sync --to {VERSION}`, which takes the pins' commit"])

    def test_a_commit_is_compared_only_with_pins_at_the_contents_version(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        x, y, _ = agents_md.version_key(VERSION)
        pin(root, f"v{x}.{y + 1}.0")
        write(root, ".fleet/version", f"{VERSION}\n{'f' * 40}\n")
        self.assertEqual(self.problems(root), [])

    def test_a_pin_at_the_contents_version_by_a_movable_ref_fails(self) -> None:
        for ref in ("main", VERSION):
            with self.subTest(ref=ref):
                root = copy(self, WAYPOINT, repository=False)
                write(root, ".github/workflows/other.yml", f"jobs:\n  x:\n    uses: AnthonyPluth/fleet/.github/workflows/checks.yml@{ref}   # {VERSION}\n")
                self.assertEqual(self.problems(root), [
                    f".github/workflows/other.yml: the fleet pin at {VERSION} is @{ref}, a ref that can move; pin it by full commit "
                    "SHA (`@<sha>   # vX.Y.Z`)"])
                path = root / PIN_FILE
                path.write_text(path.read_text(encoding="utf-8").replace(FIXTURE_COMMIT, ref), encoding="utf-8")
                self.assertEqual(len(self.problems(root)), 2)

    def test_pins_at_one_version_naming_two_commits_fail(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        write(root, ".github/workflows/other.yml", f"jobs:\n  x:\n    uses: AnthonyPluth/fleet/.github/workflows/checks.yml@{'f' * 40}   # {VERSION}\n")
        self.assertEqual(self.problems(root), [
            f"the fleet pins at {VERSION} name more than one commit: {FIXTURE_COMMIT} in {PIN_FILE}; {'f' * 40} in "
            ".github/workflows/other.yml; bump them together (Dependabot's fleet group does)"])

    def test_a_malformed_version_file_fails_naming_the_line(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        for text, problem in ((f"{VERSION}\nabc\n", ".fleet/version line 2 holds 'abc', not a commit (40 hex digits)"),
                              ("latest\n", ".fleet/version holds 'latest', not a version (vX.Y.Z)"),
                              (f"{VERSION}\n{FIXTURE_COMMIT}\nmore\n", ".fleet/version has 3 lines; it holds the version (line 1) "
                                                                        "and the commit it is at (line 2)")):
            with self.subTest(text=text):
                write(root, ".fleet/version", text)
                problems = self.problems(root)
                self.assertIn(f"{problem}; run `fleet sync`", problems)
                self.assertEqual(len(problems), 1, problems)

    def test_spaces_and_tabs_around_the_commit_pass_as_the_shim_reads_them(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        for label, line in (("a leading space", f" {FIXTURE_COMMIT}"), ("a trailing space", f"{FIXTURE_COMMIT} "),
                            ("a leading tab", f"\t{FIXTURE_COMMIT}"), ("a trailing tab", f"{FIXTURE_COMMIT}\t")):
            with self.subTest(label):
                write(root, ".fleet/version", f"{VERSION}\n{line}\n")
                self.assertEqual(self.problems(root), [])

    def test_whitespace_the_shim_keeps_fails_naming_the_line(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        for label, line in (("a trailing carriage return", f"{FIXTURE_COMMIT}\r"), ("a leading carriage return", f"\r{FIXTURE_COMMIT}"),
                            ("a trailing form feed", f"{FIXTURE_COMMIT}\f"), ("a carriage return after a space", f"{FIXTURE_COMMIT} \r")):
            with self.subTest(label):
                write(root, ".fleet/version", f"{VERSION}\n{line}\n")
                problems = self.problems(root)
                self.assertEqual(len(problems), 1, problems)
                self.assertTrue(problems[0].startswith(f".fleet/version line 2 holds {line!r}: "), problems)
                self.assertTrue(problems[0].endswith("; run `fleet sync`"), problems)

    def test_a_missing_block_version_file_or_agents_md_fails(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        path = root / "AGENTS.md"
        text = path.read_text(encoding="utf-8")
        block = next(each for each in agents_md.blocks(text) if each.name == "git-and-prs")
        path.write_text(text[:block.start] + text[block.end:], encoding="utf-8")
        (root / ".fleet/version").unlink()
        self.assertEqual(self.problems(root), [f"AGENTS.md: no git-and-prs block (fleet sync --to {VERSION} inserts it)",
                                               ".fleet/version doesn't exist (fleet sync writes it)"])
        path.unlink()
        self.assertIn("AGENTS.md doesn't exist (fleet sync writes its blocks)", self.problems(root))

    def test_a_broken_marker_is_reported(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        path = root / "AGENTS.md"
        path.write_text(path.read_text(encoding="utf-8").replace("<!-- fleet:end mistakes -->\n", ""), encoding="utf-8")
        self.assertEqual(run(root)[0], 1)

    def test_problems_are_annotations_on_github_actions(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        pin(root, older(VERSION))
        code, _, err = run(root, actions=True)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("::error::fleet content "))

    def test_no_root_exits_2(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), mock.patch.object(Path, "cwd", return_value=Path("/")):
            self.assertEqual(cli.main(["drift"]), 2)


class OtherVersions(unittest.TestCase):
    def fleet(self) -> Path:
        root = temporary(self) / "fleet"
        for name, section in agents_md.sections().items():
            front = f"---\nheading: {section.heading}\n---\n"
            write(root, f"agents-md/sections/{name}.md", front + section.body.replace("Layout", "Old layout"))
        git(root, "init", "-q", "-b", "main")
        commit(root, "chore: an older fleet")
        git(root, "tag", "v0.0.5")
        return root

    def consumer(self, fleet: Path) -> Path:
        root = copy(self, WAYPOINT, repository=False)
        with mock.patch.object(agents_md, "ROOT", fleet):
            old = agents_md.sections_at("v0.0.5", fleet)
        assert old is not None
        path = root / "AGENTS.md"
        path.write_text(agents_md.sync(path.read_text(encoding="utf-8"), old, "v0.0.5")[0], encoding="utf-8")
        write(root, ".fleet/version", f"v0.0.5\n{FIXTURE_COMMIT}\n")
        pin(root, "v0.0.5")
        marketplace.sync(root, "v0.0.5")
        return root

    def test_blocks_at_an_older_tag_are_read_from_fleets_history(self) -> None:
        fleet = self.fleet()
        root = self.consumer(fleet)
        with mock.patch.object(agents_md, "ROOT", fleet), mock.patch.object(agents_md, "own_version", return_value=VERSION):
            self.assertEqual(drift.drift(root)[0], [])
            path = root / "AGENTS.md"
            path.write_text(path.read_text(encoding="utf-8").replace("**Old layout**", "**Layout**"), encoding="utf-8")
            self.assertEqual(drift.drift(root)[0], ["AGENTS.md: the mistakes block differs from fleet's at v0.0.5; edit "
                                                    "it in fleet (agents-md/sections/mistakes.md), not here, or run "
                                                    "`fleet sync`"])

    def test_fleets_own_version_is_read_from_the_working_tree_and_another_from_its_tag(self) -> None:
        fleet = self.fleet()
        path = fleet / "agents-md/sections/mistakes.md"
        path.write_text(path.read_text(encoding="utf-8").replace("Old layout", "Newer layout"), encoding="utf-8")
        with mock.patch.object(agents_md, "own_version", return_value="v0.0.5"):
            own = agents_md.sections_at("v0.0.5", fleet)
        with mock.patch.object(agents_md, "own_version", return_value=VERSION):
            released = agents_md.sections_at("v0.0.5", fleet)
        assert own is not None and released is not None
        self.assertIn("Newer layout", own["mistakes"].body)
        self.assertNotIn("Old layout", own["mistakes"].body)
        self.assertIn("Old layout", released["mistakes"].body)
        self.assertNotIn("Newer layout", released["mistakes"].body)

    def test_a_consumer_synced_between_releases_matches_the_working_tree_not_the_tag(self) -> None:
        fleet = self.fleet()
        path = fleet / "agents-md/sections/mistakes.md"
        path.write_text(path.read_text(encoding="utf-8").replace("Old layout", "Newer layout"), encoding="utf-8")
        root = copy(self, WAYPOINT, repository=False)
        with mock.patch.object(agents_md, "ROOT", fleet), mock.patch.object(agents_md, "own_version", return_value="v0.0.5"):
            agents = root / "AGENTS.md"
            agents.write_text(agents_md.sync(agents.read_text(encoding="utf-8"), agents_md.sections(fleet), "v0.0.5")[0], encoding="utf-8")
            write(root, ".fleet/version", f"v0.0.5\n{FIXTURE_COMMIT}\n")
            pin(root, "v0.0.5")
            marketplace.sync(root, "v0.0.5")
            self.assertEqual(drift.drift(root)[0], [])

    def test_an_unknown_tag_cannot_be_compared(self) -> None:
        fleet = self.fleet()
        self.assertIsNone(agents_md.sections_at("v9.9.9", fleet))
        self.assertIsNone(agents_md.sections_at("v9.9.9", temporary(self)))
