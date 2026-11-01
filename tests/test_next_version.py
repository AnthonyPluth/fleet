import contextlib
import io
import unittest
from pathlib import Path

from consumers import commit, git, temporary

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

    def test_a_git_failure_is_reported(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["next-version", "--root", str(self.clone), "--sha", "x", "--remote", "nowhere"])
        self.assertEqual(code, 1)
        self.assertIn("fleet next-version: `git fetch --quiet nowhere main` failed", err.getvalue())


if __name__ == "__main__":
    unittest.main()
