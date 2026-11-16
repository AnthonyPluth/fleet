import contextlib
import io
import unittest
from pathlib import Path

from consumers import commit, git, temporary, write

from fleet_tools import cli, next_version


class Bump(unittest.TestCase):
    def test_a_fix_or_anything_else_is_a_patch(self) -> None:
        for log in ("fix: a thing\n", "docs: words\n", "Merge pull request #3\n\nchore: tidy\n"):
            with self.subTest(log=log):
                self.assertEqual(next_version.bump("v1.2.3", log), "v1.2.4")

    def test_a_feature_is_a_minor(self) -> None:
        for log in ("feat: a thing\n", "feat(trips): a thing\n", "fix: x\n\nsomething #minor\n"):
            with self.subTest(log=log):
                self.assertEqual(next_version.bump("v1.2.3", log), "v1.3.0")

    def test_a_bang_or_breaking_change_is_a_major(self) -> None:
        for log in ("feat!: a thing\n", "fix(api)!: x\n", "fix: x\n\nBREAKING CHANGE: the input is gone\n", "x #major\n"):
            with self.subTest(log=log):
                self.assertEqual(next_version.bump("v1.2.3", log), "v2.0.0")

    def test_a_marker_counts_only_at_the_start_or_end_of_a_line(self) -> None:
        for log in ("fix: explain feat: and #minor markers\n", "docs: what BREAKING CHANGE: means\n",
                    "fix: say #major in the middle\n", "chore: a feat!: example\n"):
            with self.subTest(log=log):
                self.assertEqual(next_version.bump("v1.2.3", log), "v1.2.4")

    def test_the_highest_tag_is_by_version_not_by_name(self) -> None:
        self.assertEqual(next_version.highest(["v1.9.0", "v1.10.0", "v1.2.0", "latest", "v2.0"]), "v1.10.0")
        self.assertIsNone(next_version.highest(["latest"]))

    def test_a_taken_version_is_skipped(self) -> None:
        self.assertEqual(next_version.unused("v1.3.0", {"v1.3.0", "v1.3.1"}), "v1.3.2")


class Repository(unittest.TestCase):
    def setUp(self) -> None:
        base = temporary(self)
        self.origin = base / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        commit(self.origin, "chore: start")
        self.clone = base / "clone"
        git(base, "clone", "-q", str(self.origin), str(self.clone))

    def land(self, message: str) -> str:
        commit(self.origin, message)
        git(self.clone, "pull", "-q", "--ff-only")
        return git(self.clone, "rev-parse", "HEAD").strip()

    def decide(self, sha: str | None = None) -> dict[str, str]:
        head = git(self.clone, "rev-parse", "HEAD").strip()
        return next_version.decide(self.clone, sha or head)[0]

    def tag(self, name: str, where: Path | None = None) -> None:
        git(where or self.origin, "tag", name)
        git(self.clone, "fetch", "-q", "--tags")

    def test_the_first_release_is_v1_0_0(self) -> None:
        self.assertEqual(self.decide(), {"newest": "true", "version": "v1.0.0", "new": "true"})

    def test_the_step_comes_from_the_commits_since_the_last_tag(self) -> None:
        self.tag("v1.0.0")
        self.land("fix: one")
        self.assertEqual(self.decide()["version"], "v1.0.1")
        self.land("feat: two")
        self.assertEqual(self.decide()["version"], "v1.1.0")
        self.land("feat!: three")
        self.assertEqual(self.decide()["version"], "v2.0.0")

    def test_an_already_tagged_commit_reuses_its_version(self) -> None:
        self.tag("v1.4.0")
        self.assertEqual(self.decide(), {"newest": "true", "version": "v1.4.0", "new": "false"})

    def test_a_commit_that_isn_t_main_s_newest_is_skipped(self) -> None:
        self.tag("v1.0.0")
        old = self.land("fix: one")
        self.land("fix: two")
        self.assertEqual(self.decide(old), {"newest": "false", "skip": "true"})

    def test_versions_only_go_up_after_a_rewrite(self) -> None:
        git(self.origin, "tag", "v2.3.21", "HEAD")
        git(self.origin, "checkout", "-q", "--orphan", "rewritten")
        commit(self.origin, "chore: rewritten history")
        git(self.origin, "tag", "v1.0.2")
        git(self.origin, "branch", "-q", "-f", "main", "rewritten")
        git(self.origin, "checkout", "-q", "main")
        commit(self.origin, "fix: after the rewrite")
        git(self.clone, "fetch", "-q", "--tags", "origin")
        git(self.clone, "reset", "-q", "--hard", "origin/main")
        self.assertEqual(self.decide()["version"], "v2.3.22")

    def test_the_command_appends_its_outputs_like_the_script(self) -> None:
        self.tag("v1.0.0")
        sha = self.land("feat: a thing")
        output = temporary(self) / "github_output"
        output.write_text("earlier=1\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["next-version", "--root", str(self.clone), "--sha", sha, "--output", str(output)])
        self.assertEqual(code, 0)
        self.assertEqual(output.read_text(), "earlier=1\nnewest=true\nversion=v1.1.0\nnew=true\n")
        self.assertEqual(out.getvalue(), "Next version: v1.1.0 (last in this history: v1.0.0; highest: v1.0.0)\n")

    def merge(self, title: str, inner: str, files: dict[str, str] | None = None) -> str:
        git(self.origin, "checkout", "-q", "-b", "change")
        for path, text in (files or {}).items():
            write(self.origin, path, text)
        commit(self.origin, inner)
        git(self.origin, "checkout", "-q", "main")
        git(self.origin, "merge", "-q", "--no-ff", "-m", title, "change")
        git(self.origin, "branch", "-q", "-D", "change")
        git(self.clone, "pull", "-q", "--ff-only")
        return git(self.clone, "rev-parse", "HEAD").strip()

    def fleet(self, sha: str | None = None) -> tuple[dict[str, str], list[str]]:
        head = git(self.clone, "rev-parse", "HEAD").strip()
        return next_version.decide(self.clone, sha or head, first_parent=True, fragments=True)

    def test_the_first_parent_history_reads_each_merges_title_not_its_branchs_commits(self) -> None:
        self.tag("v5.0.0")
        self.merge("fix: a message (#1)", "feat!: an inner commit that isn't the title")
        self.assertEqual(self.decide()["version"], "v6.0.0")
        self.assertEqual(self.fleet()[0]["version"], "v5.0.1")
        self.merge("feat: a new input (#2)", "fix: inner")
        self.assertEqual(self.fleet()[0]["version"], "v5.1.0")

    def test_a_breaking_fragment_makes_a_major_whatever_the_title_says(self) -> None:
        self.tag("v5.0.0")
        self.merge("fix: an input is gone (#3)", "wip", {"changelog.d/input-gone.md": "---\nkind: breaking\n---\nGone (#3).\n"})
        outputs, notes = self.fleet()
        self.assertEqual(outputs, {"newest": "true", "version": "v6.0.0", "new": "true"})
        self.assertEqual(notes, ["Next version: v6.0.0 (last in this history: v5.0.0; highest: v5.0.0; a major for "
                                 "changelog.d/input-gone.md (kind: breaking))"])
        self.assertEqual(self.decide()["version"], "v5.0.1")

    def test_only_a_fragment_added_since_the_last_tag_counts(self) -> None:
        self.merge("feat!: an input is gone (#4)", "wip", {"changelog.d/input-gone.md": "---\nkind: breaking\n---\nGone (#4).\n"})
        self.tag("v6.0.0")
        self.merge("fix: a message (#5)", "wip", {"changelog.d/message.md": "---\nkind: fixed\n---\nA message (#5).\n",
                                                  "changelog.d/released/5.0.0.md": "## [5.0.0] - 2026-11-15\n\n- X.\n"})
        self.assertEqual(self.fleet()[0]["version"], "v6.0.1")
        self.merge("feat: a new input (#6)", "wip", {"changelog.d/new-input.md": "---\nkind: added\n---\nNew (#6).\n"})
        self.assertEqual(self.fleet()[0]["version"], "v6.1.0")

    def test_a_breaking_title_and_a_breaking_fragment_make_one_major(self) -> None:
        self.tag("v5.0.0")
        self.merge("feat!: an input is gone (#7)", "wip", {"changelog.d/input-gone.md": "---\nkind: breaking\n---\nGone (#7).\n"})
        self.assertEqual(self.fleet()[0]["version"], "v6.0.0")

    def test_the_planner_skips_what_isnt_newest_reuses_a_tag_and_counts_from_the_highest(self) -> None:
        self.tag("v5.0.0")
        old = self.merge("fix: one (#8)", "wip")
        self.merge("fix: two (#9)", "wip")
        self.assertEqual(self.fleet(old)[0], {"newest": "false", "skip": "true"})
        self.tag("v5.0.1")
        self.assertEqual(self.fleet()[0], {"newest": "true", "version": "v5.0.1", "new": "false"})
        self.merge("fix: three (#10)", "wip")
        git(self.origin, "tag", "v5.0.2", "HEAD~1")
        git(self.clone, "fetch", "-q", "--tags")
        self.assertEqual(self.fleet()[0]["version"], "v5.0.3")

    def test_a_fragment_that_doesnt_parse_fails_the_planner(self) -> None:
        self.tag("v5.0.0")
        sha = self.merge("fix: x (#11)", "wip", {"changelog.d/bad.md": "---\nkind: feature\n---\nX.\n"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["next-version", "--root", str(self.clone), "--sha", sha, "--output", str(temporary(self) / "out"),
                             "--first-parent", "--fragments"])
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "fleet next-version: changelog.d/bad.md: kind 'feature' isn't one of breaking, added, "
                                         "changed, fixed\n")

    def test_the_apps_planner_reads_no_fragment(self) -> None:
        self.tag("v1.0.0")
        sha = self.merge("fix: x", "wip", {"changelog.d/bad.md": "not a fragment\n"})
        output = temporary(self) / "out"
        with contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["next-version", "--root", str(self.clone), "--sha", sha, "--output", str(output)])
        self.assertEqual(code, 0)
        self.assertEqual(output.read_text(), "newest=true\nversion=v1.0.1\nnew=true\n")

    def test_a_git_failure_is_reported(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["next-version", "--root", str(self.clone), "--sha", "x", "--remote", "nowhere"])
        self.assertEqual(code, 1)
        self.assertIn("fleet next-version: `git fetch --quiet nowhere main` failed", err.getvalue())


if __name__ == "__main__":
    unittest.main()
