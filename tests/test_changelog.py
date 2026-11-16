import contextlib
import io
import os
import re
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from consumers import commit, git, temporary, write

from fleet_tools import cli, release, self_check

ROOT = cli.ROOT
CI = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_CHECK = ROOT / ".github" / "workflows" / "release-check.yml"
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

    def check(self, base: str = "main", root: Path | None = None, actions: bool = False,
              title: str | None = None, body: str | None = None) -> tuple[int, str, str]:
        environment = {key: value for key, value in os.environ.items() if key != "GITHUB_ACTIONS"}
        if actions:
            environment["GITHUB_ACTIONS"] = "true"
        described = ([f"--title={title}"] if title is not None else []) + ([f"--body={body}"] if body is not None else [])
        with mock.patch.dict(os.environ, environment, clear=True):
            return run("release", "check", f"--base={base}", "--root", str(root or self.root), *described)

    def titled(self, kind: str, title: str, body: str = "") -> tuple[int, str, str]:
        write(self.root, "changelog.d/change.md", fragment_text(kind, "A change."))
        commit(self.root, title)
        return self.check(title=title, body=body)

    def test_the_title_and_the_fragment_agree_on_a_breaking_change(self) -> None:
        for title, body in (("feat!: an input is gone", ""), ("fix(sync)!: the body", ""),
                            ("feat: an input is gone", "Why.\n\nBREAKING CHANGE: the input is gone\n")):
            with self.subTest(title=title, body=body):
                git(self.root, "checkout", "-q", "-B", "change", "main")
                self.assertEqual(self.titled("breaking", title, body),
                                 (0, "fleet release check: ok, it adds changelog.d/change.md (breaking); its title makes "
                                     "it a major change\n", ""))

    def test_a_breaking_fragment_under_a_title_that_doesnt_say_so_fails(self) -> None:
        for title, body in (("feat: an input is gone", ""), ("fix: an input is gone", "It explains what BREAKING CHANGE: means.\n"),
                            ("chore: an input is gone", "")):
            with self.subTest(title=title):
                git(self.root, "checkout", "-q", "-B", "change", "main")
                code, out, err = self.titled("breaking", title, body)
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(err, "fleet release check: changelog.d/change.md: `kind: breaking`, but the title doesn't "
                                      "mark the pull request breaking: `feat!: …` (a `!` before the colon), or a `BREAKING "
                                      "CHANGE:` line in the description; a breaking change is a major release of its own "
                                      "(docs/release.md)\n")

    def test_a_title_that_says_breaking_without_a_breaking_fragment_fails(self) -> None:
        for title, body in (("feat!: an input is gone", ""), ("feat: x", "BREAKING CHANGE: the input is gone\n"),
                            ("feat: x", "Released as a major.\n#major\n")):
            with self.subTest(title=title, body=body):
                git(self.root, "checkout", "-q", "-B", "change", "main")
                code, out, err = self.titled("added", title, body)
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(err, "fleet release check: the title or the description marks this pull request breaking "
                                      "(a `!` before the title's colon, a `BREAKING CHANGE:` line or a `#major` marker), but "
                                      "it adds no `kind: breaking` fragment: a major release's notes say what a consumer "
                                      "has to change (docs/release.md)\n")

    def test_an_added_fragment_needs_a_feat_title(self) -> None:
        for title, body in (("fix: a new input", ""), ("chore: a new input", ""), ("docs(sync): a new input", ""),
                            ("fix: a new input", "feat: a new input\n"), ("featured: a new input", "")):
            with self.subTest(title=title, body=body):
                git(self.root, "checkout", "-q", "-B", "change", "main")
                code, out, err = self.titled("added", title, body)
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(err, "fleet release check: changelog.d/change.md: `kind: added`, but the title doesn't make "
                                      "the pull request a minor: `feat: …`, in the title itself (the merge commit's subject), "
                                      "not the description, or `feat!: …` with a `kind: breaking` fragment; an addition is "
                                      "a minor release at least (docs/release.md)\n")

    def test_a_marker_doesnt_make_an_added_fragments_title_a_feat(self) -> None:
        code, out, err = self.titled("added", "fix: a new input #minor")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("ends with a `#major` or `#minor` marker, which never reaches main", err)
        self.assertIn("changelog.d/change.md: `kind: added`, but the title doesn't make the pull request a minor", err)

    def test_the_title_gives_a_minor_or_a_patch(self) -> None:
        for kind, title, step in (("added", "feat: a new input", "minor"), ("added", "feat(sync): a new input", "minor"),
                                  ("fixed", "fix: a message", "patch"), ("changed", "docs: a sentence", "patch"),
                                  ("fixed", "fix: explain what feat: and #minor mean", "patch")):
            with self.subTest(title=title):
                git(self.root, "checkout", "-q", "-B", "change", "main")
                self.assertEqual(self.titled(kind, title),
                                 (0, f"fleet release check: ok, it adds changelog.d/change.md ({kind}); its title makes it "
                                     f"a {step} change\n", ""))

    def test_a_marker_at_the_end_of_the_title_never_reaches_main(self) -> None:
        for title in ("fix: a thing #minor", "feat: a thing #major", "#minor"):
            with self.subTest(title=title):
                git(self.root, "checkout", "-q", "-B", "change", "main")
                code, out, err = self.titled("added", title)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("ends with a `#major` or `#minor` marker, which never reaches main: GitHub adds ` (#<number>)` "
                              "after the title in the merge commit's subject", err)

    def test_without_a_title_the_check_is_the_fragments_alone(self) -> None:
        write(self.root, "changelog.d/change.md", fragment_text("breaking", "A change."))
        commit(self.root, "fix: an input is gone")
        self.assertEqual(self.check(), (0, "fleet release check: ok, it adds changelog.d/change.md (breaking)\n", ""))

    def test_a_release_pull_request_is_no_change_of_its_own(self) -> None:
        self.release_on_main()
        self.assertEqual(self.check(title=f"chore: release v{LATER}", body="Notes.\n"),
                         (0, "fleet release check: ok, a release pull request, which folds the fragments\n", ""))

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

    def test_a_release_pull_request_fails_when_a_fragment_lands_on_the_base_after_prepare(self) -> None:
        self.release_on_main()
        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "--no-ff", "-m", "Merge the release pull request into main", "release")
        self.assertEqual(self.check("HEAD^1")[0], 0)
        git(self.root, "reset", "-q", "--hard", "HEAD^1")
        write(self.root, "changelog.d/late.md", fragment_text("breaking", "Merged while the release was open."))
        commit(self.root, "feat!: merged while the release was open")
        git(self.root, "merge", "-q", "--no-ff", "-m", "Merge the release pull request into main", "release")
        code, _, err = self.check("HEAD^1")
        self.assertEqual(code, 1)
        self.assertEqual(err, "fleet release check: changelog.d/late.md: still pending in the tree this release pull "
                              "request merges into (merged to the base after `fleet release prepare` ran), so the release "
                              "would ship without it in its notes; cut the release again from the base: `fleet release "
                              "prepare X.Y.Z` on a fresh branch\n")

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
        git(shallow.parent, "clone", "-q", "--depth", "2", f"file://{self.root}", str(shallow))
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
        git(shallow.parent, "clone", "-q", "--depth", "2", f"file://{self.root}", str(shallow))
        self.assertEqual(self.check("HEAD^1", shallow), (0, "fleet release check: ok, it adds changelog.d/readme.md (fixed)\n", ""))

    def test_errors_are_annotations_in_actions_and_plain_text_elsewhere(self) -> None:
        write(self.root, "changelog.d/Bad.md", fragment_text("added", "Misnamed."))
        commit(self.root, "chore: a misnamed fragment")
        code, _, plain = self.check()
        self.assertEqual(code, 1)
        lines = plain.splitlines()
        self.assertEqual(len(lines), 2, plain)
        self.assertTrue(all(line.startswith("fleet release check: ") for line in lines), plain)
        code, out, annotated = self.check(actions=True)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(annotated.splitlines(), [f"::error::{line}" for line in lines])
        self.assertEqual(self.check(actions=True)[1:], ("", annotated))

    def test_a_base_that_isnt_a_ref_fails(self) -> None:
        for base, needle in (("--output=x", "--base '--output=x' isn't a ref"), ("no-such-branch", "`git merge-base no-such-branch HEAD` failed")):
            with self.subTest(base=base):
                code, out, err = self.check(base)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(f"fleet release: {needle}", err)
        self.assertFalse((self.root / "x").exists())


class NotesBetweenTreesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temporary(self) / "fleet"
        write(self.root, "README.md", "fleet\n")
        write(self.root, "changelog.d/released/5.0.0.md", "## [5.0.0] - 2026-11-15\n\n### Fixed\n\n- Folded.\n")
        write(self.root, "changelog.d/already.md", fragment_text("added", "In the last release."))
        git(self.root, "init", "-q", "-b", "main")
        commit(self.root, "chore: release v5.0.0")
        git(self.root, "tag", "v5.0.0")

    def land(self, title: str, files: dict[str, str]) -> str:
        git(self.root, "checkout", "-q", "-b", "change")
        for path, text in files.items():
            write(self.root, path, text)
        commit(self.root, f"wip: {title}")
        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "--no-ff", "-m", title, "change")
        git(self.root, "branch", "-q", "-D", "change")
        return git(self.root, "rev-parse", "HEAD").strip()

    def notes(self, *argv: str) -> tuple[int, str, str]:
        return run("release", "notes", "6.0.0", "--root", str(self.root), *argv)

    def test_the_notes_are_the_fragments_two_trees_differ_by(self) -> None:
        self.land("feat!: an input is gone (#140)", {"changelog.d/input-gone.md": fragment_text("breaking", "An input is gone (#140).")})
        self.land("fix: a message (#141)", {"changelog.d/message.md": fragment_text("fixed", "A message (#141)."),
                                            "changelog.d/released/5.0.0.md": "## [5.0.0] - 2026-11-15\n\n- Rewritten.\n"})
        self.land("feat: a new input (#142)", {"changelog.d/new-input.md": fragment_text("added", "A new input (#142).")})
        self.assertEqual(self.notes("--since", "v5.0.0"), (0, "### Added\n\n- A new input (#142).\n\n### Changed\n\n"
                                                              "- **Breaking:** An input is gone (#140).\n\n### Fixed\n\n"
                                                              "- A message (#141).\n", ""))
        found = release.added_fragments(self.root, "v5.0.0", "HEAD")
        self.assertEqual([fragment.path.as_posix() for fragment in found],
                         ["changelog.d/input-gone.md", "changelog.d/message.md", "changelog.d/new-input.md"])

    def test_a_merge_without_a_fragment_is_listed_by_its_subject(self) -> None:
        self.land("deps: bump the python group with 3 updates (#143)", {"poetry.lock": "new\n"})
        self.land("fix: a message (#144)", {"changelog.d/message.md": fragment_text("fixed", "A message (#144).")})
        self.assertEqual(self.notes("--since", "v5.0.0"), (0, "### Changed\n\n- deps: bump the python group with 3 updates "
                                                              "(#143)\n\n### Fixed\n\n- A message (#144).\n", ""))

    def test_the_notes_of_a_commit_before_the_newest(self) -> None:
        first = self.land("fix: a message (#145)", {"changelog.d/message.md": fragment_text("fixed", "A message (#145).")})
        self.land("fix: another (#146)", {"changelog.d/another.md": fragment_text("fixed", "Another (#146).")})
        self.assertEqual(self.notes("--since", "v5.0.0", "--at", first), (0, "### Fixed\n\n- A message (#145).\n", ""))

    def test_nothing_to_release_or_a_fragment_that_doesnt_parse_fails(self) -> None:
        code, out, err = self.notes("--since", "v5.0.0")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "fleet release: nothing to release between v5.0.0 and HEAD: no commit, and no fragment added\n")
        self.land("fix: x (#147)", {"changelog.d/bad.md": "---\nkind: feature\n---\nX.\n"})
        code, out, err = self.notes("--since", "v5.0.0")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "fleet release: changelog.d/bad.md: kind 'feature' isn't one of breaking, added, changed, fixed\n")

    def test_a_ref_that_isnt_one_fails(self) -> None:
        for argv, needle in ((("--since=--output=x",), "--since '--output=x' isn't a ref"),
                             (("--since", "v5.0.0", "--at", "HEAD:README.md"), "--at 'HEAD:README.md' isn't a ref"),
                             (("--since", "v9.9.9"), "`git diff --diff-filter=A")):
            with self.subTest(argv=argv):
                code, out, err = self.notes(*argv)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(f"fleet release: {needle}", err)

    def test_the_released_folder_is_left_out_and_only_additions_count(self) -> None:
        self.land("docs: rewrite the folded notes (#148)", {"changelog.d/released/5.1.0.md": "## [5.1.0] - 2026-11-16\n\n- X.\n",
                                                             "changelog.d/already.md": fragment_text("added", "Reworded.")})
        self.assertEqual(release.added_fragments(self.root, "v5.0.0", "HEAD"), [])
        self.assertEqual(self.notes("--since", "v5.0.0"), (0, "### Changed\n\n- docs: rewrite the folded notes (#148)\n", ""))


class ReleaseCheckWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = mapping(self_check.parse(RELEASE_CHECK.read_text(encoding="utf-8")))
        self.job = mapping(mapping(self.workflow["jobs"])["check"])
        steps = self.job["steps"]
        assert isinstance(steps, list)
        self.steps = [mapping(step) for step in steps]

    def test_it_runs_on_each_pull_request_and_again_when_one_is_edited(self) -> None:
        self.assertEqual(mapping(mapping(self.workflow["on"])["pull_request"]), {"types": "[opened, synchronize, reopened, edited]"})
        self.assertEqual(self.workflow["permissions"], "{}")
        self.assertEqual(mapping(self.workflow["concurrency"]),
                         {"group": "release-check-${{ github.event.pull_request.number }}", "cancel-in-progress": "true"})

    def test_it_checks_each_pull_requests_fragment_and_title_except_dependabots(self) -> None:
        self.assertEqual(self.job["if"], "github.event.pull_request.user.login != 'dependabot[bot]'")
        self.assertEqual(mapping(self.job["permissions"]), {"contents": "read"})
        step = self.steps[-1]
        self.assertEqual(step["run"], "/usr/bin/python3 -E -s -m fleet_tools.cli release check --base 'HEAD^1' "
                                      "--title=\"$TITLE\" --body=\"$BODY\"")
        self.assertEqual(mapping(step["env"]), {"TITLE": "${{ github.event.pull_request.title }}",
                                                "BODY": "${{ github.event.pull_request.body }}"})

    def test_the_checkout_holds_the_merge_commits_base(self) -> None:
        checkout = self.steps[0]
        self.assertTrue(str(checkout["uses"]).startswith("actions/checkout@"))
        self.assertEqual(mapping(checkout["with"]), {"persist-credentials": "false", "fetch-depth": "2"})

    def test_fleet_ci_leaves_the_fragment_to_it(self) -> None:
        text = CI.read_text(encoding="utf-8")
        self.assertNotIn("release check", text)
        self.assertIn("release-check.yml", text)


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
