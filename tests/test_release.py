import contextlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from fleet_tools import agents_md, cli, release, self_check

ROOT = cli.ROOT
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
IGNORE = shutil.ignore_patterns(".git", ".tools", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache")


def copy(test: unittest.TestCase) -> Path:
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    root = Path(temporary.name) / "fleet"
    shutil.copytree(ROOT, root, ignore=IGNORE, symlinks=True)
    return root


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, (path, old)
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


class FleetItselfTest(unittest.TestCase):
    def test_the_current_stamps_agree(self) -> None:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(release.check(ROOT, version), [])
        self.assertEqual(self_check.check(ROOT), [])


class DisagreementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = copy(self)

    def assert_one_error(self, *needles: str) -> None:
        errors = self_check.check(self.root)
        self.assertEqual(len(errors), 1, errors)
        for needle in needles:
            self.assertIn(needle, errors[0])

    def test_a_plugin_version_that_disagrees_fails(self) -> None:
        path = self.root / release.PLUGIN
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["version"] = "0.1.1"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assert_one_error("plugin.json", "'0.1.1'")

    def test_a_pyproject_version_that_disagrees_fails(self) -> None:
        edit(self.root / "pyproject.toml", 'version = "0.1.0"', 'version = "0.2.0"')
        self.assert_one_error("pyproject.toml", "'0.2.0'")

    def test_a_snippet_version_that_disagrees_fails(self) -> None:
        edit(self.root / release.ADOPTING, "security.yml@<commit>   # vX.Y.Z", "security.yml@<commit>   # v0.0.9")
        self.assert_one_error("docs/adopting.md:", "v0.0.9")

    def test_a_settings_ref_that_disagrees_fails(self) -> None:
        edit(self.root / release.ADOPTING, '"ref": "vX.Y.Z"', '"ref": "v2.0.0"')
        self.assert_one_error("v2.0.0")

    def test_a_snippet_in_another_doc_that_disagrees_fails(self) -> None:
        edit(self.root / "docs" / "review-mining.md", "@<commit>   # vX.Y.Z", "@<commit>   # v0.0.9")
        self.assert_one_error("docs/review-mining.md:", "v0.0.9")

    def test_an_agents_marker_that_disagrees_fails(self) -> None:
        edit(self.root / "AGENTS.md", "<!-- fleet:begin mistakes v0.1.0 -->", "<!-- fleet:begin mistakes v0.0.9 -->")
        self.assert_one_error("AGENTS.md", "mistakes", "v0.0.9")

    def test_an_impossible_release_date_fails(self) -> None:
        self.assertEqual(release.changelog_errors("## [Unreleased]\n\n## [1.0.0] - 2026-02-31\n\n- x\n", "1.0.0"),
                         ["CHANGELOG.md: the section for 1.0.0 has the impossible date 2026-02-31"])

    def test_versions_are_canonical_ascii(self) -> None:
        for text in ("01.0.0", "1.00.0", "1.0.01", "\u0661.\u0660.\u0660", "1.0", "1.0.0-rc.1"):
            with self.subTest(text=text):
                with self.assertRaises(release.ReleaseError):
                    release.parse_version(text)
                (self.root / "VERSION").write_text(text + "\n", encoding="utf-8")
                errors = self_check.check(self.root)
                self.assertEqual(len(errors), 1)
                self.assertIn("isn't a version", errors[0])
        for text in ("0.0.0", "1.0.0", "10.20.30"):
            self.assertEqual(release.parse_version(text), tuple(int(part) for part in text.split(".")))

    def test_a_version_outside_a_snippet_is_prose(self) -> None:
        edit(self.root / release.ADOPTING, "# Adopting fleet\n", "# Adopting fleet\n\nSince v0.0.9 (`fleet sync --to v0.0.9`).\n")
        self.assertEqual(self_check.check(self.root), [])

    def test_a_released_section_that_disagrees_fails(self) -> None:
        edit(self.root / "CHANGELOG.md", "## [Unreleased]\n", "## [Unreleased]\n\n## [0.0.9] - 2026-01-01\n\n- Old.\n")
        self.assert_one_error("CHANGELOG.md", "0.0.9")

    def test_an_unreleased_section_that_isnt_first_fails(self) -> None:
        edit(self.root / "CHANGELOG.md", "## [Unreleased]\n", "## Notes\n\n## [Unreleased]\n")
        self.assert_one_error("CHANGELOG.md", "first section")

    def test_a_malformed_released_heading_fails(self) -> None:
        edit(self.root / "CHANGELOG.md", "## [Unreleased]\n", "## [Unreleased]\n\n## [0.1.0]\n\n- Old.\n")
        self.assert_one_error("CHANGELOG.md", "isn't `## [X.Y.Z] - YYYY-MM-DD`")

    def test_a_missing_stamped_file_fails(self) -> None:
        (self.root / release.PLUGIN).unlink()
        self.assert_one_error("plugin.json: missing")

    def test_a_placeholder_after_1_0_fails(self) -> None:
        self.assertEqual(release.adopting_errors("```yaml\nuses: AnthonyPluth/fleet/x.yml@abc   # vX.Y.Z\n```\n", "0.1.0"), [])
        errors = release.adopting_errors("```yaml\nuses: AnthonyPluth/fleet/x.yml@abc   # vX.Y.Z\n```\n", "1.0.0")
        self.assertEqual(errors, ["docs/adopting.md:2: a snippet says vX.Y.Z, but VERSION is 1.0.0"])

    def test_1_x_needs_a_released_section(self) -> None:
        self.assertEqual(release.changelog_errors("# C\n\n## [Unreleased]\n\n- x\n", "1.0.0"),
                         ["CHANGELOG.md: no `## [1.0.0] - YYYY-MM-DD` section for VERSION 1.0.0"])
        self.assertEqual(release.changelog_errors("# C\n\n## [Unreleased]\n\n## [1.0.0] - 2026-11-02\n\n- x\n", "1.0.0"), [])


class PrepareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = copy(self)

    def prepare(self, version: str = "1.0.0") -> tuple[int, str, str]:
        return run("release", "prepare", version, "--date", "2026-11-02", "--root", str(self.root))

    def test_prepare_stamps_everything_and_self_check_passes(self) -> None:
        before = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        unreleased = before.split("## [Unreleased]\n", 1)[1]
        code, out, err = self.prepare()
        self.assertEqual(code, 0, err)
        self.assertIn("stamped VERSION\n", out)
        self.assertEqual(self_check.check(self.root), [])
        self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), "1.0.0\n")
        self.assertEqual(json.loads((self.root / release.PLUGIN).read_text(encoding="utf-8"))["version"], "1.0.0")
        self.assertEqual(tomllib.loads((self.root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"], "1.0.0")
        changelog = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(changelog, before.replace("## [Unreleased]\n", "## [Unreleased]\n\n## [1.0.0] - 2026-11-02\n", 1))
        self.assertTrue(unreleased.strip())
        self.assertEqual(release.notes(changelog, "1.0.0"), unreleased.strip("\n") + "\n")
        for workflow in release.reusable_workflows(self.root):
            text = workflow.read_text(encoding="utf-8")
            self.assertEqual(re.findall(r"^  FLEET_REF: (\S+)", text, re.MULTILINE), ["v1.0.0"], workflow.name)
        adopting = (self.root / release.ADOPTING).read_text(encoding="utf-8")
        self.assertEqual({tag for _, tag in release.snippet_versions(adopting)}, {"v1.0.0"})
        self.assertIn("   tools/fleet sync --to v1.0.0\n", adopting)
        self.assertIn("   echo v1.0.0 > .fleet/version\n", adopting)
        self.assertIn('"ref": "v1.0.0"', adopting)
        self.assertIn("review-mining.yml@<commit>   # v1.0.0", (self.root / "docs" / "review-mining.md").read_text(encoding="utf-8"))
        agents = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual({block.version for block in agents_md.blocks(agents)}, {"v1.0.0"})
        self.assertEqual((self.root / ".github/workflows/ci.yml").read_text(encoding="utf-8"),
                         (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))

    def test_the_release_after_that_moves_every_stamp_again(self) -> None:
        self.assertEqual(self.prepare()[0], 0)
        edit(self.root / "CHANGELOG.md", "## [Unreleased]\n", "## [Unreleased]\n\n### Fixed\n\n- A fix.\n")
        code, _, err = self.prepare("1.0.1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self_check.check(self.root), [])
        changelog = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(release.notes(changelog, "1.0.1"), "### Fixed\n\n- A fix.\n")
        self.assertIn("## [1.0.0] - 2026-11-02", changelog)
        adopting = (self.root / release.ADOPTING).read_text(encoding="utf-8")
        self.assertEqual({tag for _, tag in release.snippet_versions(adopting)}, {"v1.0.1"})

    def test_prepare_refuses_and_changes_nothing(self) -> None:
        snapshot = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        for version, needle in (("0.2.0", "start at 1.0.0"), ("1.0", "isn't a version"), ("0.1.0", "start at 1.0.0")):
            with self.subTest(version=version):
                code, out, err = self.prepare(version)
                self.assertEqual(code, 1)
                self.assertIn(needle, err)
                self.assertEqual(out, "")
        self.assertEqual(snapshot, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_prepare_refuses_a_version_that_isnt_newer(self) -> None:
        self.assertEqual(self.prepare()[0], 0)
        edit(self.root / "CHANGELOG.md", "## [Unreleased]\n", "## [Unreleased]\n\n- More.\n")
        code, _, err = self.prepare("1.0.0")
        self.assertEqual(code, 1)
        self.assertIn("isn't newer than VERSION 1.0.0", err)

    def test_prepare_refuses_an_empty_unreleased_section(self) -> None:
        self.assertEqual(self.prepare()[0], 0)
        code, _, err = self.prepare("1.1.0")
        self.assertEqual(code, 1)
        self.assertIn("no entries to release", err)
        self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), "1.0.0\n")

    def test_prepare_refuses_stamps_that_already_disagree(self) -> None:
        edit(self.root / "pyproject.toml", 'version = "0.1.0"', 'version = "0.2.0"')
        code, _, err = self.prepare()
        self.assertEqual(code, 1)
        self.assertIn("disagree before preparing", err)
        self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), "0.1.0\n")

    def test_prepare_refuses_a_bad_date(self) -> None:
        for date in ("tomorrow", "2026-02-31", "2026-13-01", "\uff12\uff10\uff12\uff16-11-02", "20261102", "2026-11-02T00:00"):
            with self.subTest(date=date):
                code, _, err = run("release", "prepare", "1.0.0", "--date", date, "--root", str(self.root))
                self.assertEqual(code, 1)
                self.assertIn("isn't a date", err)
                self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), "0.1.0\n")

    def test_prepare_refuses_a_non_canonical_version(self) -> None:
        for version in ("01.0.0", "1.0.00", "\u0661.\u0660.\u0660"):
            with self.subTest(version=version):
                code, _, err = self.prepare(version)
                self.assertEqual(code, 1)
                self.assertIn("isn't a version", err)
                self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), "0.1.0\n")

    def test_a_marker_moved_back_after_prepare_fails_self_check(self) -> None:
        self.assertEqual(self.prepare()[0], 0)
        edit(self.root / "AGENTS.md", "<!-- fleet:begin git-and-prs v1.0.0 -->", "<!-- fleet:begin git-and-prs v0.1.0 -->")
        errors = self_check.check(self.root)
        self.assertEqual(errors, ["AGENTS.md: the git-and-prs block's marker says v0.1.0, but VERSION is 1.0.0"])


class NotesTest(unittest.TestCase):
    CHANGELOG = "# C\n\n## [Unreleased]\n\n## [1.1.0] - 2026-12-01\n\n### Added\n\n- New.\n\n## [1.0.0] - 2026-11-02\n\n- First.\n"

    def test_notes_print_the_section(self) -> None:
        self.assertEqual(release.notes(self.CHANGELOG, "1.1.0"), "### Added\n\n- New.\n")
        self.assertEqual(release.notes(self.CHANGELOG, "1.0.0"), "- First.\n")

    def test_notes_for_a_missing_or_empty_section_fail(self) -> None:
        with self.assertRaisesRegex(release.ReleaseError, "no `## \\[2.0.0\\]"):
            release.notes(self.CHANGELOG, "2.0.0")
        with self.assertRaisesRegex(release.ReleaseError, "empty"):
            release.notes("## [Unreleased]\n\n## [1.0.0] - 2026-11-02\n\n", "1.0.0")

    def test_the_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "CHANGELOG.md").write_text(self.CHANGELOG, encoding="utf-8")
            self.assertEqual(run("release", "notes", "1.0.0", "--root", directory), (0, "- First.\n", ""))
            code, out, err = run("release", "notes", "3.0.0", "--root", directory)
            self.assertEqual((code, out), (1, ""))
            self.assertIn("fleet release: CHANGELOG.md: no", err)


class ReleaseWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = RELEASE.read_text(encoding="utf-8")
        parsed = self_check.parse(self.text)
        assert isinstance(parsed, dict)
        self.workflow = parsed

    def test_it_runs_only_on_a_push_to_main_that_changes_version(self) -> None:
        self.assertEqual(self.workflow["on"], {"push": {"branches": "[main]", "paths": "[VERSION]"}})
        self.assertNotIn("pull_request", self.text)
        self.assertNotIn("workflow_run", self.text)

    def test_contents_write_only(self) -> None:
        self.assertEqual(self.workflow["permissions"], "{}")
        jobs = self.workflow["jobs"]
        assert isinstance(jobs, dict)
        self.assertEqual(list(jobs), ["release"])
        job = jobs["release"]
        assert isinstance(job, dict)
        self.assertEqual(job["permissions"], {"contents": "write"})
        self.assertEqual(job["if"], "github.repository == 'AnthonyPluth/fleet'")

    def test_actions_are_pinned_and_the_checkout_keeps_no_credentials(self) -> None:
        uses = re.findall(r"^\s*-? *uses: (\S+)(.*)$", self.text, re.MULTILINE)
        self.assertTrue(uses)
        for action, comment in uses:
            self.assertRegex(action, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
            self.assertRegex(comment, r"^\s+# v\d")
        self.assertIn("persist-credentials: false", self.text)

    def test_expressions_reach_the_shell_only_through_env(self) -> None:
        for number, line in enumerate(self.text.splitlines(), start=1):
            if "${{" not in line:
                continue
            with self.subTest(line=number):
                self.assertRegex(line, r"^ {10}[A-Z_]+: \$\{\{ [a-z.]+ \}\}$")

    def test_it_refuses_an_existing_tag_and_publishes_the_changelog_section(self) -> None:
        self.assertIn('gh api "repos/$REPOSITORY/git/ref/tags/$TAG"', self.text)
        self.assertIn("exit 1", self.text.split("Tag the commit", 1)[1].split("gh api \"repos/$REPOSITORY/git/refs\"", 1)[0])
        self.assertIn('-f "ref=refs/tags/$TAG" -f "sha=$SHA"', self.text)
        self.assertIn("fleet_tools.cli release notes", self.text)
        self.assertIn('--verify-tag --title "$TAG" --notes-file "$RUNNER_TEMP/notes.md"', self.text)
        self.assertLess(self.text.index("self-check"), self.text.index("Tag the commit"))

    def test_shellcheck_clean_where_available(self) -> None:
        if shutil.which("shellcheck") is None:
            self.skipTest("shellcheck isn't installed")
        scripts = re.findall(r"^        run: \|\n((?: {10}.*\n)+)", self.text, re.MULTILINE)
        scripts += [line + "\n" for line in re.findall(r"^        run: (?!\|)(.*)$", self.text, re.MULTILINE)]
        self.assertTrue(scripts)
        for script in scripts:
            body = "\n".join(line[10:] if line.startswith(" " * 10) else line for line in script.splitlines())
            result = subprocess.run(["shellcheck", "-s", "bash", "-"], input=body, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout)


FAKE_GH = """#!/bin/bash
printf '%s\\n' "$*" >> "$GH_LOG"
if [[ "$1" == api && "$2" == */git/ref/tags/* ]]; then
  if [[ -n "$FAKE_TAG_SHA" ]]; then
    echo "$FAKE_TAG_SHA"
    exit 0
  fi
  echo "HTTP 404: Not Found" >&2
  exit 1
fi
if [[ "$1" == release && "$2" == view ]]; then
  [[ -n "$FAKE_RELEASE" ]]
  exit
fi
exit 0
"""


def step_script(text: str, name: str) -> str:
    found = re.search(rf"^      - name: {re.escape(name)}\n(?:        .*\n)*?        run: \|\n((?: {{10}}.*\n|\n)+)", text, re.MULTILINE)
    assert found, name
    return "".join(line[10:] + "\n" for line in found.group(1).splitlines())


class ReleaseScriptsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = RELEASE.read_text(encoding="utf-8")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        bin_dir = self.directory / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
        (bin_dir / "gh").chmod(0o755)
        self.log = self.directory / "gh.log"
        self.log.write_text("", encoding="utf-8")
        self.env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "GH_LOG": str(self.log), "REPOSITORY": "example/fleet",
                    "SHA": "a" * 40, "TAG": "v1.0.0", "RUNNER_TEMP": str(self.directory), "GH_TOKEN": "fake"}

    def run_step(self, name: str, cwd: Path | None = None, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", "-eo", "pipefail", "-c", step_script(self.text, name)], cwd=cwd or self.directory,
                              env={**self.env, **env}, capture_output=True, text=True, check=False)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def test_a_missing_tag_is_created_on_the_commit(self) -> None:
        result = self.run_step("Tag the commit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[1], f"api repos/example/fleet/git/refs -f ref=refs/tags/v1.0.0 -f sha={'a' * 40} --silent")

    def test_a_tag_already_on_the_commit_passes_without_tagging_again(self) -> None:
        result = self.run_step("Tag the commit", FAKE_TAG_SHA="a" * 40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not tagging again", result.stdout)
        self.assertEqual(len(self.calls()), 1)

    def test_a_tag_on_another_commit_fails(self) -> None:
        result = self.run_step("Tag the commit", FAKE_TAG_SHA="b" * 40)
        self.assertEqual(result.returncode, 1)
        self.assertIn("never moved", result.stdout)
        self.assertEqual(len(self.calls()), 1)

    def test_an_existing_release_isnt_published_again(self) -> None:
        result = self.run_step("Publish the release", FAKE_RELEASE="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), ["release view v1.0.0 --repo example/fleet"])

    def test_a_missing_release_is_published_with_the_notes(self) -> None:
        result = self.run_step("Publish the release")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[1], f"release create v1.0.0 --repo example/fleet --verify-tag --title v1.0.0 --notes-file {self.directory}/notes.md")

    def test_the_version_step_takes_only_canonical_ascii_versions(self) -> None:
        work = self.directory / "work"
        work.mkdir()
        (work / "fleet_tools").symlink_to(ROOT / "fleet_tools")
        (work / "CHANGELOG.md").write_text("## [Unreleased]\n\n## [1.0.0] - 2026-11-02\n\n- First.\n", encoding="utf-8")
        output = self.directory / "output"
        for version in ("01.0.0", "1.00.0", "\u0661.\u0660.\u0660", "1.0", "v1.0.0"):
            with self.subTest(version=version):
                (work / "VERSION").write_text(version + "\n", encoding="utf-8")
                result = self.run_step("Read the version and the notes", cwd=work, GITHUB_OUTPUT=str(output))
                self.assertEqual(result.returncode, 1)
                self.assertIn("isn't MAJOR.MINOR.PATCH", result.stdout)
        (work / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        result = self.run_step("Read the version and the notes", cwd=work, GITHUB_OUTPUT=str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text(encoding="utf-8"), "tag=v1.0.0\n")
        self.assertEqual((self.directory / "notes.md").read_text(encoding="utf-8"), "- First.\n")


class AdoptingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (ROOT / release.ADOPTING).read_text(encoding="utf-8")

    def test_every_reusable_workflow_has_a_caller(self) -> None:
        for workflow in release.reusable_workflows(ROOT):
            with self.subTest(workflow=workflow.name):
                self.assertRegex(self.text, rf"uses: AnthonyPluth/fleet/\.github/workflows/{re.escape(workflow.name)}@<commit>   # vX\.Y\.Z")

    def test_secrets_are_passed_by_name(self) -> None:
        self.assertNotIn("secrets: inherit\n", self.text.replace("(`secrets: inherit`", ""))
        for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "FLEET_BOT_PRIVATE_KEY"):
            self.assertIn(f"      {name}: ${{{{ secrets.{name} }}}}", self.text)

    def test_the_adoption_steps(self) -> None:
        for needle in ("tools/fleet sync --to vX.Y.Z", ".fleet/version", '"ref": "vX.Y.Z"', "Merge gate",
                       'patterns: ["AnthonyPluth/fleet/*"]', "cooldown: { default-days: 3 }", "guides =",
                       "safety-critical", "review-mining", "manual"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.text)


if __name__ == "__main__":
    unittest.main()
