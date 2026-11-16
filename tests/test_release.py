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
BREAKING_PENDING = any(fragment.kind == "breaking" for fragment in release.fragments(ROOT)[0])
NEXT = "1.0.0" if PRE_RELEASE else f"{MAJOR + 1}.0.0" if BREAKING_PENDING else f"{MAJOR}.{MINOR + 1}.0"
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


def fragment(root: Path, name: str, kind: str, entry: str) -> Path:
    path = root / release.FRAGMENTS / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nkind: {kind}\n---\n{entry}\n", encoding="utf-8")
    return path


def only_fragments(root: Path, *found: tuple[str, str, str]) -> None:
    for pending in release.fragments(root)[0]:
        (root / pending.path).unlink()
    for name, kind, entry in found:
        fragment(root, name, kind, entry)


def ensure_a_fragment(root: Path) -> None:
    if not release.fragments(root)[0]:
        fragment(root, "a-change", "added", "A change.")


FROZEN_HEADING = f"## [{release.FROZEN}] - 2026-11-14\n"


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
        self.assertEqual(release.changelog_errors("# C\n\n## [1.0.0] - 2026-02-31\n\n- x\n"),
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

    def test_notes_newer_than_version_fail(self) -> None:
        later = self.root / release.RELEASE_NOTES / f"{LATER}.md"
        later.parent.mkdir(parents=True, exist_ok=True)
        later.write_text(f"## [{LATER}] - 2026-12-01\n\n### Fixed\n\n- Later.\n", encoding="utf-8")
        self.assert_one_error(f"changelog.d/released/{LATER}.md: the newest release's notes are for {LATER}, but VERSION "
                              f"is {CURRENT}")

    def test_a_version_without_notes_fails(self) -> None:
        notes = self.root / release.RELEASE_NOTES / f"{CURRENT}.md"
        if notes.is_file():
            notes.unlink()
        else:
            edit(self.root / "CHANGELOG.md", FROZEN_HEADING, "## [0.0.9] - 2026-11-14\n")
        hint = (f" (`fleet release prepare {CURRENT}` writes changelog.d/released/{CURRENT}.md)"
                if release.parse_version(CURRENT) > release.parse_version(release.FROZEN) else "")
        errors = self_check.check(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].endswith(f"but VERSION is {CURRENT}{hint}"), errors)

    def test_an_unreleased_section_fails_as_the_changelog_is_frozen(self) -> None:
        edit(self.root / "CHANGELOG.md", FROZEN_HEADING, "## [Unreleased]\n\n### Added\n\n- A change.\n\n" + FROZEN_HEADING)
        self.assert_one_error(f"CHANGELOG.md: it has an `## [Unreleased]` section, but it is frozen at {release.FROZEN}: a "
                              "change's entry is a fragment, changelog.d/<name>.md")

    def test_a_section_newer_than_the_freeze_fails(self) -> None:
        edit(self.root / "CHANGELOG.md", FROZEN_HEADING, "## [99.0.0] - 2026-12-01\n\n- New.\n\n" + FROZEN_HEADING)
        self.assert_one_error(f"CHANGELOG.md: it is frozen at {release.FROZEN}, but its newest section is 99.0.0")

    def test_a_malformed_released_heading_fails(self) -> None:
        edit(self.root / "CHANGELOG.md", FROZEN_HEADING, "## [0.1.0]\n\n- Old.\n\n" + FROZEN_HEADING)
        self.assert_one_error("CHANGELOG.md", "isn't `## [X.Y.Z] - YYYY-MM-DD`")

    def test_a_fragment_that_doesnt_parse_fails(self) -> None:
        fragment(self.root, "broken", "feature", "A change.")
        self.assert_one_error("changelog.d/broken.md: kind 'feature' isn't one of breaking, added, changed, fixed")

    def test_a_missing_stamped_file_fails(self) -> None:
        (self.root / release.PLUGIN).unlink()
        self.assert_one_error("plugin.json: missing")

    def test_a_placeholder_after_1_0_fails(self) -> None:
        self.assertEqual(release.adopting_errors("```yaml\nuses: AnthonyPluth/fleet/x.yml@abc   # vX.Y.Z\n```\n", "0.1.0"), [])
        errors = release.adopting_errors("```yaml\nuses: AnthonyPluth/fleet/x.yml@abc   # vX.Y.Z\n```\n", "1.0.0")
        self.assertEqual(errors, ["docs/adopting.md:2: a snippet says vX.Y.Z, but VERSION is 1.0.0"])

    def test_1_x_needs_release_notes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "CHANGELOG.md").write_text("# C\n", encoding="utf-8")
            self.assertEqual(release.history_errors(root, "0.1.0"), [])
            self.assertEqual(release.history_errors(root, "1.0.0"),
                             ["no release notes for VERSION 1.0.0: neither a `## [1.0.0] - YYYY-MM-DD` section in "
                              "CHANGELOG.md nor changelog.d/released/1.0.0.md"])
            (root / "CHANGELOG.md").write_text("# C\n\n## [1.0.0] - 2026-11-02\n\n- x\n", encoding="utf-8")
            self.assertEqual(release.history_errors(root, "1.0.0"), [])


class PrepareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = copy(self)
        ensure_a_fragment(self.root)

    def prepare(self, version: str = NEXT) -> tuple[int, str, str]:
        return run("release", "prepare", version, "--date", "2026-11-02", "--root", str(self.root))

    def test_prepare_stamps_everything_and_self_check_passes(self) -> None:
        before = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        pending = release.fragments(self.root)[0]
        code, out, err = self.prepare()
        self.assertEqual(code, 0, err)
        self.assertIn("stamped VERSION\n", out)
        self.assertIn(f"stamped changelog.d/released/{NEXT}.md\n", out)
        self.assertEqual(self_check.check(self.root), [])
        self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), f"{NEXT}\n")
        self.assertEqual(json.loads((self.root / release.PLUGIN).read_text(encoding="utf-8"))["version"], NEXT)
        self.assertEqual(tomllib.loads((self.root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"], NEXT)
        self.assertEqual((self.root / "CHANGELOG.md").read_text(encoding="utf-8"), before)
        rendered = release.render(NEXT, "2026-11-02", pending)
        self.assertEqual((self.root / release.RELEASE_NOTES / f"{NEXT}.md").read_text(encoding="utf-8"), rendered)
        self.assertEqual(release.fragments(self.root), ([], []))
        for folded in pending:
            self.assertIn(f"folded {folded.path.as_posix()} into changelog.d/released/{NEXT}.md\n", out)
            self.assertIn(folded.entry, rendered)
        self.assertEqual(release.notes(before, NEXT, release.released(self.root)[0]),
                         rendered.split("\n", 1)[1].strip("\n") + "\n")
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
        notes_folder = (self.root / release.RELEASE_NOTES).is_dir()
        code, out, err = self.prepare()
        self.assertEqual(code, 1, out)
        self.assertIn("fleet release:", err)
        self.assertEqual(state(), before)
        self.assertEqual((self.root / release.RELEASE_NOTES).is_dir(), notes_folder)
        agents.write_text(good, encoding="utf-8")
        code, _, err = self.prepare()
        self.assertEqual(code, 0, err)
        self.assertEqual(self_check.check(self.root), [])
        self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), f"{NEXT}\n")

    def test_the_notes_group_the_fragments_by_kind(self) -> None:
        only_fragments(self.root, ("z-fix", "fixed", "Fixed z."), ("b-break", "breaking", "Broke b."),
                       ("d-add", "added", "Added d."), ("c-change", "changed", "Changed c."), ("a-add", "added", "Added a."))
        major = f"{MAJOR + 1}.0.0"
        code, _, err = self.prepare(major)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.root / release.RELEASE_NOTES / f"{major}.md").read_text(encoding="utf-8"),
                         f"## [{major}] - 2026-11-02\n\n### Added\n\n- Added a.\n- Added d.\n\n### Changed\n\n"
                         "- **Breaking:** Broke b.\n- Changed c.\n\n### Fixed\n\n- Fixed z.\n")
        self.assertEqual(self_check.check(self.root), [])

    def test_prepare_refuses_a_minor_or_a_patch_when_a_fragment_is_breaking(self) -> None:
        only_fragments(self.root, ("a-break", "breaking", "Broke a."), ("b-fix", "fixed", "Fixed b."))
        for version in (f"{MAJOR}.{MINOR + 1}.0", f"{MAJOR}.{MINOR}.{int(CURRENT.split('.')[2]) + 1}"):
            with self.subTest(version=version):
                code, out, err = self.prepare(version)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(f"changelog.d/a-break.md: `kind: breaking`, which makes the release a major, {MAJOR + 1}.0.0, "
                              f"not {version}", err)
                self.assertEqual((self.root / "VERSION").read_text(encoding="utf-8"), f"{CURRENT}\n")
                self.assertTrue((self.root / "changelog.d/a-break.md").is_file())

    def test_an_unterminated_fence_fails_self_check_and_prepare(self) -> None:
        with (self.root / "CHANGELOG.md").open("a", encoding="utf-8") as changelog:
            changelog.write("\n- Broken:\n\n```sh\n")
        errors = self_check.check(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertRegex(errors[0], r"^CHANGELOG\.md:\d+: a code fence that is never closed$")
        code, _, err = self.prepare()
        self.assertEqual(code, 1)
        self.assertIn("never closed", err)

    def test_the_release_after_that_moves_every_stamp_again(self) -> None:
        self.assertEqual(self.prepare()[0], 0)
        fragment(self.root, "a-fix", "fixed", "A fix.")
        code, _, err = self.prepare(PATCH)
        self.assertEqual(code, 0, err)
        self.assertEqual(self_check.check(self.root), [])
        changelog = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        later = release.released(self.root)[0]
        self.assertEqual(release.notes(changelog, PATCH, later), "### Fixed\n\n- A fix.\n")
        self.assertIn(NEXT, [notes.version for notes in later])
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
        fragment(self.root, "more", "changed", "More.")
        code, _, err = self.prepare(NEXT)
        self.assertEqual(code, 1)
        self.assertIn(f"isn't newer than VERSION {NEXT}", err)

    def test_prepare_refuses_a_release_without_fragments(self) -> None:
        self.assertEqual(self.prepare()[0], 0)
        code, _, err = self.prepare(LATER)
        self.assertEqual(code, 1)
        self.assertIn("changelog.d/ has no fragments to release", err)
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
        ensure_a_fragment(root)

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
            step(sys.executable, "-m", "fleet_tools", "drift", "--root", str(consumer), "--own-commit", release.FIXTURE_COMMIT)
        self.assertRegex(step(sys.executable, "-m", "unittest", "discover", "tests"), r"\nOK")


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

    def test_a_later_release_is_read_from_its_notes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "CHANGELOG.md").write_text(self.CHANGELOG, encoding="utf-8")
            notes = root / release.RELEASE_NOTES / "5.0.0.md"
            notes.parent.mkdir(parents=True)
            notes.write_text("## [5.0.0] - 2026-12-02\n\n### Changed\n\n- **Breaking:** Moved.\n", encoding="utf-8")
            self.assertEqual(run("release", "notes", "5.0.0", "--root", directory), (0, "### Changed\n\n- **Breaking:** Moved.\n", ""))
            self.assertEqual(run("release", "notes", "1.1.0", "--root", directory), (0, "### Added\n\n- New.\n", ""))


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

    def pending_step(self, root: Path) -> subprocess.CompletedProcess[str]:
        return self.run_step("Check that no fragment is pending", cwd=root)

    def release_commit(self, *late: str) -> Path:
        work = self.directory / "work"
        (work / release.RELEASE_NOTES).mkdir(parents=True)
        (work / "fleet_tools").symlink_to(ROOT / "fleet_tools")
        (work / release.RELEASE_NOTES / "4.1.0.md").write_text("## [4.1.0] - 2026-11-20\n\n### Fixed\n\n- Folded.\n",
                                                                encoding="utf-8")
        for name in late:
            fragment(work, name, "breaking", "Merged after prepare ran.")
        return work

    def test_a_fragment_pending_at_the_release_commit_stops_the_release(self) -> None:
        result = self.pending_step(self.release_commit("late-change", "later-change"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::Pending at the release commit: changelog.d/late-change.md changelog.d/later-change.md.",
                      result.stdout)
        self.assertIn("Not tagging", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_release_commit_with_every_fragment_folded_passes(self) -> None:
        result = self.pending_step(self.release_commit())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No changelog fragment is pending", result.stdout)

    def test_nothing_is_tagged_or_published_before_the_pending_check(self) -> None:
        steps = re.findall(r"^      - name: (.+)$", self.text, re.MULTILINE)
        self.assertLess(steps.index("Check that no fragment is pending"), steps.index("Tag the commit"))
        self.assertLess(steps.index("Check that no fragment is pending"), steps.index("Publish the release"))

    def test_the_pending_check_passes_what_prepare_leaves_and_refuses_a_fragment_merged_after_it(self) -> None:
        root = copy(self)
        ensure_a_fragment(root)
        self.assertEqual(self.pending_step(root).returncode, 1)
        code, _, err = run("release", "prepare", NEXT, "--date", "2026-11-02", "--root", str(root))
        self.assertEqual(code, 0, err)
        folded = self.pending_step(root)
        self.assertEqual(folded.returncode, 0, folded.stdout + folded.stderr)
        fragment(root, "merged-after-prepare", "breaking", "Merged to main after prepare ran.")
        self.assertEqual(self_check.check(root), [])
        late = self.pending_step(root)
        self.assertEqual(late.returncode, 1)
        self.assertIn("::error::Pending at the release commit: changelog.d/merged-after-prepare.md.", late.stdout)

    def test_the_version_step_reads_a_later_releases_notes_from_its_file(self) -> None:
        work = self.directory / "work"
        (work / release.RELEASE_NOTES).mkdir(parents=True)
        (work / "fleet_tools").symlink_to(ROOT / "fleet_tools")
        (work / "CHANGELOG.md").write_text(f"# Changelog\n\n## [{release.FROZEN}] - 2026-11-14\n\n- Frozen.\n", encoding="utf-8")
        (work / release.RELEASE_NOTES / "4.1.0.md").write_text("## [4.1.0] - 2026-11-20\n\n### Fixed\n\n- Folded.\n",
                                                                encoding="utf-8")
        (work / "VERSION").write_text("4.1.0\n", encoding="utf-8")
        output = self.directory / "output"
        result = self.run_step("Read the version and the notes", cwd=work, GITHUB_OUTPUT=str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text(encoding="utf-8"), "tag=v4.1.0\n")
        self.assertEqual((self.directory / "notes.md").read_text(encoding="utf-8"), "### Fixed\n\n- Folded.\n")


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
                       'patterns: ["AnthonyPluth/fleet", "AnthonyPluth/fleet/*"]', "schedule: { interval: daily }", "cooldown: { default-days: 3 }", "guides =",
                       "safety-critical", "review-mining", "manual"):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.text)


if __name__ == "__main__":
    unittest.main()
