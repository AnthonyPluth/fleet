import contextlib
import io
import os
import re
import subprocess
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from consumers import commit, git, temporary, write

from fleet_tools import cli, release, self_check

ROOT = cli.ROOT
CI = ROOT / ".github" / "workflows" / "ci.yml"
FROZEN_CHANGELOG = f"# Changelog\n\n## [{release.FROZEN}] - 2026-11-14\n\n### Added\n\n- The last entry here.\n"
LATER = "4.1.0"
Node = dict[str, Any]


def fragment_text(kind: str, entry: str) -> str:
    return f"---\nkind: {kind}\n---\n{entry}\n"


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def mapping(node: object) -> Node:
    assert isinstance(node, dict), node
    return node


class FragmentFormatTest(unittest.TestCase):
    def parse(self, text: str) -> release.Fragment:
        return release.parse_fragment(Path("changelog.d/x.md"), text)

    def test_a_fragment_is_its_kind_and_its_entry(self) -> None:
        self.assertEqual(self.parse(fragment_text("added", "`fleet x` (#1): it does y.")),
                         release.Fragment(Path("changelog.d/x.md"), "added", "`fleet x` (#1): it does y."))
        self.assertEqual(self.parse("---\nkind: fixed\n---\n\nA fix.  \n\n").entry, "A fix.")
        self.assertEqual(self.parse("---\nkind: changed\n---\nNo newline at the end").entry, "No newline at the end")
        for entry in ("#112 moved.", "--own-commit is new.", "3.0.0's entry.", "**Bold** start.", "`- ` in code."):
            with self.subTest(entry=entry):
                self.assertEqual(self.parse(fragment_text("changed", entry)).entry, entry)

    def test_each_refusal(self) -> None:
        cases = {
            "kind: added\nA change.\n": "a fragment is `---`, `kind: <breaking|added|changed|fixed>`, `---`, then the entry on one line",
            "---\nkind: added\nA change.\n": "its front matter never closes",
            "---\nkind: added\nissue: 1\n---\nA change.\n": "its front matter is one line, `kind: <breaking|added|changed|fixed>`",
            "---\ntype: added\n---\nA change.\n": "its front matter is one line",
            "---\n---\nA change.\n": "its front matter is one line",
            fragment_text("feature", "A change."): "kind 'feature' isn't one of breaking, added, changed, fixed",
            fragment_text("Added", "A change."): "kind 'Added' isn't one of",
            "---\nkind: added\n---\n\n": "has no entry after its front matter",
            "---\nkind: added\n---\nA change\nwrapped.\n": "its entry is one line",
            "---\nkind: added\n---\nOne.\n\nTwo.\n": "its entry is one line",
            fragment_text("added", "  Indented."): "its entry starts with a space",
            fragment_text("added", "- A bullet."): "with no list marker or heading",
            fragment_text("added", "* A bullet."): "with no list marker or heading",
            fragment_text("added", "1. A number."): "with no list marker or heading",
            fragment_text("added", "### Added"): "with no list marker or heading",
            fragment_text("breaking", "**Breaking:** twice."): "its entry starts with **Breaking; `kind: breaking` adds",
            fragment_text("changed", "**Breaking (pre-1.0):** old."): "its entry starts with **Breaking",
            "---\r\nkind: added\r\n---\r\nA change.\r\n": "has a carriage return",
        }
        for text, needle in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(release.ReleaseError) as raised:
                    self.parse(text)
                self.assertTrue(str(raised.exception).startswith("changelog.d/x.md: "), raised.exception)
                self.assertIn(needle, str(raised.exception))

    def test_the_folder_holds_fragments_and_released_only(self) -> None:
        root = temporary(self)
        write(root, "changelog.d/good-one.md", fragment_text("added", "Good."))
        write(root, "changelog.d/Bad_Name.md", fragment_text("added", "Bad."))
        write(root, "changelog.d/notes.txt", "Not a fragment.\n")
        write(root, "changelog.d/sub/x.md", fragment_text("added", "Nested."))
        write(root, "changelog.d/.DS_Store", "")
        write(root, "changelog.d/released/4.1.0.md", "## [4.1.0] - 2026-11-20\n\n### Added\n\n- Good.\n")
        (root / "changelog.d/latin.md").write_bytes(b"---\nkind: added\n---\n\xff\n")
        (root / "changelog.d/linked.md").symlink_to(root / "changelog.d/good-one.md")
        found, errors = release.fragments(root)
        self.assertEqual([fragment.path.as_posix() for fragment in found], ["changelog.d/good-one.md"])
        self.assertEqual(errors, [
            "changelog.d/Bad_Name.md: a fragment's name is lowercase letters, digits and hyphens, then `.md`",
            "changelog.d/latin.md: isn't UTF-8",
            "changelog.d/linked.md: isn't a fragment; changelog.d/ holds fragments (<name>.md) and released/",
            "changelog.d/notes.txt: a fragment's name is lowercase letters, digits and hyphens, then `.md`",
            "changelog.d/sub: isn't a fragment; changelog.d/ holds fragments (<name>.md) and released/",
        ])

    def test_no_folder_is_no_fragments(self) -> None:
        self.assertEqual(release.fragments(temporary(self)), ([], []))
        root = temporary(self)
        write(root, "changelog.d", "a file\n")
        self.assertEqual(release.fragments(root), ([], ["changelog.d: isn't a directory"]))

    def test_render_groups_by_kind_in_the_changelogs_format(self) -> None:
        found = [release.Fragment(Path(f"changelog.d/{name}.md"), kind, entry) for name, kind, entry in (
            ("b", "fixed", "Fixed b."), ("a", "changed", "Changed a."), ("c", "breaking", "Broke c."), ("d", "added", "Added d."))]
        self.assertEqual(release.render("5.0.0", "2026-12-01", found),
                         "## [5.0.0] - 2026-12-01\n\n### Added\n\n- Added d.\n\n### Changed\n\n- **Breaking:** Broke c.\n"
                         "- Changed a.\n\n### Fixed\n\n- Fixed b.\n")
        self.assertEqual(release.render("4.0.1", "2026-12-01", found[:1]), "## [4.0.1] - 2026-12-01\n\n### Fixed\n\n- Fixed b.\n")

    def test_fleets_own_fragments_parse(self) -> None:
        self.assertEqual(release.fragments(ROOT)[1], [])
        self.assertEqual(release.released(ROOT)[1], [])


class ReleasedNotesTest(unittest.TestCase):
    def test_a_releases_notes_are_one_section_named_for_its_file(self) -> None:
        root = temporary(self)
        write(root, f"changelog.d/released/{LATER}.md", f"## [{LATER}] - 2026-11-20\n\n### Fixed\n\n- A fix.\n")
        self.assertEqual(release.released(root), ([release.Notes(LATER, "2026-11-20", "### Fixed\n\n- A fix.\n",
                                                                 f"changelog.d/released/{LATER}.md")], []))

    def test_each_refusal(self) -> None:
        cases = {
            "notes.md": ("## [4.1.0] - 2026-11-20\n\n- x\n", "isn't a release's notes (changelog.d/released/X.Y.Z.md"),
            "4.1.md": ("## [4.1.0] - 2026-11-20\n\n- x\n", "isn't a release's notes"),
            "4.1.0.txt": ("## [4.1.0] - 2026-11-20\n\n- x\n", "isn't a release's notes"),
            f"{release.FROZEN}.md": (f"## [{release.FROZEN}] - 2026-11-20\n\n- x\n", "notes are in CHANGELOG.md, which is frozen"),
            "4.1.0.md": ("## [4.2.0] - 2026-11-20\n\n- x\n", "one section, `## [4.1.0] - YYYY-MM-DD`, with nothing before it"),
            "4.2.0.md": ("Intro.\n\n## [4.2.0] - 2026-11-20\n\n- x\n", "with nothing before it"),
            "4.3.0.md": ("## [4.3.0] - 2026-11-20\n\n- x\n\n## [4.2.9] - 2026-11-19\n\n- y\n", "one section"),
            "4.4.0.md": ("## [4.4.0]\n\n- x\n", "one section"),
            "4.5.0.md": ("## [4.5.0] - 2026-02-30\n\n- x\n", "the section for 4.5.0 has the impossible date 2026-02-30"),
            "4.6.0.md": ("## [4.6.0] - 2026-11-20\n\n### Added\n", "the section for 4.6.0 has no entries"),
            "4.7.0.md": ("## [4.7.0] - 2026-11-20\n\n- x\n\n```sh\n", "changelog.d/released/4.7.0.md:5: a code fence that is never closed"),
        }
        for name, (text, needle) in cases.items():
            with self.subTest(name=name):
                root = temporary(self)
                write(root, f"changelog.d/released/{name}", text)
                found, errors = release.released(root)
                self.assertEqual(found, [])
                self.assertEqual(len(errors), 1, errors)
                self.assertTrue(errors[0].startswith(f"changelog.d/released/{name}"), errors)
                self.assertIn(needle, errors[0])


class PullRequestCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temporary(self) / "fleet"
        write(self.root, "VERSION", f"{release.FROZEN}\n")
        write(self.root, "CHANGELOG.md", FROZEN_CHANGELOG)
        write(self.root, "README.md", "fleet\n")
        git(self.root, "init", "-q", "-b", "main")
        commit(self.root, "chore: main")
        git(self.root, "checkout", "-q", "-b", "change")

    def check(self, base: str = "main", root: Path | None = None) -> tuple[int, str, str]:
        return run("release", "check", f"--base={base}", "--root", str(root or self.root))

    def test_a_pull_request_that_adds_a_fragment_passes(self) -> None:
        write(self.root, "README.md", "fleet, changed\n")
        write(self.root, "changelog.d/readme.md", fragment_text("fixed", "The README says more."))
        commit(self.root, "fix: the README")
        self.assertEqual(self.check(), (0, "fleet release check: ok, it adds changelog.d/readme.md (fixed)\n", ""))

    def test_a_pull_request_without_a_fragment_fails(self) -> None:
        write(self.root, "README.md", "fleet, changed\n")
        commit(self.root, "fix: the README")
        code, out, err = self.check()
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "fleet release check: this pull request adds no changelog fragment: add changelog.d/<name>.md, "
                              f"{release.FRAGMENT_FORMAT} (docs/release.md)\n")

    def test_editing_someone_elses_fragment_isnt_adding_one(self) -> None:
        write(self.root, "changelog.d/earlier.md", fragment_text("added", "Earlier."))
        commit(self.root, "feat: earlier")
        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "--ff-only", "change")
        git(self.root, "checkout", "-q", "-b", "follow-up")
        write(self.root, "changelog.d/earlier.md", fragment_text("added", "Earlier, reworded."))
        commit(self.root, "docs: reword")
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("adds no changelog fragment", err)

    def test_a_fragment_that_doesnt_parse_fails(self) -> None:
        write(self.root, "changelog.d/readme.md", "---\nkind: feature\n---\nThe README.\n")
        commit(self.root, "fix: the README")
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(err, "fleet release check: changelog.d/readme.md: kind 'feature' isn't one of breaking, added, "
                              "changed, fixed\n")

    def test_a_misnamed_fragment_isnt_one(self) -> None:
        write(self.root, "changelog.d/README.MD", fragment_text("fixed", "The README."))
        commit(self.root, "fix: the README")
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("adds no changelog fragment", err)
        self.assertIn("changelog.d/README.MD: a fragment's name is lowercase letters, digits and hyphens", err)

    def test_an_entry_added_to_the_frozen_changelog_fails(self) -> None:
        write(self.root, "changelog.d/readme.md", fragment_text("fixed", "The README."))
        write(self.root, "CHANGELOG.md", FROZEN_CHANGELOG + "- One more, by habit.\n")
        commit(self.root, "fix: the README")
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(err, f"fleet release check: CHANGELOG.md: frozen at {release.FROZEN}, but this pull request adds an "
                              "entry to it: a change's entry is a fragment, changelog.d/<name>.md (docs/release.md)\n")

    def test_a_correction_to_the_frozen_changelog_passes(self) -> None:
        write(self.root, "changelog.d/typo.md", fragment_text("fixed", "A typo in the CHANGELOG."))
        write(self.root, "CHANGELOG.md", FROZEN_CHANGELOG.replace("The last entry here.", "The last entry, here."))
        commit(self.root, "docs: a typo")
        self.assertEqual(self.check()[0], 0)

    def test_deleting_the_changelog_fails(self) -> None:
        write(self.root, "changelog.d/gone.md", fragment_text("changed", "No CHANGELOG."))
        (self.root / "CHANGELOG.md").unlink()
        commit(self.root, "chore: no CHANGELOG")
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn(f"CHANGELOG.md: frozen at {release.FROZEN}, and this pull request deletes it", err)

    def release_on_main(self) -> None:
        write(self.root, "changelog.d/readme.md", fragment_text("fixed", "The README."))
        commit(self.root, "fix: the README")
        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "--ff-only", "change")
        git(self.root, "checkout", "-q", "-b", "release")
        (self.root / "changelog.d/readme.md").unlink()
        write(self.root, "VERSION", f"{LATER}\n")
        write(self.root, f"changelog.d/released/{LATER}.md", f"## [{LATER}] - 2026-11-20\n\n### Fixed\n\n- The README.\n")
        commit(self.root, f"chore: release v{LATER}")

    def test_a_release_pull_request_folds_the_fragments_and_adds_none(self) -> None:
        self.release_on_main()
        self.assertEqual(self.check(), (0, "fleet release check: ok, a release pull request, which folds the fragments\n", ""))

    def test_a_release_pull_request_that_adds_a_fragment_fails(self) -> None:
        self.release_on_main()
        write(self.root, "changelog.d/sneaked.md", fragment_text("added", "Sneaked in."))
        commit(self.root, "feat: sneaked in")
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("changelog.d/sneaked.md: a release pull request (it changes VERSION) folds the fragments and adds none", err)

    def test_a_release_pull_request_writes_only_its_own_notes(self) -> None:
        self.release_on_main()
        write(self.root, "changelog.d/released/4.0.9.md", "## [4.0.9] - 2026-11-19\n\n- Made up.\n")
        commit(self.root, "chore: more notes")
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("changelog.d/released/4.0.9.md: a released version's notes, which a pull request doesn't change", err)

    def test_a_release_pull_request_doesnt_rewrite_an_earlier_releases_notes(self) -> None:
        self.release_on_main()
        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "--ff-only", "release")
        write(self.root, "changelog.d/next.md", fragment_text("added", "Next."))
        commit(self.root, "feat: next")
        git(self.root, "checkout", "-q", "-b", "release-2")
        (self.root / "changelog.d/next.md").unlink()
        write(self.root, "VERSION", "4.2.0\n")
        write(self.root, "changelog.d/released/4.2.0.md", "## [4.2.0] - 2026-11-21\n\n### Added\n\n- Next.\n")
        commit(self.root, "chore: release v4.2.0")
        self.assertEqual(self.check()[0], 0)
        write(self.root, f"changelog.d/released/{LATER}.md", f"## [{LATER}] - 2026-11-20\n\n### Fixed\n\n- Rewritten.\n")
        commit(self.root, "chore: and rewrite the last notes")
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(err, f"fleet release check: changelog.d/released/{LATER}.md: a released version's notes, which a "
                              "pull request doesn't change (`fleet release prepare` writes the new release's, in its "
                              "release pull request)\n")

    def test_a_pull_request_that_changes_a_releases_notes_fails(self) -> None:
        self.release_on_main()
        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "--ff-only", "release")
        git(self.root, "checkout", "-q", "-b", "rewrite")
        write(self.root, f"changelog.d/released/{LATER}.md", f"## [{LATER}] - 2026-11-20\n\n### Fixed\n\n- Rewritten.\n")
        write(self.root, "changelog.d/rewrite.md", fragment_text("fixed", "Rewrote the notes."))
        commit(self.root, "docs: rewrite the notes")
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(err, f"fleet release check: changelog.d/released/{LATER}.md: a released version's notes, which a "
                              "pull request doesn't change (`fleet release prepare` writes the new release's, in its "
                              "release pull request)\n")

    def test_in_ci_the_merge_commits_first_parent_is_the_base(self) -> None:
        write(self.root, "README.md", "fleet, changed\n")
        commit(self.root, "fix: the README, without a fragment")
        git(self.root, "checkout", "-q", "main")
        write(self.root, "changelog.d/on-main.md", fragment_text("added", "Merged to main meanwhile."))
        commit(self.root, "feat: on main")
        git(self.root, "merge", "-q", "--no-ff", "-m", "Merge the pull request into main", "change")
        shallow = temporary(self) / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "2", f"file://{self.root}", str(shallow)], check=True,
                       capture_output=True)
        self.assertEqual(git(shallow, "rev-parse", "--is-shallow-repository").strip(), "true")
        code, _, err = self.check("HEAD^1", shallow)
        self.assertEqual(code, 1)
        self.assertIn("adds no changelog fragment", err)
        git(self.root, "reset", "-q", "--hard", "HEAD^1")
        git(self.root, "checkout", "-q", "change")
        write(self.root, "changelog.d/readme.md", fragment_text("fixed", "The README."))
        commit(self.root, "fix: the README's fragment")
        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "--no-ff", "-m", "Merge the pull request into main", "change")
        shallow = temporary(self) / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "2", f"file://{self.root}", str(shallow)], check=True,
                       capture_output=True)
        self.assertEqual(self.check("HEAD^1", shallow), (0, "fleet release check: ok, it adds changelog.d/readme.md (fixed)\n", ""))

    def test_errors_are_annotations_in_actions(self) -> None:
        commit(self.root, "chore: nothing")
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("::error::fleet release check: this pull request adds no changelog fragment"), err)

    def test_a_base_that_isnt_a_ref_fails(self) -> None:
        for base, needle in (("--output=x", "--base '--output=x' isn't a ref"), ("no-such-branch", "`git merge-base no-such-branch HEAD` failed")):
            with self.subTest(base=base):
                code, out, err = self.check(base)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(f"fleet release: {needle}", err)
        self.assertFalse((self.root / "x").exists())


class CiTest(unittest.TestCase):
    def setUp(self) -> None:
        workflow = mapping(self_check.parse(CI.read_text(encoding="utf-8")))
        job = mapping(mapping(workflow["jobs"])["check"])
        self.permissions = mapping(job["permissions"])
        steps = job["steps"]
        assert isinstance(steps, list)
        self.steps = [mapping(step) for step in steps]

    def test_fleet_ci_checks_each_pull_requests_fragment_except_dependabots(self) -> None:
        names = [step.get("name") for step in self.steps]
        self.assertLess(names.index("make check"), names.index("Changelog fragment"))
        step = self.steps[names.index("Changelog fragment")]
        self.assertEqual(step["if"], "github.event_name == 'pull_request' && github.event.pull_request.user.login != 'dependabot[bot]'")
        self.assertEqual(step["run"], "poetry run fleet release check --base 'HEAD^1'")
        self.assertNotIn("env", step)
        self.assertEqual(self.permissions, {"contents": "read"})

    def test_the_checkout_holds_the_merge_commits_base(self) -> None:
        checkout = self.steps[0]
        self.assertTrue(str(checkout["uses"]).startswith("actions/checkout@"))
        self.assertEqual(mapping(checkout["with"]), {"persist-credentials": "false", "fetch-depth": "2"})


class DocsTest(unittest.TestCase):
    def test_the_release_doc_says_how_to_write_a_fragment(self) -> None:
        text = (ROOT / "docs" / "release.md").read_text(encoding="utf-8")
        section = text.split("\n## Changelog fragments\n", 1)[1].split("\n## ", 1)[0]
        for kind in release.KINDS:
            self.assertIn(f"`{kind}`", section)
        for needle in ("changelog.d/<name>.md", "changelog.d/released/X.Y.Z.md", "fleet release check --base origin/main",
                       f"frozen at {release.FROZEN}"):
            self.assertIn(needle, section)
        self.assertIsNotNone(re.search(r"^---\nkind: \w+\n---\n", section, re.MULTILINE))

    def test_the_frozen_changelog_says_where_later_notes_are(self) -> None:
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"This file is frozen at {release.FROZEN}.", text.split("\n## [", 1)[0])
        self.assertIn("changelog.d/released/X.Y.Z.md", text.split("\n## [", 1)[0])


if __name__ == "__main__":
    unittest.main()
