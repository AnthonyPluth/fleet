import contextlib
import io
import os
import random
import unittest
from pathlib import Path
from unittest import mock

from consumers import RUNWAY, WAYPOINT, commit, copy, git, temporary, write

from fleet_tools import agents_md, cli, drift

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
                self.assertEqual(out, f"fleet drift: no drift: 4 AGENTS.md blocks and .fleet/version at {VERSION}; "
                                      f"fleet workflow pins: {VERSION}.\n")

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
        write(root, ".fleet/version", older(VERSION) + "\n")
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
        write(root, ".fleet/version", "v0.0.5\n")
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

    def test_an_unknown_tag_cannot_be_compared(self) -> None:
        fleet = self.fleet()
        self.assertIsNone(agents_md.sections_at("v9.9.9", fleet))
        self.assertIsNone(agents_md.sections_at("v9.9.9", temporary(self)))
