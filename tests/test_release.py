import base64
import contextlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from consumers import GIT_ENV

from fleet_tools import agents_md, cli, release, self_check, sync, sync_workflow

ROOT = cli.ROOT
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
CI = WORKFLOWS / "ci.yml"
IGNORE = shutil.ignore_patterns(".git", ".tools", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache")
FROZEN_HEADING = f"## [{release.FROZEN}] - 2026-11-14\n"
SYSTEM_PYTHON = Path("/usr/bin/python3")
FIRST_RELEASE = (
    ("dry-run-never-carries-forward", "fix: the self-test's dry runs never carry a review verdict forward (#138)"),
    ("agent-review-narrow-claude-risky", "feat!: the second review no longer counts a skill's reference documents as always "
                                         "risky (#139)"),
    ("release-planner", "feat: fleet's release planner, notes from two tags and a title check (#141)"),
    ("release-on-merge", "feat: every merge to main is a release, and nothing in fleet's tree names its version (#142)"),
)
Node = dict[str, Any]


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


def fragment(root: Path, name: str, kind: str, entry: str) -> Path:
    path = root / release.FRAGMENTS / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nkind: {kind}\n---\n{entry}\n", encoding="utf-8")
    return path


def mapping(node: object) -> Node:
    assert isinstance(node, dict), node
    return node


def step_script(name: str) -> str:
    text = RELEASE.read_text(encoding="utf-8")
    found = re.search(rf"^      - name: {re.escape(name)}\n(?:        .*\n)*?        run: (.*)\n((?: {{10}}.*\n|\n)*)", text,
                      re.MULTILINE)
    assert found, name
    if found.group(1) != "|":
        return found.group(1) + "\n"
    return "".join(line[10:] + "\n" for line in found.group(2).splitlines())


class FleetItselfTest(unittest.TestCase):
    def test_fleet_names_no_version_of_its_own(self) -> None:
        self.assertEqual(self_check.check(ROOT), [])
        self.assertFalse((ROOT / "VERSION").exists())
        self.assertEqual(json.loads((ROOT / release.PLUGIN).read_text(encoding="utf-8"))["version"], release.UNVERSIONED)
        self.assertEqual(tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"],
                         release.UNVERSIONED)
        for path in sorted(WORKFLOWS.glob("*.yml")):
            self.assertNotRegex(path.read_text(encoding="utf-8"), r"(?m)^\s*FLEET_REF\s*:", path.name)
        snippets = {doc.as_posix(): {tag for _, tag in release.snippet_versions((ROOT / doc).read_text(encoding="utf-8"))}
                    for doc in release.docs(ROOT)}
        self.assertEqual(snippets["docs/adopting.md"], {release.PLACEHOLDER})
        self.assertEqual(snippets["docs/review-mining.md"], {release.PLACEHOLDER})
        self.assertEqual(set().union(*snippets.values()), {release.PLACEHOLDER})
        found = agents_md.blocks((ROOT / "AGENTS.md").read_text(encoding="utf-8"), own=True)
        self.assertEqual(sorted(block.name for block in found), sorted(agents_md.ORDER))
        self.assertEqual({block.version for block in found}, {""})

    def test_the_folded_notes_stop_at_5_0_0_and_the_fixtures_stay_there(self) -> None:
        self.assertEqual(release.FOLDED, "5.0.0")
        self.assertEqual(max((notes.version for notes in release.released(ROOT)[0]), key=release.version_key), release.FOLDED)
        self.assertEqual(release.FIXTURE_VERSION, "v5.0.0")
        for fixture in (*release.consumers(ROOT), ROOT / "tests/fixtures/drifted"):
            with self.subTest(fixture=fixture.name):
                self.assertEqual((fixture / ".fleet/version").read_text(encoding="utf-8"),
                                 f"{release.FIXTURE_VERSION}\n{release.FIXTURE_COMMIT}\n")


class StampTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = copy(self)

    def assert_one_error(self, *needles: str) -> None:
        errors = self_check.check(self.root)
        self.assertEqual(len(errors), 1, errors)
        for needle in needles:
            self.assertIn(needle, errors[0])

    def test_a_version_file_fails(self) -> None:
        for raw in (b"6.0.0\n", b"", b"vX.Y.Z\n"):
            with self.subTest(raw=raw):
                (self.root / "VERSION").write_bytes(raw)
                self.assertEqual(self_check.check(self.root), [
                    "VERSION: fleet keeps no version file: a release is a tag on a commit merged to main, and nothing "
                    "in the tree names one (docs/release.md); delete it"])
        (self.root / "VERSION").unlink()
        (self.root / "VERSION").symlink_to(self.root / "README.md")
        self.assert_one_error("VERSION: fleet keeps no version file")

    def test_a_plugin_version_fails(self) -> None:
        path = self.root / release.PLUGIN
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for version in ("6.0.0", None):
            with self.subTest(version=version):
                if version is None:
                    manifest.pop("version")
                else:
                    manifest["version"] = version
                path.write_text(json.dumps(manifest), encoding="utf-8")
                self.assertEqual(self_check.check(self.root), [
                    f"plugins/fleet/.claude-plugin/plugin.json: version is {version!r}; fleet stamps no version, so it "
                    "stays the constant '0.0.0' (docs/release.md)"])

    def test_a_pyproject_version_fails(self) -> None:
        edit(self.root / "pyproject.toml", 'version = "0.0.0"', 'version = "6.0.0"')
        self.assertEqual(self_check.check(self.root), [
            "pyproject.toml: [project] version is '6.0.0'; fleet stamps no version, so it stays the constant '0.0.0' "
            "(docs/release.md)"])

    def test_a_version_in_a_snippet_fails(self) -> None:
        for old, new, found in (("fleet sync --to vX.Y.Z\n", "fleet sync --to v6.0.0\n", "v6.0.0"),
                                ("security.yml@<commit>   # vX.Y.Z", "security.yml@<commit>   # v6.0.0", "v6.0.0"),
                                ('"ref": "vX.Y.Z"', '"ref": "v6.0.0"', "v6.0.0"),
                                ("<!-- fleet:begin mistakes vX.Y.Z -->", "<!-- fleet:begin mistakes v6.0.0 -->", "v6.0.0"),
                                ("security.yml@<commit>   # vX.Y.Z", "security.yml@<commit>   # v1.0", "v1.0"),
                                ("fleet sync --to vX.Y.Z\n", "fleet sync --to main\n", "main")):
            with self.subTest(new=new):
                root = copy(self)
                edit(root / release.ADOPTING, old, new)
                errors = self_check.check(root)
                self.assertEqual(len(errors), 1, errors)
                self.assertRegex(errors[0], rf"^docs/adopting\.md:\d+: a snippet says '{re.escape(found)}'; fleet stamps no "
                                            r"version, so a snippet names the release vX\.Y\.Z \(docs/release\.md\)$")
        edit(self.root / "docs" / "review-mining.md", "@<commit>   # vX.Y.Z", "@<commit>   # v6.0.0")
        self.assert_one_error("docs/review-mining.md:", "'v6.0.0'")

    def test_a_snippet_that_fetches_fleet_at_a_movable_ref_fails(self) -> None:
        old = 'fleet@<commit>" fleet sync'
        for ref in ("v6.0.0", "main"):
            with self.subTest(ref=ref):
                root = copy(self)
                edit(root / release.ADOPTING, old, f'fleet@{ref}" fleet sync')
                errors = self_check.check(root)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(f"a snippet fetches fleet at {ref!r}, a ref that can move; name the commit (`@<commit>`)", errors[0])
        edit(self.root / release.ADOPTING, old, f'fleet@{"a" * 40}" fleet sync')
        self.assertEqual(self_check.check(self.root), [])

    def test_a_version_outside_a_snippet_is_prose(self) -> None:
        edit(self.root / release.ADOPTING, "# Adopting fleet\n", "# Adopting fleet\n\nSince v6.0.0 (`fleet sync --to v6.0.0`).\n")
        self.assertEqual(self_check.check(self.root), [])

    def test_a_version_in_fleets_own_markers_fails_and_sync_self_takes_it_out(self) -> None:
        edit(self.root / "AGENTS.md", "<!-- fleet:begin mistakes -->", "<!-- fleet:begin mistakes v6.0.0 -->")
        self.assertEqual(self_check.check(self.root), [
            "AGENTS.md: the mistakes block's marker carries v6.0.0; fleet's own markers carry no version (`poetry run fleet "
            "sync --self` writes them)"])
        self.assertEqual(sync.sync_self(self.root), ["AGENTS.md: replaced mistakes"])
        self.assertEqual(self_check.check(self.root), [])

    def test_an_edited_block_fails_and_sync_self_puts_it_back(self) -> None:
        edit(self.root / "AGENTS.md", "<!-- fleet:end mistakes -->", "- **Mine**.\n<!-- fleet:end mistakes -->")
        self.assertEqual(self_check.check(self.root), [
            "AGENTS.md: the mistakes block differs from agents-md/sections/mistakes.md; edit the section, never the "
            "block, then `poetry run fleet sync --self`"])
        self.assertEqual(sync.sync_self(self.root), ["AGENTS.md: replaced mistakes"])
        self.assertEqual(self_check.check(self.root), [])

    def test_a_deleted_agents_block_fails_naming_it(self) -> None:
        path = self.root / "AGENTS.md"
        text = path.read_text(encoding="utf-8")
        block = next(block for block in agents_md.blocks(text, own=True) if block.name == "mistakes")
        path.write_text(text[:block.start] + text[block.end:], encoding="utf-8")
        self.assertEqual(self_check.check(self.root),
                         ["AGENTS.md: the mistakes block is missing; fleet ships it (agents-md/sections/mistakes.md)"])

    def test_a_renamed_agents_block_fails_naming_both(self) -> None:
        path = self.root / "AGENTS.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("fleet:begin mistakes -->", "fleet:begin mistakes-old -->")
                        .replace("fleet:end mistakes -->", "fleet:end mistakes-old -->"), encoding="utf-8")
        errors = self_check.check(self.root)
        self.assertEqual(len(errors), 2, errors)
        self.assertIn("the mistakes block is missing", errors[0])
        self.assertIn("the mistakes-old block isn't one fleet ships", errors[1])

    def test_a_malformed_marker_or_a_missing_agents_md_fails(self) -> None:
        edit(self.root / "AGENTS.md", "<!-- fleet:end mistakes -->\n", "")
        self.assert_one_error("AGENTS.md: `<!-- fleet:begin git-and-prs -->` opens a block inside `mistakes`")
        (self.root / "AGENTS.md").unlink()
        self.assertEqual(self_check.check(self.root), ["AGENTS.md: missing"])

    def test_notes_after_the_last_folded_release_fail(self) -> None:
        later = self.root / release.RELEASE_NOTES / "6.0.0.md"
        later.write_text("## [6.0.0] - 2026-11-17\n\n### Fixed\n\n- Later.\n", encoding="utf-8")
        self.assertEqual(self_check.check(self.root), [
            f"changelog.d/released/6.0.0.md: changelog.d/released/ is frozen at {release.FOLDED}, the last release whose "
            "fragments were folded; a later release's notes are the changelog fragments it adds (docs/release.md)"])

    def test_an_unreleased_section_fails_as_the_changelog_is_frozen(self) -> None:
        edit(self.root / "CHANGELOG.md", FROZEN_HEADING, "## [Unreleased]\n\n### Added\n\n- A change.\n\n" + FROZEN_HEADING)
        self.assert_one_error(f"CHANGELOG.md: it has an `## [Unreleased]` section, but it is frozen at {release.FROZEN}: a "
                              "change's entry is a fragment, changelog.d/<name>.md")

    def test_a_section_newer_than_the_freeze_fails(self) -> None:
        edit(self.root / "CHANGELOG.md", FROZEN_HEADING, "## [99.0.0] - 2026-12-01\n\n- New.\n\n" + FROZEN_HEADING)
        self.assert_one_error(f"CHANGELOG.md: it is frozen at {release.FROZEN}, but its newest section is 99.0.0: a later "
                              "release's notes are the changelog fragments it adds, changelog.d/<name>.md (docs/release.md)")

    def test_a_malformed_released_heading_or_fence_fails(self) -> None:
        edit(self.root / "CHANGELOG.md", FROZEN_HEADING, "## [0.1.0]\n\n- Old.\n\n" + FROZEN_HEADING)
        self.assert_one_error("CHANGELOG.md", "isn't `## [X.Y.Z] - YYYY-MM-DD`")
        root = copy(self)
        with (root / "CHANGELOG.md").open("a", encoding="utf-8") as changelog:
            changelog.write("\n- Broken:\n\n```sh\n")
        errors = self_check.check(root)
        self.assertEqual(len(errors), 1, errors)
        self.assertRegex(errors[0], r"^CHANGELOG\.md:\d+: a code fence that is never closed$")

    def test_a_fragment_that_doesnt_parse_fails(self) -> None:
        fragment(self.root, "broken", "feature", "A change.")
        self.assert_one_error("changelog.d/broken.md: kind 'feature' isn't one of breaking, added, changed, fixed")

    def test_a_missing_file_fails(self) -> None:
        (self.root / release.PLUGIN).unlink()
        self.assert_one_error("plugin.json: missing")

    def test_an_impossible_release_date_fails(self) -> None:
        self.assertEqual(release.changelog_errors("# C\n\n## [1.0.0] - 2026-02-31\n\n- x\n"),
                         ["CHANGELOG.md: the section for 1.0.0 has the impossible date 2026-02-31"])

    def test_versions_are_canonical_ascii(self) -> None:
        for text in ("01.0.0", "1.00.0", "1.0.01", "\u0661.\u0660.\u0660", "1.0", "1.0.0-rc.1"):
            with self.subTest(text=text), self.assertRaises(release.ReleaseError):
                release.parse_version(text)
        for text in ("0.0.0", "1.0.0", "10.20.30"):
            self.assertEqual(release.parse_version(text), tuple(int(part) for part in text.split(".")))

    def test_the_release_command_that_wrote_the_stamps_is_gone(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            cli.main(["release", "prepare", "6.0.0", "--root", str(self.root)])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid choice: 'prepare'", err.getvalue())


class NotesTest(unittest.TestCase):
    CHANGELOG = "# C\n\n## [Unreleased]\n\n## [1.1.0] - 2026-12-01\n\n### Added\n\n- New.\n\n## [1.0.0] - 2026-11-02\n\n- First.\n"

    def test_notes_print_the_section(self) -> None:
        self.assertEqual(release.notes(self.CHANGELOG, "1.1.0"), "### Added\n\n- New.\n")
        self.assertEqual(release.notes(self.CHANGELOG, "1.0.0"), "- First.\n")

    def test_a_fenced_heading_is_part_of_the_section(self) -> None:
        changelog = ("# C\n\n## [1.1.0] - 2026-12-01\n\n- An example:\n\n```md\n## Example\n```\n\n"
                     "~~~~sh\n## [9.9.9] - 2026-01-01\n~~~\n~~~~\n\n- After.\n\n## [1.0.0] - 2026-11-02\n\n- First.\n")
        self.assertEqual(release.notes(changelog, "1.1.0"),
                         "- An example:\n\n```md\n## Example\n```\n\n~~~~sh\n## [9.9.9] - 2026-01-01\n~~~\n~~~~\n\n- After.\n")
        self.assertEqual(release.changelog_errors(changelog), [])
        self.assertEqual(release.entries(changelog), 3)
        self.assertEqual([section.heading for section in release.sections(changelog)],
                         ["## [1.1.0] - 2026-12-01", "## [1.0.0] - 2026-11-02"])
        between = sync_workflow.changelog_between(changelog, "v1.0.0", "v1.1.0")
        self.assertTrue(between.startswith("## [1.1.0] - 2026-12-01\n"))
        self.assertTrue(between.endswith("~~~\n~~~~\n\n- After.\n"))

    def test_an_unterminated_fence_is_an_error(self) -> None:
        changelog = "# C\n\n## [Unreleased]\n\n- An example:\n\n```md\n## Example\n"
        self.assertEqual(release.changelog_errors(changelog), ["CHANGELOG.md:7: a code fence that is never closed"])
        with self.assertRaisesRegex(release.ReleaseError, "CHANGELOG.md:7: a code fence that is never closed"):
            release.notes(changelog, "0.1.0")

    def test_notes_for_a_missing_or_empty_section_fail(self) -> None:
        with self.assertRaisesRegex(release.ReleaseError, "no notes for 2.0.0: neither a `## \\[2.0.0\\] - YYYY-MM-DD` "
                                    "section in CHANGELOG.md nor changelog.d/released/2.0.0.md"):
            release.notes(self.CHANGELOG, "2.0.0")
        with self.assertRaisesRegex(release.ReleaseError, "empty"):
            release.notes("## [Unreleased]\n\n## [1.0.0] - 2026-11-02\n\n", "1.0.0")

    def test_the_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "CHANGELOG.md").write_text(self.CHANGELOG, encoding="utf-8")
            self.assertEqual(run("release", "notes", "1.0.0", "--root", directory), (0, "- First.\n", ""))
            code, out, err = run("release", "notes", "3.0.0", "--root", directory)
            self.assertEqual((code, out), (1, ""))
            self.assertIn("fleet release: no notes for 3.0.0", err)

    def test_a_folded_release_is_read_from_its_notes(self) -> None:
        self.assertEqual(run("release", "notes", "5.0.0", "--root", str(ROOT))[1],
                         release.notes((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), "5.0.0", release.released(ROOT)[0]))
        self.assertTrue(run("release", "notes", "5.0.0", "--root", str(ROOT))[1].startswith("### Changed\n\n- **Breaking:** "))


class ReleaseWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = RELEASE.read_text(encoding="utf-8")
        self.workflow = mapping(self_check.parse(self.text))
        self.jobs = {name: mapping(job) for name, job in mapping(self.workflow["jobs"]).items()}

    def steps(self, job: str) -> list[Node]:
        steps = self.jobs[job]["steps"]
        assert isinstance(steps, list)
        return [mapping(step) for step in steps]

    def test_it_runs_on_every_push_to_main_one_run_at_a_time(self) -> None:
        self.assertEqual(self.workflow["on"], {"push": {"branches": "[main]"}})
        for trigger in ("pull_request", "workflow_run", "workflow_dispatch", "paths:"):
            self.assertNotIn(trigger, self.text)
        self.assertEqual(self.workflow["concurrency"], {"group": "fleet-release", "cancel-in-progress": "false"})

    def test_the_jobs_and_what_each_may_do(self) -> None:
        self.assertEqual(self.workflow["permissions"], "{}")
        self.assertEqual(list(self.jobs), ["check", "plan", "release"])
        self.assertEqual({name: job["permissions"] for name, job in self.jobs.items()},
                         {"check": {"contents": "read"}, "plan": {"contents": "read"}, "release": {"contents": "write"}})
        self.assertEqual([self.jobs[name].get("needs") for name in self.jobs], [None, "check", "plan"])
        guard = "github.repository == 'AnthonyPluth/fleet'"
        self.assertEqual([job["if"] for job in self.jobs.values()],
                         [guard, guard, f"{guard} && needs.plan.outputs.skip != 'true'"])
        self.assertEqual(mapping(self.jobs["plan"]["outputs"]), {
            "version": "${{ steps.next.outputs.version }}", "skip": "${{ steps.next.outputs.skip }}",
            "notes": "${{ steps.notes.outputs.notes }}"})

    def test_the_check_is_fleet_cis_make_check(self) -> None:
        ci = mapping(mapping(self_check.parse(CI.read_text(encoding="utf-8")))["jobs"])["check"]
        self.assertEqual(self.steps("check"), [mapping(step) for step in mapping(ci)["steps"]])
        self.assertEqual(self.steps("check")[-1], {"name": "make check", "env": {"GH_TOKEN": "${{ github.token }}"},
                                                   "run": "make check"})

    def test_the_plan_runs_the_planner_and_the_notes_on_the_system_python(self) -> None:
        checkout, planner, notes = self.steps("plan")
        self.assertTrue(str(checkout["uses"]).startswith("actions/checkout@"))
        self.assertEqual(checkout["with"], {"fetch-depth": "0", "persist-credentials": "false"})
        self.assertEqual(planner["run"], '/usr/bin/python3 -E -s -m fleet_tools.cli next-version --first-parent --fragments '
                                         '--sha "$SHA"')
        self.assertEqual(planner["env"], {"SHA": "${{ github.sha }}"})
        self.assertEqual(notes["if"], "steps.next.outputs.skip != 'true'")
        script = step_script("Write the notes")
        self.assertIn("git describe --tags --abbrev=0 --first-parent --match 'v[0-9]*.[0-9]*.[0-9]*' \"$SHA^\"", script)
        self.assertIn('/usr/bin/python3 -E -s -m fleet_tools.cli release notes "${VERSION#v}" --since "$previous" --at '
                      '"$SHA"', script)

    def test_the_write_job_runs_only_gh_on_checked_values(self) -> None:
        steps = self.steps("release")
        self.assertEqual([step["name"] for step in steps], ["Check the plan", "Tag the commit", "Publish the release"])
        self.assertTrue(all("uses" not in step for step in steps))
        scripts = "".join(step_script(str(step["name"])) for step in steps)
        for forbidden in ("python", "fleet_tools", "make", "git ", "./", "source", "eval", "curl"):
            self.assertNotIn(forbidden, scripts)
        self.assertEqual(steps[1]["env"], {"GH_TOKEN": "${{ github.token }}", "REPOSITORY": "${{ github.repository }}",
                                           "SHA": "${{ github.sha }}", "TAG": "${{ steps.plan.outputs.tag }}"})
        self.assertNotIn("GH_TOKEN", mapping(steps[0]["env"]))
        self.assertIn('--verify-tag --title "$TAG" --notes-file "$RUNNER_TEMP/notes.md"', scripts)

    def test_actions_are_pinned_and_the_checkouts_keep_no_credentials(self) -> None:
        uses = re.findall(r"^\s*-? *uses: (\S+)(.*)$", self.text, re.MULTILINE)
        self.assertEqual(len(uses), 3)
        for action, comment in uses:
            self.assertRegex(action, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
            self.assertRegex(comment, r"^\s+# v\d")
        self.assertEqual(self.text.count("persist-credentials: false"), 2)

    def test_expressions_reach_the_shell_only_through_env(self) -> None:
        for number, line in enumerate(self.text.splitlines(), start=1):
            if "${{" not in line:
                continue
            with self.subTest(line=number):
                self.assertRegex(line, r"^(?: {10}[A-Z_]+| {6}[a-z]+): \$\{\{ [a-z.-]+ \}\}$")

    def test_shellcheck_clean_where_available(self) -> None:
        if shutil.which("shellcheck") is None:
            self.skipTest("shellcheck isn't installed")
        names = re.findall(r"^      - name: (.+)$", self.text, re.MULTILINE)
        scripts = [step_script(name) for name in names if re.search(rf"^      - name: {re.escape(name)}\n(?:        .*\n)*?"
                                                                    r"        run: ", self.text, re.MULTILINE)]
        self.assertGreater(len(scripts), 6)
        for script in scripts:
            result = subprocess.run(["shellcheck", "-s", "bash", "-"], input=script, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout)


STUB_GH = """#!/bin/bash
set -eo pipefail
printf '%s\\n' "$*" >> "$GH_LOG"
if [[ "$1" == api && "$2" == repos/*/git/ref/tags/* ]]; then
  if ! sha=$(git -C "$ORIGIN" rev-parse -q --verify "refs/tags/${2##*/git/ref/tags/}^{commit}"); then
    echo "gh: Not Found (HTTP 404)" >&2
    exit 1
  fi
  echo "$sha"
  exit 0
fi
if [[ "$1" == api && "$2" == repos/*/git/refs && "$3 $5 $7" == "-f -f --silent" ]]; then
  git -C "$ORIGIN" update-ref "${4#ref=}" "${6#sha=}" ""
  exit 0
fi
if [[ "$1" == release && "$2" == view ]]; then
  [[ -f "$RELEASES/$3" ]]
  exit
fi
if [[ "$1" == release && "$2" == create && "$4 $6 $7 $8 $9" == "--repo --verify-tag --title $3 --notes-file" ]]; then
  git -C "$ORIGIN" rev-parse -q --verify "refs/tags/$3" > /dev/null || { echo "gh: tag $3 doesn't exist" >&2; exit 1; }
  cp "${10}" "$RELEASES/$3"
  exit 0
fi
echo "gh: unexpected call: $*" >&2
exit 2
"""


class Remote:
    def __init__(self, test: unittest.TestCase) -> None:
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.test = test
        self.base = Path(temporary.name)
        self.origin = self.base / "origin"
        self.releases = self.base / "releases"
        self.releases.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        (self.bin / "gh").write_text(STUB_GH, encoding="utf-8")
        (self.bin / "gh").chmod(0o755)
        self.log = self.base / "gh.log"
        self.log.write_text("", encoding="utf-8")
        self.runs = 0
        shutil.copytree(ROOT / "fleet_tools", self.origin / "fleet_tools", ignore=IGNORE)
        shutil.copy2(ROOT / "CHANGELOG.md", self.origin / "CHANGELOG.md")
        shutil.copytree(ROOT / release.RELEASE_NOTES, self.origin / release.RELEASE_NOTES)
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "chore: release v5.0.0 (#137)")
        self.git("tag", "v5.0.0")

    def env(self, **extra: str) -> dict[str, str]:
        return {**GIT_ENV, "PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.base), "GH_LOG": str(self.log),
                "ORIGIN": str(self.origin), "RELEASES": str(self.releases), "GH_TOKEN": "fake", "LC_ALL": "C.UTF-8", **extra}

    def git(self, *args: str, where: Path | None = None) -> str:
        return subprocess.run(["git", *args], cwd=where or self.origin, check=True, capture_output=True, text=True,
                              env=self.env()).stdout

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    def land(self, title: str, files: Mapping[str, str | None], squash: bool = False) -> str:
        if squash:
            self.change(files)
            self.git("commit", "-q", "--allow-empty", "-m", title)
            return self.head()
        self.git("checkout", "-q", "-b", "change")
        self.change(files)
        self.git("commit", "-q", "--allow-empty", "-m", f"wip: {title}")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", title, "change")
        self.git("branch", "-q", "-D", "change")
        return self.head()

    def change(self, files: Mapping[str, str | None]) -> None:
        for path, text in files.items():
            target = self.origin / path
            if text is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        self.git("add", "-A")

    def script(self, name: str, cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", "-eo", "pipefail", "-c", step_script(name)], cwd=cwd, env=self.env(**env),
                              capture_output=True, text=True, check=False)

    def outputs(self, path: Path) -> dict[str, str]:
        return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())

    def plan(self, sha: str | None = None) -> tuple[dict[str, str], str]:
        sha = sha or self.head()
        self.runs += 1
        work = self.base / f"run-{self.runs}"
        work.mkdir()
        checkout = work / "fleet"
        self.git("clone", "-q", "--no-local", str(self.origin), str(checkout), where=self.base)
        self.git("checkout", "-q", "--detach", sha, where=checkout)
        output = work / "plan-output"
        output.write_text("", encoding="utf-8")
        temp = work / "plan-temp"
        temp.mkdir()
        logs = []
        done = self.script("Work out the next version", checkout, SHA=sha, GITHUB_OUTPUT=str(output), RUNNER_TEMP=str(temp))
        self.test.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        logs.append(done.stdout)
        if self.outputs(output).get("skip") != "true":
            done = self.script("Write the notes", checkout, SHA=sha, VERSION=self.outputs(output)["version"],
                               GITHUB_OUTPUT=str(output), RUNNER_TEMP=str(temp))
            self.test.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            logs.append(done.stdout)
        return self.outputs(output), "".join(logs)

    def release(self, plan: dict[str, str], sha: str | None = None) -> list[subprocess.CompletedProcess[str]]:
        self.test.assertNotEqual(plan.get("skip"), "true", "the release job runs only when the plan doesn't skip")
        sha = sha or self.head()
        self.runs += 1
        temp = self.base / f"release-temp-{self.runs}"
        temp.mkdir()
        output = self.base / f"release-output-{self.runs}"
        output.write_text("", encoding="utf-8")
        empty = self.base / f"release-workspace-{self.runs}"
        empty.mkdir()
        checked = self.script("Check the plan", empty, VERSION=plan.get("version", ""), NOTES=plan.get("notes", ""),
                              GITHUB_OUTPUT=str(output), RUNNER_TEMP=str(temp))
        done = [checked]
        if checked.returncode != 0:
            return done
        tag = self.outputs(output)["tag"]
        for name in ("Tag the commit", "Publish the release"):
            done.append(self.script(name, empty, REPOSITORY="example/fleet", SHA=sha, TAG=tag, RUNNER_TEMP=str(temp)))
            if done[-1].returncode != 0:
                break
        return done

    def published(self) -> dict[str, str]:
        return {path.name: path.read_text(encoding="utf-8") for path in sorted(self.releases.iterdir())}

    def tags(self) -> dict[str, str]:
        listing = self.git("tag", "--list", "v*", "--format=%(refname:short) %(objectname)").split("\n")
        return dict(line.split(" ", 1) for line in listing if line)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()


def notes_of(plan: dict[str, str]) -> str:
    return base64.b64decode(plan["notes"]).decode("utf-8")


def first_release_fragments() -> dict[str, str]:
    return {name: (ROOT / release.FRAGMENTS / f"{name}.md").read_text(encoding="utf-8") for name, _ in FIRST_RELEASE}


@unittest.skipUnless(SYSTEM_PYTHON.is_file() and shutil.which("git"), "the release runs on the runner's /usr/bin/python3 and git")
class RehearsalTest(unittest.TestCase):
    def first_release(self, remote: Remote, squash: bool = False) -> str:
        texts = first_release_fragments()
        for name, title in FIRST_RELEASE:
            remote.land(title, {f"changelog.d/{name}.md": texts[name]}, squash=squash)
        return remote.head()

    def expected_first_notes(self) -> str:
        found = [release.parse_fragment(Path(f"changelog.d/{name}.md"), text) for name, text in first_release_fragments().items()]
        return "\n".join(release.grouped(found)).strip("\n") + "\n"

    def test_this_merge_is_v6_0_0_with_the_four_pending_fragments(self) -> None:
        for squash in (False, True):
            with self.subTest(squash=squash):
                remote = Remote(self)
                head = self.first_release(remote, squash)
                plan, log = remote.plan()
                self.assertEqual({key: value for key, value in plan.items() if key != "notes"},
                                 {"newest": "true", "version": "v6.0.0", "new": "true"})
                self.assertIn("Next version: v6.0.0 (last in this history: v5.0.0; highest: v5.0.0; a major for "
                              "changelog.d/agent-review-narrow-claude-risky.md (kind: breaking))", log)
                notes = notes_of(plan)
                self.assertEqual(notes, self.expected_first_notes())
                self.assertEqual(re.findall(r"^### (\w+)$", notes, re.MULTILINE), ["Added", "Changed", "Fixed"])
                self.assertEqual(notes.count("\n- "), 4)
                self.assertIn("\n- **Breaking:** `agent-review.yml`'s second review", notes)
                self.assertIn("The notes of v6.0.0, since v5.0.0:", log)
                done = remote.release(plan)
                self.assertEqual([step.returncode for step in done], [0, 0, 0], [step.stdout + step.stderr for step in done])
                self.assertEqual(remote.tags()["v6.0.0"], head)
                self.assertEqual(remote.published(), {"v6.0.0": notes})
                self.assertEqual(remote.calls(), [
                    "api repos/example/fleet/git/ref/tags/v6.0.0 --jq .object.sha",
                    f"api repos/example/fleet/git/refs -f ref=refs/tags/v6.0.0 -f sha={head} --silent",
                    "release view v6.0.0 --repo example/fleet",
                    f"release create v6.0.0 --repo example/fleet --verify-tag --title v6.0.0 --notes-file "
                    f"{remote.base}/release-temp-{remote.runs}/notes.md"])

    def test_a_re_run_reuses_the_tag_and_publishes_nothing_twice(self) -> None:
        remote = Remote(self)
        head = self.first_release(remote)
        plan, _ = remote.plan()
        self.assertEqual([step.returncode for step in remote.release(plan)], [0, 0, 0])
        again, _ = remote.plan()
        self.assertEqual({key: value for key, value in again.items() if key != "notes"},
                         {"newest": "true", "version": "v6.0.0", "new": "false"})
        self.assertEqual(notes_of(again), notes_of(plan))
        done = remote.release(again)
        self.assertEqual([step.returncode for step in done], [0, 0, 0])
        self.assertIn(f"v6.0.0 is already on {head}; not tagging again", done[1].stdout)
        self.assertIn("The release v6.0.0 exists already; not publishing again", done[2].stdout)
        self.assertEqual(sum(call.startswith(("release create", "api repos/example/fleet/git/refs ")) for call in remote.calls()), 2)

    def test_a_tag_on_another_commit_is_never_moved(self) -> None:
        remote = Remote(self)
        self.first_release(remote)
        plan, _ = remote.plan()
        remote.git("tag", "v6.0.0", "v5.0.0")
        done = remote.release(plan)
        self.assertEqual([step.returncode for step in done], [0, 1])
        self.assertIn("::error::v6.0.0 exists already on", done[1].stdout)
        self.assertIn("a release tag is never moved", done[1].stdout)
        self.assertEqual(remote.published(), {})

    def test_each_later_merge_releases_what_it_adds_and_the_fragments_stay(self) -> None:
        remote = Remote(self)
        self.first_release(remote)
        plan, _ = remote.plan()
        remote.release(plan)
        steps = (
            ("fix: a message (#143)", {"changelog.d/a-message.md": "---\nkind: fixed\n---\nA message (#143).\n"},
             "v6.0.1", "### Fixed\n\n- A message (#143).\n"),
            ("docs: a sentence (#144)", {"docs/x.md": "A sentence.\n", "changelog.d/a-sentence.md":
                                         "---\nkind: changed\n---\nA sentence (#144).\n"},
             "v6.0.2", "### Changed\n\n- A sentence (#144).\n"),
            ("feat: a new input (#145)", {"changelog.d/a-new-input.md": "---\nkind: added\n---\nA new input (#145).\n"},
             "v6.1.0", "### Added\n\n- A new input (#145).\n"),
            ("deps: bump the actions group with 2 updates (#146)", {"poetry.lock": "new\n"},
             "v6.1.1", "### Changed\n\n- deps: bump the actions group with 2 updates (#146)\n"),
            ("fix: an input is gone (#147)", {"changelog.d/an-input-is-gone.md": "---\nkind: breaking\n---\nGone (#147).\n"},
             "v7.0.0", "### Changed\n\n- **Breaking:** Gone (#147).\n"),
        )
        for title, files, version, notes in steps:
            with self.subTest(title=title):
                head = remote.land(title, files, squash=True)
                plan, _ = remote.plan()
                self.assertEqual((plan["version"], plan["new"], notes_of(plan)), (version, "true", notes))
                self.assertEqual([step.returncode for step in remote.release(plan)], [0, 0, 0])
                self.assertEqual(remote.tags()[version], head)
        pending = sorted(path.name for path in (remote.origin / release.FRAGMENTS).glob("*.md"))
        self.assertEqual(len(pending), len(FIRST_RELEASE) + 4)
        self.assertEqual(len(remote.published()), len(steps) + 1)

    def test_rapid_merges_release_once_from_the_newest_commit(self) -> None:
        remote = Remote(self)
        self.first_release(remote)
        remote.release(remote.plan()[0])
        older = remote.land("fix: one (#150)", {"changelog.d/one.md": "---\nkind: fixed\n---\nOne (#150).\n"})
        remote.land("feat: two (#151)", {"changelog.d/two.md": "---\nkind: added\n---\nTwo (#151).\n"})
        skipped, log = remote.plan(older)
        self.assertEqual(skipped, {"newest": "false", "skip": "true"})
        self.assertIn("isn't the newest commit on main any more, so it isn't released", log)
        plan, _ = remote.plan()
        self.assertEqual((plan["version"], notes_of(plan)), ("v6.1.0", "### Added\n\n- Two (#151).\n\n### Fixed\n\n- One (#150).\n"))
        self.assertEqual([step.returncode for step in remote.release(plan)], [0, 0, 0])
        self.assertEqual(remote.plan(older)[0], {"newest": "false", "skip": "true"})
        self.assertEqual(sorted(remote.tags()), ["v5.0.0", "v6.0.0", "v6.1.0"])

    def test_a_bad_release_is_reverted_and_the_revert_is_the_next_patch(self) -> None:
        remote = Remote(self)
        self.first_release(remote)
        remote.release(remote.plan()[0])
        bad = remote.land("feat: a bad input (#152)", {"feature.txt": "A bad input.\n",
                                                       "changelog.d/a-bad-input.md": "---\nkind: added\n---\nA bad input (#152).\n"})
        plan, _ = remote.plan()
        self.assertEqual(plan["version"], "v6.1.0")
        remote.release(plan)
        remote.git("checkout", "-q", "-b", "revert-152")
        remote.git("revert", "--no-edit", "-m", "1", bad)
        title = 'Revert "feat: a bad input (#152)"'
        code, _, err = run("release", "check", "--base", "main", "--title", title, "--root", str(remote.origin))
        self.assertEqual(code, 1)
        self.assertIn("changelog.d/a-bad-input.md: a merged fragment, which a pull request doesn't change or delete", err)
        remote.git("checkout", "-q", bad, "--", "changelog.d/a-bad-input.md")
        remote.change({"changelog.d/revert-a-bad-input.md": "---\nkind: fixed\n---\nReverts #152, which v6.1.0 released "
                                                            "(#153).\n"})
        remote.git("commit", "-q", "-m", "fix: keep the reverted pull request's fragment, and add the revert's")
        self.assertEqual(run("release", "check", "--base", "main", "--title", title, "--root", str(remote.origin)),
                         (0, "fleet release check: ok, it adds changelog.d/revert-a-bad-input.md (fixed); its title makes "
                             "it a patch change\n", ""))
        remote.git("checkout", "-q", "main")
        remote.git("merge", "-q", "--no-ff", "-m", f"{title} (#153)", "revert-152")
        plan, _ = remote.plan()
        self.assertEqual((plan["version"], notes_of(plan)), ("v6.1.1", "### Fixed\n\n- Reverts #152, which v6.1.0 released (#153).\n"))
        self.assertEqual([step.returncode for step in remote.release(plan)], [0, 0, 0])
        self.assertFalse((remote.origin / "feature.txt").exists())
        self.assertEqual(remote.git("show", "v6.1.1:changelog.d/a-bad-input.md"),
                         remote.git("show", "v6.1.0:changelog.d/a-bad-input.md"))

    def test_the_plan_refuses_what_the_release_job_shouldnt_tag(self) -> None:
        remote = Remote(self)
        self.first_release(remote)
        plan, _ = remote.plan()
        for version in ("", "6.0.0", "v6.0", "v06.0.0", "v6.0.0\nv7.0.0", "v6.0.0; gh release delete v5.0.0", "v\u0666.0.0"):
            with self.subTest(version=version):
                done = remote.release({**plan, "version": version})
                self.assertEqual([step.returncode for step in done], [1])
                self.assertIn("isn't vX.Y.Z", done[0].stdout)
        for notes in ("", "not base64!", base64.b64encode(b"x").decode() + "\nversion=v9.0.0"):
            with self.subTest(notes=notes):
                done = remote.release({**plan, "notes": notes})
                self.assertEqual([step.returncode for step in done], [1])
                self.assertIn("::error::The plan's notes", done[0].stdout)
        self.assertEqual(remote.calls(), [])
        self.assertEqual(sorted(remote.tags()), ["v5.0.0"])


if __name__ == "__main__":
    unittest.main()
