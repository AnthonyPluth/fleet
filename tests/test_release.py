import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from fleet_tools import agents_md, cli, release, self_check, sync_workflow

ROOT = cli.ROOT
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
CURRENT = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
PRE_RELEASE = CURRENT.startswith("0.")
MAJOR, MINOR, _ = (int(part) for part in CURRENT.split("."))
NEXT = "1.0.0" if PRE_RELEASE else f"{MAJOR}.{MINOR + 1}.0"
NEXT_MAJOR, NEXT_MINOR, _ = (int(part) for part in NEXT.split("."))
PATCH = f"{NEXT_MAJOR}.{NEXT_MINOR}.1"
LATER = f"{NEXT_MAJOR}.{NEXT_MINOR + 1}.0"
DOC_TAG = "vX.Y.Z" if PRE_RELEASE else f"v{CURRENT}"
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
        edit(self.root / "pyproject.toml", f'version = "{CURRENT}"', 'version = "0.0.9"')
        self.assert_one_error("pyproject.toml", "'0.0.9'")

    def test_a_snippet_version_that_disagrees_fails(self) -> None:
        edit(self.root / release.ADOPTING, f"security.yml@<commit>   # {DOC_TAG}", "security.yml@<commit>   # v0.0.9")
        self.assert_one_error("docs/adopting.md:", "v0.0.9")

    def test_a_settings_ref_that_disagrees_fails(self) -> None:
        edit(self.root / release.ADOPTING, f'"ref": "{DOC_TAG}"', '"ref": "v0.0.8"')
        self.assert_one_error("v0.0.8")

    def test_a_snippet_in_another_doc_that_disagrees_fails(self) -> None:
        edit(self.root / "docs" / "review-mining.md", f"@<commit>   # {DOC_TAG}", "@<commit>   # v0.0.9")
        self.assert_one_error("docs/review-mining.md:", "v0.0.9")

    def test_an_agents_marker_that_disagrees_fails(self) -> None:
        edit(self.root / "AGENTS.md", f"<!-- fleet:begin mistakes v{CURRENT} -->", "<!-- fleet:begin mistakes v0.0.9 -->")
        self.assert_one_error("AGENTS.md", "mistakes", "v0.0.9")

    def test_a_deleted_agents_block_fails_naming_it(self) -> None:
        path = self.root / "AGENTS.md"
        text = path.read_text(encoding="utf-8")
        block = next(block for block in agents_md.blocks(text) if block.name == "mistakes")
        path.write_text(text[:block.start] + text[block.end:], encoding="utf-8")
        self.assertEqual(self_check.check(self.root),
                         ["AGENTS.md: the mistakes block is missing; fleet ships it (agents-md/sections/mistakes.md)"])

    def test_a_renamed_agents_block_fails_naming_both(self) -> None:
        path = self.root / "AGENTS.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("fleet:begin mistakes ", "fleet:begin mistakes-old ").replace("fleet:end mistakes ", "fleet:end mistakes-old "),
                        encoding="utf-8")
        errors = self_check.check(self.root)
        self.assertEqual(len(errors), 2, errors)
        self.assertIn("the mistakes block is missing", errors[0])
        self.assertIn("the mistakes-old block isn't one fleet ships", errors[1])

    def test_fleets_agents_md_carries_every_shipped_block(self) -> None:
        names = [block.name for block in agents_md.blocks((ROOT / "AGENTS.md").read_text(encoding="utf-8"))]
        self.assertEqual(sorted(names), sorted(agents_md.ORDER))
        self.assertEqual(sorted(agents_md.sections(ROOT)), sorted(agents_md.ORDER))
        self.assertEqual(sorted(path.stem for path in (ROOT / "agents-md" / "sections").glob("*.md")), sorted(agents_md.ORDER))

    def test_a_missing_agents_md_fails(self) -> None:
        (self.root / "AGENTS.md").unlink()
        self.assertEqual(self_check.check(self.root), ["AGENTS.md: missing"])

    def test_version_is_read_raw(self) -> None:
        for raw in (b"1.0.0 ", b" 1.0.0", b"1.0.0\r\n", b"1.0.0\n\n", b"\xef\xbb\xbf1.0.0\n", b"1.0.0 \n", b"\n1.0.0", b"\xff"):
            with self.subTest(raw=raw):
                (self.root / "VERSION").write_bytes(raw)
                errors = self_check.check(self.root)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("isn't a version", errors[0])
                with self.assertRaisesRegex(release.ReleaseError, "isn't a version"):
                    release.read_version(self.root)
        for good in (CURRENT + "\n", CURRENT):
            (self.root / "VERSION").write_bytes(good.encode("ascii"))
            self.assertEqual(release.read_version(self.root), CURRENT)
            self.assertEqual(self_check.check(self.root), [])

    def test_a_malformed_snippet_tag_fails(self) -> None:
        for old, new, found in ((f"fleet sync --to {DOC_TAG}\n", "fleet sync --to v1.0\n", "v1.0"),
                                (f"security.yml@<commit>   # {DOC_TAG}", "security.yml@<commit>   # v1.0", "v1.0"),
                                (f'"ref": "{DOC_TAG}"', '"ref": "1.0.0"', "1.0.0"),
                                (f"<!-- fleet:begin mistakes v{CURRENT} -->", "<!-- fleet:begin mistakes v01.0.0 -->", "v01.0.0")):
            with self.subTest(new=new):
                root = copy(self)
                edit(root / release.ADOPTING, old, new)
                errors = self_check.check(root)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(f"a snippet says {found}, but VERSION is {CURRENT}", errors[0])

    def test_a_snippet_that_fetches_fleet_at_a_movable_ref_fails(self) -> None:
        old = 'fleet@<commit>" fleet sync'
        for ref in (DOC_TAG, "main"):
            with self.subTest(ref=ref):
                root = copy(self)
                edit(root / release.ADOPTING, old, f'fleet@{ref}" fleet sync')
                errors = self_check.check(root)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(f"a snippet fetches fleet at {ref!r}, a ref that can move; name the commit (`@<commit>`)", errors[0])
        root = copy(self)
        edit(root / release.ADOPTING, old, f'fleet@{"a" * 40}" fleet sync')
        self.assertEqual(self_check.check(root), [])

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
        changelog = self.root / "CHANGELOG.md"
        unreleased = changelog.read_text(encoding="utf-8").split("## [Unreleased]\n", 1)[1].split("\n## [", 1)[0]
        if not re.search(r"^- ", unreleased, re.MULTILINE):
            edit(changelog, "## [Unreleased]\n", "## [Unreleased]\n\n### Added\n\n- A change.\n")

    def prepare(self, version: str = NEXT) -> tuple[int, str, str]:
        return run("release", "prepare", version, "--date", "2026-11-02", "--root", str(self.root))

    def test_prepare_stamps_everything_and_self_check_passes(self) -> None:
        before = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        unreleased = before.split("## [Unreleased]\n", 1)[1].split("\n## [", 1)[0]
        code, out, err = self.prepare()
        self.assertEqual(code, 0, err)
        self.assertIn("stamped VERSION\n", out)
        self.assertEqual(self_check.check(self.root), [])
        self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), f"{NEXT}\n")
        self.assertEqual(json.loads((self.root / release.PLUGIN).read_text(encoding="utf-8"))["version"], NEXT)
        self.assertEqual(tomllib.loads((self.root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"], NEXT)
        changelog = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(changelog, before.replace("## [Unreleased]\n", f"## [Unreleased]\n\n## [{NEXT}] - 2026-11-02\n", 1))
        self.assertTrue(unreleased.strip())
        self.assertEqual(release.notes(changelog, NEXT), unreleased.strip("\n") + "\n")
        for workflow in release.reusable_workflows(self.root):
            text = workflow.read_text(encoding="utf-8")
            self.assertEqual(re.findall(r"^  FLEET_REF: (\S+)", text, re.MULTILINE), [f"v{NEXT}"], workflow.name)
        adopting = (self.root / release.ADOPTING).read_text(encoding="utf-8")
        self.assertEqual({tag for _, tag in release.snippet_versions(adopting)}, {f"v{NEXT}"})
        self.assertIn(f'   uvx --from "git+https://github.com/AnthonyPluth/fleet@<commit>" fleet sync --to v{NEXT}\n', adopting)
        self.assertIn(f'"ref": "v{NEXT}"', adopting)
        self.assertIn(f"review-mining.yml@<commit>   # v{NEXT}", (self.root / "docs" / "review-mining.md").read_text(encoding="utf-8"))
        agents = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual({block.version for block in agents_md.blocks(agents)}, {f"v{NEXT}"})
        self.assertEqual((self.root / ".github/workflows/ci.yml").read_text(encoding="utf-8"),
                         (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))

    def test_prepare_resyncs_the_fixtures(self) -> None:
        code, _, err = self.prepare()
        self.assertEqual(code, 0, err)
        fixtures = self.root / "tests" / "fixtures"
        for consumer in sorted(fixtures.glob("consumer-*")):
            with self.subTest(consumer=consumer.name):
                self.assertEqual((consumer / ".fleet/version").read_text(encoding="utf-8"), f"v{NEXT}\n{release.FIXTURE_COMMIT}\n")
                text = (consumer / "AGENTS.md").read_text(encoding="utf-8")
                self.assertEqual({block.version for block in agents_md.blocks(text)}, {f"v{NEXT}"})
                pins = re.findall(r"uses: AnthonyPluth/fleet/\S+@\S+\s+# (\S+)", "".join(
                    path.read_text(encoding="utf-8") for path in (consumer / ".github/workflows").glob("*.yml")))
                self.assertEqual(set(pins), {f"v{NEXT}"})
                self.assertIn(f"vendored from fleet v{NEXT} ", (consumer / "tools/fleet").read_text(encoding="utf-8"))
        drifted = fixtures / "drifted"
        self.assertEqual((drifted / ".fleet/version").read_text(encoding="utf-8"), f"v{NEXT}\n{release.FIXTURE_COMMIT}\n")
        self.assertEqual({block.version for block in agents_md.blocks((drifted / "AGENTS.md").read_text(encoding="utf-8"))}, {f"v{NEXT}"})

    def test_prepare_refuses_a_version_file_with_extra_whitespace(self) -> None:
        (self.root / "VERSION").write_bytes(f"{CURRENT} \n".encode("ascii"))
        code, _, err = self.prepare()
        self.assertEqual(code, 1)
        self.assertIn("isn't a version", err)

    def test_a_failing_fixture_sync_leaves_the_tree_untouched(self) -> None:
        def state() -> dict[str, tuple[str, int]]:
            return {path.relative_to(self.root).as_posix(): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode)
                    for path in sorted(self.root.rglob("*")) if path.is_file()}

        agents = sorted((self.root / "tests" / "fixtures").glob("consumer-*"))[-1] / "AGENTS.md"
        good = agents.read_text(encoding="utf-8")
        agents.write_text(good.replace("<!-- fleet:end mistakes -->", "", 1), encoding="utf-8")
        before = state()
        code, out, err = self.prepare()
        self.assertEqual(code, 1, out)
        self.assertIn("fleet release:", err)
        self.assertEqual(state(), before)
        agents.write_text(good, encoding="utf-8")
        code, _, err = self.prepare()
        self.assertEqual(code, 0, err)
        self.assertEqual(self_check.check(self.root), [])
        self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), f"{NEXT}\n")

    def test_an_unreleased_entry_with_a_fenced_heading_prepares(self) -> None:
        example = "- An entry with an example:\n\n  ```md\n## Example\n  ```\n"
        edit(self.root / "CHANGELOG.md", "## [Unreleased]\n", f"## [Unreleased]\n\n{example}")
        self.assertEqual(self_check.check(self.root), [])
        code, _, err = self.prepare()
        self.assertEqual(code, 0, err)
        notes = release.notes((self.root / "CHANGELOG.md").read_text(encoding="utf-8"), NEXT)
        self.assertTrue(notes.startswith(example), notes[:200])
        self.assertIn("\n## Example\n", notes)
        self.assertGreater(len(notes), len(example))

    def test_an_unterminated_fence_fails_self_check_and_prepare(self) -> None:
        edit(self.root / "CHANGELOG.md", "## [Unreleased]\n", "## [Unreleased]\n\n- Broken:\n\n```sh\n")
        errors = self_check.check(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertRegex(errors[0], r"^CHANGELOG\.md:\d+: a code fence that is never closed$")
        code, _, err = self.prepare()
        self.assertEqual(code, 1)
        self.assertIn("never closed", err)

    def test_the_release_after_that_moves_every_stamp_again(self) -> None:
        self.assertEqual(self.prepare()[0], 0)
        edit(self.root / "CHANGELOG.md", "## [Unreleased]\n", "## [Unreleased]\n\n### Fixed\n\n- A fix.\n")
        code, _, err = self.prepare(PATCH)
        self.assertEqual(code, 0, err)
        self.assertEqual(self_check.check(self.root), [])
        changelog = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(release.notes(changelog, PATCH), "### Fixed\n\n- A fix.\n")
        self.assertIn(f"## [{NEXT}] - 2026-11-02", changelog)
        adopting = (self.root / release.ADOPTING).read_text(encoding="utf-8")
        self.assertEqual({tag for _, tag in release.snippet_versions(adopting)}, {f"v{PATCH}"})

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
        code, _, err = self.prepare(NEXT)
        self.assertEqual(code, 1)
        self.assertIn(f"isn't newer than VERSION {NEXT}", err)

    def test_prepare_refuses_an_empty_unreleased_section(self) -> None:
        self.assertEqual(self.prepare()[0], 0)
        code, _, err = self.prepare(LATER)
        self.assertEqual(code, 1)
        self.assertIn("no entries to release", err)
        self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), f"{NEXT}\n")

    def test_prepare_refuses_stamps_that_already_disagree(self) -> None:
        edit(self.root / "pyproject.toml", f'version = "{CURRENT}"', 'version = "0.0.9"')
        code, _, err = self.prepare()
        self.assertEqual(code, 1)
        self.assertIn("disagree before preparing", err)
        self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), f"{CURRENT}\n")

    def test_prepare_refuses_a_bad_date(self) -> None:
        for date in ("tomorrow", "2026-02-31", "2026-13-01", "\uff12\uff10\uff12\uff16-11-02", "20261102", "2026-11-02T00:00"):
            with self.subTest(date=date):
                code, _, err = run("release", "prepare", NEXT, "--date", date, "--root", str(self.root))
                self.assertEqual(code, 1)
                self.assertIn("isn't a date", err)
                self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), f"{CURRENT}\n")

    def test_prepare_refuses_a_non_canonical_version(self) -> None:
        for version in ("01.0.0", "1.0.00", "\u0661.\u0660.\u0660"):
            with self.subTest(version=version):
                code, _, err = self.prepare(version)
                self.assertEqual(code, 1)
                self.assertIn("isn't a version", err)
                self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), f"{CURRENT}\n")

    def test_a_marker_moved_back_after_prepare_fails_self_check(self) -> None:
        self.assertEqual(self.prepare()[0], 0)
        edit(self.root / "AGENTS.md", f"<!-- fleet:begin git-and-prs v{NEXT} -->", f"<!-- fleet:begin git-and-prs v{CURRENT} -->")
        errors = self_check.check(self.root)
        self.assertEqual(errors, [f"AGENTS.md: the git-and-prs block's marker says v{CURRENT}, but VERSION is {NEXT}"])


@unittest.skipIf(os.environ.get("FLEET_RELEASE_E2E") == "1", "already inside the release rehearsal")
class ReleaseRehearsalTest(unittest.TestCase):
    def test_the_release_pull_request_passes_the_suite(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git isn't installed")
        root = copy(self)
        env = {**os.environ, "FLEET_RELEASE_E2E": "1", "GIT_AUTHOR_NAME": "fleet", "GIT_AUTHOR_EMAIL": "fleet@example.com",
               "GIT_COMMITTER_NAME": "fleet", "GIT_COMMITTER_EMAIL": "fleet@example.com"}
        env.pop("GITHUB_ACTIONS", None)
        changelog = root / "CHANGELOG.md"
        unreleased = changelog.read_text(encoding="utf-8").split("## [Unreleased]\n", 1)[1].split("\n## [", 1)[0]
        if not re.search(r"^- ", unreleased, re.MULTILINE):
            edit(changelog, "## [Unreleased]\n", "## [Unreleased]\n\n### Added\n\n- A change.\n")

        def step(*argv: str) -> str:
            done = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(done.returncode, 0, f"{' '.join(argv)}\n{done.stdout[-4000:]}{done.stderr[-4000:]}")
            return done.stdout + done.stderr

        step("git", "init", "-q", "-b", "main")
        step("git", "add", "-A")
        step("git", "commit", "-q", "-m", "chore: before the release")
        step(sys.executable, "-m", "fleet_tools", "release", "prepare", NEXT, "--date", "2026-11-02")
        step("git", "add", "-A")
        step("git", "commit", "-q", "-m", f"chore: release v{NEXT}")
        self.assertIn("fleet self-check: ok", step(sys.executable, "-m", "fleet_tools", "self-check"))
        for consumer in sorted((root / "tests/fixtures").glob("consumer-*")):
            step(sys.executable, "-m", "fleet_tools", "drift", "--root", str(consumer))
        self.assertRegex(step(sys.executable, "-m", "unittest", "discover", "tests"), r"\nOK")


class NotesTest(unittest.TestCase):
    CHANGELOG = "# C\n\n## [Unreleased]\n\n## [1.1.0] - 2026-12-01\n\n### Added\n\n- New.\n\n## [1.0.0] - 2026-11-02\n\n- First.\n"

    def test_notes_print_the_section(self) -> None:
        self.assertEqual(release.notes(self.CHANGELOG, "1.1.0"), "### Added\n\n- New.\n")
        self.assertEqual(release.notes(self.CHANGELOG, "1.0.0"), "- First.\n")

    def test_a_fenced_heading_is_part_of_the_section(self) -> None:
        changelog = ("# C\n\n## [Unreleased]\n\n## [1.1.0] - 2026-12-01\n\n- An example:\n\n```md\n## Example\n```\n\n"
                     "~~~~sh\n## [9.9.9] - 2026-01-01\n~~~\n~~~~\n\n- After.\n\n## [1.0.0] - 2026-11-02\n\n- First.\n")
        self.assertEqual(release.notes(changelog, "1.1.0"),
                         "- An example:\n\n```md\n## Example\n```\n\n~~~~sh\n## [9.9.9] - 2026-01-01\n~~~\n~~~~\n\n- After.\n")
        self.assertEqual(release.changelog_errors(changelog, "1.1.0"), [])
        self.assertEqual([section.heading for section in release.sections(changelog)],
                         ["## [Unreleased]", "## [1.1.0] - 2026-12-01", "## [1.0.0] - 2026-11-02"])
        between = sync_workflow.changelog_between(changelog, "v1.0.0", "v1.1.0")
        self.assertTrue(between.startswith("## [1.1.0] - 2026-12-01\n"))
        self.assertTrue(between.endswith("~~~\n~~~~\n\n- After.\n"))

    def test_an_unterminated_fence_is_an_error(self) -> None:
        changelog = "# C\n\n## [Unreleased]\n\n- An example:\n\n```md\n## Example\n"
        self.assertEqual(release.changelog_errors(changelog, "0.1.0"), ["CHANGELOG.md:7: a code fence that is never closed"])
        with self.assertRaisesRegex(release.ReleaseError, "CHANGELOG.md:7: a code fence that is never closed"):
            release.notes(changelog, "0.1.0")

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
        for version in ("01.0.0\n", "1.00.0\n", "\u0661.\u0660.\u0660\n", "1.0\n", "v1.0.0\n", "1.0.0 \n", " 1.0.0\n",
                        "1.0.0\r\n", "1.0.0\n\n", "1.0.0 ", "\ufeff1.0.0\n"):
            with self.subTest(version=version):
                (work / "VERSION").write_bytes(version.encode("utf-8"))
                result = self.run_step("Read the version and the notes", cwd=work, GITHUB_OUTPUT=str(output))
                self.assertEqual(result.returncode, 1)
                self.assertIn("isn't MAJOR.MINOR.PATCH", result.stdout)
        for version in ("1.0.0\n", "1.0.0"):
            (work / "VERSION").write_bytes(version.encode("utf-8"))
            output.write_text("", encoding="utf-8")
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
                self.assertRegex(self.text, rf"uses: AnthonyPluth/fleet/\.github/workflows/{re.escape(workflow.name)}@<commit>   # {re.escape(DOC_TAG)}")

    def test_secrets_are_passed_by_name(self) -> None:
        self.assertNotIn("secrets: inherit\n", self.text.replace("(`secrets: inherit`", ""))
        for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "FLEET_BOT_PRIVATE_KEY"):
            self.assertIn(f"      {name}: ${{{{ secrets.{name} }}}}", self.text)

    def test_the_adoption_steps(self) -> None:
        for needle in (f"fleet sync --to {DOC_TAG}", ".fleet/version", f'"ref": "{DOC_TAG}"', "Merge gate",
                       'patterns: ["AnthonyPluth/fleet/*"]', "cooldown: { default-days: 3 }", "guides =",
                       "safety-critical", "review-mining", "manual"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.text)


if __name__ == "__main__":
    unittest.main()
