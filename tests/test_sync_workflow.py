import base64
import contextlib
import io
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import sync_fixture
from consumers import FIXTURES, RUNWAY, WAYPOINT, commit, copy, git, temporary, write

from fleet_tools import (
    agents_md,
    cli,
    drift,
    release,
    self_check,
    sync_workflow,
    vendor,
)

ROOT = cli.ROOT
VERSION = agents_md.own_version()
WORKFLOWS = ROOT / ".github" / "workflows"
SYNC = WORKFLOWS / "sync.yml"
CHECKS = WORKFLOWS / "checks.yml"
SELF_TEST = WORKFLOWS / "self-test.yml"
DRIFTED = FIXTURES / "drifted"
PIN_FILE = ".github/workflows/merge-gate.yml"
SHA_PIN = re.compile(r"@([0-9a-f]{40})\s+# v\d+\.\d+\.\d+$")
CHANGELOG = """# Changelog

## [Unreleased]

### Added

- Unreleased work.

## [1.2.0] - 2026-03-01

### Added

- A third thing.

## [1.1.0] - 2026-02-01

### Changed

- A second thing.

## [1.0.0] - 2026-01-01

### Added

- The first release.
"""
Node = dict[str, Any]


def mapping(node: object) -> Node:
    assert isinstance(node, dict), node
    return node


def parsed(path: Path) -> Node:
    return mapping(self_check.parse(path.read_text(encoding="utf-8")))


def steps_of(workflow: Node, job: str) -> list[Node]:
    steps = mapping(workflow["jobs"])[job]["steps"]
    assert isinstance(steps, list)
    return [mapping(step) for step in steps]


def step_sources(text: str, job: str) -> dict[str, str]:
    found = re.search(rf"^  {re.escape(job)}:\n(.*?)(?=^  \S|\Z)", text, re.MULTILINE | re.DOTALL)
    assert found, job
    steps = found.group(1).split("    steps:\n", 1)[1]
    sources = {}
    for chunk in re.split(r"^      - ", steps, flags=re.MULTILINE):
        if all(line.strip().startswith("#") for line in chunk.splitlines() if line.strip()):
            continue
        name = re.search(r"^name: (.*)$", chunk, re.MULTILINE)
        if name is None:
            uses = re.search(r"^uses: (.*)$", chunk, re.MULTILINE)
            path = re.search(r"^ +path: (.*)$", chunk, re.MULTILINE)
            assert uses, chunk
            key = uses.group(1).strip() + (f" path: {path.group(1).strip()}" if path else "")
        else:
            key = name.group(1).strip()
        assert key not in sources, key
        sources[key] = chunk
    return sources


def run_bodies(text: str) -> list[str]:
    return [body for _, body in re.findall(r"^( +)run: \|\n((?:\1  .*\n|\n)+)", text, re.MULTILINE)]


def consumer(test: unittest.TestCase, pins: str, content: str) -> Path:
    return sync_fixture.build(temporary(test) / "app", pins, content)


def plan_cli(root: Path, *args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["sync-plan", "--root", str(root), *args])
    return code, out.getvalue(), err.getvalue()


class Planner(unittest.TestCase):
    def test_pins_a_minor_ahead_plan_a_sync_pull_request(self) -> None:
        found = sync_workflow.plan(consumer(self, "v1.1.0", "v1.0.0"))
        self.assertEqual((found.action, found.content, found.pin), ("sync", "v1.0.0", "v1.1.0"))
        self.assertEqual(found.files, (".github/workflows/merge-gate.yml", ".github/workflows/sync.yml"))
        self.assertEqual(found.title, "chore: fleet content v1.1.0")

    def test_pins_a_patch_ahead_plan_a_sync_too(self) -> None:
        self.assertEqual(sync_workflow.plan(consumer(self, "v1.0.3", "v1.0.0")).action, "sync")

    def test_pins_equal_to_the_content_plan_nothing(self) -> None:
        found = sync_workflow.plan(consumer(self, "v1.0.0", "v1.0.0"))
        self.assertEqual((found.action, found.title), ("none", ""))
        self.assertIn("agree at v1.0.0", found.reason)

    def test_pins_a_major_ahead_plan_the_migration_issue(self) -> None:
        found = sync_workflow.plan(consumer(self, "v2.0.0", "v1.0.0"))
        self.assertEqual(found.action, "migration")
        self.assertEqual(found.title, "fleet v2 needs a migration: see CHANGELOG")
        self.assertEqual(sync_workflow.plan(consumer(self, "v3.1.0", "v1.4.0")).title, "fleet v3 needs a migration: see CHANGELOG")

    def test_pins_that_disagree_fail_naming_the_files(self) -> None:
        root = consumer(self, "v1.1.0", "v1.0.0")
        path = root / ".github/workflows/sync.yml"
        path.write_text(path.read_text(encoding="utf-8").replace("# v1.1.0", "# v1.2.0"), encoding="utf-8")
        with self.assertRaises(sync_workflow.PlanError) as raised:
            sync_workflow.plan(root)
        self.assertEqual(str(raised.exception), "the fleet pins disagree: v1.1.0 in .github/workflows/merge-gate.yml; v1.2.0 in "
                                                ".github/workflows/sync.yml; bump them together (Dependabot's fleet group does)")
        code, out, err = plan_cli(root)
        self.assertEqual((code, out), (1, ""))
        self.assertIn(".github/workflows/merge-gate.yml", err)
        self.assertIn(".github/workflows/sync.yml", err)

    def test_a_pin_without_a_version_comment_fails(self) -> None:
        root = consumer(self, "v1.1.0", "v1.0.0")
        path = root / PIN_FILE
        path.write_text(path.read_text(encoding="utf-8").replace("   # v1.1.0", ""), encoding="utf-8")
        with self.assertRaises(sync_workflow.PlanError) as raised:
            sync_workflow.plan(root)
        self.assertIn(f"{PIN_FILE}: the fleet pin @0123456789abcdef0123456789abcdef01234567 has no version", str(raised.exception))

    def test_the_plan_carries_the_pins_commit_and_pins_at_two_commits_fail(self) -> None:
        root = consumer(self, "v1.1.0", "v1.0.0")
        self.assertEqual(sync_workflow.plan(root).commit, release.FIXTURE_COMMIT)
        path = root / PIN_FILE
        path.write_text(path.read_text(encoding="utf-8").replace(release.FIXTURE_COMMIT, "f" * 40), encoding="utf-8")
        with self.assertRaises(sync_workflow.PlanError) as raised:
            sync_workflow.plan(root)
        self.assertEqual(str(raised.exception), f"the fleet pins at v1.1.0 name more than one commit: {release.FIXTURE_COMMIT} in "
                                                f".github/workflows/sync.yml; {'f' * 40} in {PIN_FILE}; bump them together "
                                                "(Dependabot's fleet group does)")

    def test_pins_that_name_no_commit_cannot_be_synced_to(self) -> None:
        for content in ("v1.0.0", "v1.1.0"):
            for every in (True, False):
                with self.subTest(content=content, every=every):
                    root = consumer(self, "v1.1.0", content)
                    paths = sorted((root / ".github/workflows").glob("*.yml")) if every else [root / PIN_FILE]
                    for path in paths:
                        path.write_text(path.read_text(encoding="utf-8").replace(f"@{release.FIXTURE_COMMIT}", "@main"),
                                        encoding="utf-8")
                    with self.assertRaises(sync_workflow.PlanError) as raised:
                        sync_workflow.plan(root)
                    self.assertIn(f"{PIN_FILE}: the fleet pin at v1.1.0 is @main, a ref that can move; pin it by full "
                                  "commit SHA", str(raised.exception))
        self.assertEqual(sync_workflow.plan(consumer(self, "v1.0.0", "v1.0.0")).commit, release.FIXTURE_COMMIT)

    def test_pins_moved_to_another_commit_at_the_same_version_sync(self) -> None:
        root = consumer(self, "v1.1.0", "v1.1.0")
        self.assertEqual(sync_workflow.plan(root).action, "none")
        other = "f" * 40
        for path in (root / ".github/workflows").glob("*.yml"):
            path.write_text(path.read_text(encoding="utf-8").replace(release.FIXTURE_COMMIT, other), encoding="utf-8")
        found = sync_workflow.plan(root)
        self.assertEqual((found.action, found.commit), ("sync", other))
        self.assertIn(f"commit {other}; .fleet/version names {release.FIXTURE_COMMIT}", found.reason)
        write(root, ".fleet/version", "v1.1.0\n")
        found = sync_workflow.plan(root)
        self.assertEqual(found.action, "sync")
        self.assertIn(".fleet/version names no commit", found.reason)
        root = consumer(self, "v1.0.0", "v1.0.0")
        write(root, ".fleet/version", "v1.0.0\n")
        self.assertEqual(sync_workflow.plan(root).action, "none")

    def test_a_version_file_with_a_malformed_commit_fails(self) -> None:
        root = consumer(self, "v1.1.0", "v1.0.0")
        write(root, ".fleet/version", "v1.0.0\nnot-a-commit\n")
        with self.assertRaises(sync_workflow.PlanError) as raised:
            sync_workflow.plan(root)
        self.assertEqual(str(raised.exception), ".fleet/version line 2 holds 'not-a-commit', not a commit (40 hex digits)")
        write(root, ".fleet/version", "v1.0.0\n")
        self.assertEqual(sync_workflow.plan(root).action, "sync")

    def test_a_commit_with_whitespace_around_it_fails_like_the_shim(self) -> None:
        root = consumer(self, "v1.1.0", "v1.0.0")
        for line in (f" {'a' * 40}", f"{'a' * 40} ", f"{'a' * 40}\r"):
            with self.subTest(line=line):
                write(root, ".fleet/version", f"v1.0.0\n{line}\n")
                with self.assertRaises(sync_workflow.PlanError) as raised:
                    sync_workflow.plan(root)
                self.assertTrue(str(raised.exception).startswith(f".fleet/version line 2 holds {line!r}: "))

    def test_content_newer_than_the_pins_fails(self) -> None:
        with self.assertRaises(sync_workflow.PlanError) as raised:
            sync_workflow.plan(consumer(self, "v1.0.0", "v1.1.0"))
        self.assertIn("fleet content v1.1.0 is newer than the workflow pin v1.0.0", str(raised.exception))

    def test_no_version_file_fails_asking_for_the_first_sync_by_hand(self) -> None:
        root = consumer(self, "v1.1.0", "v1.0.0")
        (root / ".fleet/version").unlink()
        with self.assertRaises(sync_workflow.PlanError) as raised:
            sync_workflow.plan(root)
        self.assertIn(".fleet/version doesn't exist: adopt fleet's content by hand first", str(raised.exception))
        write(root, ".fleet/version", "nonsense\n")
        with self.assertRaises(sync_workflow.PlanError):
            sync_workflow.plan(root)

    def test_no_fleet_pin_is_nothing_to_do(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        (root / PIN_FILE).unlink()
        found = sync_workflow.plan(root)
        self.assertEqual((found.action, found.pin), ("none", ""))
        self.assertIn("no fleet pin", found.reason)

    def test_the_fixture_consumers_are_in_step(self) -> None:
        for fixture in (WAYPOINT, RUNWAY):
            self.assertEqual(sync_workflow.plan(fixture).action, "none")

    def test_the_command_prints_the_plan_as_outputs(self) -> None:
        code, out, err = plan_cli(consumer(self, "v1.1.0", "v1.0.0"))
        self.assertEqual((code, err), (0, ""))
        lines = dict(line.split("=", 1) for line in out.splitlines())
        self.assertEqual(lines["action"], "sync")
        self.assertEqual(lines["pin"], "v1.1.0")
        self.assertEqual(lines["commit"], release.FIXTURE_COMMIT)
        self.assertEqual(lines["content"], "v1.0.0")
        self.assertEqual(lines["title"], "chore: fleet content v1.1.0")
        self.assertEqual(lines["branch"], "fleet/sync")
        self.assertEqual(lines["files"], ".github/workflows/merge-gate.yml, .github/workflows/sync.yml")
        for value in lines.values():
            self.assertNotIn("\n", value)

    def test_a_failure_is_an_annotation_on_github_actions(self) -> None:
        root = consumer(self, "v1.1.0", "v1.0.0")
        (root / ".fleet/version").unlink()
        with mock.patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}):
            code, _, err = plan_cli(root)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("::error::fleet sync-plan: "))

    def test_no_root_exits_2(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), mock.patch.object(Path, "cwd", return_value=Path("/")):
            self.assertEqual(cli.main(["sync-plan"]), 2)


class Changelog(unittest.TestCase):
    def test_the_entries_between_two_versions_in_file_order(self) -> None:
        between = sync_workflow.changelog_between(CHANGELOG, "v1.0.0", "v1.2.0")
        self.assertEqual(between, "## [1.2.0] - 2026-03-01\n\n### Added\n\n- A third thing.\n\n## [1.1.0] - 2026-02-01\n\n"
                                  "### Changed\n\n- A second thing.\n")
        self.assertNotIn("Unreleased", between)
        self.assertNotIn("first release", between)

    def test_one_version_up(self) -> None:
        self.assertEqual(sync_workflow.changelog_between(CHANGELOG, "v1.1.0", "v1.2.0"), "## [1.2.0] - 2026-03-01\n\n### Added\n\n- A third thing.\n")

    def test_no_entry_between_says_so(self) -> None:
        self.assertEqual(sync_workflow.changelog_between("# Changelog\n\n## [Unreleased]\n\n- x\n", "v1.0.0", "v1.1.0"),
                         "The CHANGELOG at v1.1.0 has no entry between v1.0.0 and v1.1.0.\n")

    def test_fleets_own_changelog_parses(self) -> None:
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIsInstance(sync_workflow.changelog_between(text, "v0.0.0", "v99.0.0"), str)


class Bodies(unittest.TestCase):
    def test_the_pull_request_body_names_the_versions_the_files_and_the_entries(self) -> None:
        found = sync_workflow.plan(consumer(self, "v1.2.0", "v1.0.0"))
        body = sync_workflow.body(found, CHANGELOG)
        for phrase in ("from v1.0.0 to v1.2.0", "`.github/workflows/merge-gate.yml`, `.github/workflows/sync.yml`",
                       "nothing under `.github/workflows/` changes", "fleet-bot", "`fleet/sync`",
                       "## CHANGELOG v1.0.0 → v1.2.0", "- A third thing.", "- A second thing."):
            self.assertIn(phrase, body)
        self.assertNotIn("first release", body)

    def test_the_issue_body_says_why_and_how(self) -> None:
        found = sync_workflow.plan(consumer(self, "v2.0.0", "v1.0.0"))
        body = sync_workflow.body(found, "## [2.0.0] - 2026-06-01\n\n### Changed\n\n- Breaking.\n")
        for phrase in ("another major version", "doesn't cross a major",
                       f'`uvx --from "git+https://github.com/AnthonyPluth/fleet@{release.FIXTURE_COMMIT}" fleet sync --to v2.0.0`',
                       "## CHANGELOG v1.0.0 → v2.0.0", "- Breaking."):
            self.assertIn(phrase, body)

    def test_the_pull_request_body_names_the_pins_commit(self) -> None:
        found = sync_workflow.plan(consumer(self, "v1.1.0", "v1.0.0"))
        self.assertIn(f"the pins' commit `{release.FIXTURE_COMMIT}`, which `tools/fleet` runs fleet at", sync_workflow.body(found, CHANGELOG))

    def test_nothing_to_do_has_no_body(self) -> None:
        self.assertEqual(sync_workflow.body(sync_workflow.plan(consumer(self, "v1.0.0", "v1.0.0")), CHANGELOG), "")

    def test_the_command_writes_the_body(self) -> None:
        root = consumer(self, "v1.1.0", "v1.0.0")
        changelog = write(root.parent, "CHANGELOG.md", CHANGELOG)
        out = root.parent / "body.md"
        code, _, err = plan_cli(root, "--changelog", str(changelog), "--body", str(out))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("- A second thing.", out.read_text(encoding="utf-8"))
        self.assertEqual(plan_cli(root, "--body", str(out))[0], 2)


class SyncWritesNoWorkflow(unittest.TestCase):
    def changed_by_a_sync(self, pins: str, content: str) -> tuple[Path, list[str]]:
        root = consumer(self, pins, content)
        before = {path.relative_to(root).as_posix(): path.read_bytes() for path in (root / ".github").rglob("*") if path.is_file()}
        with mock.patch.object(agents_md, "own_version", return_value=pins):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["sync", "--to", pins, "--root", str(root)]), 0, out.getvalue())
        git(root, "add", "-A")
        changed = sorted(git(root, "diff", "--cached", "--name-only").split())
        after = {path.relative_to(root).as_posix(): path.read_bytes() for path in (root / ".github").rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        return root, changed

    def test_the_sync_commit_is_exactly_the_blocks_the_vendored_files_and_the_version(self) -> None:
        _, changed = self.changed_by_a_sync("v1.1.0", "v1.0.0")
        self.assertEqual(changed, sync_fixture.expected())
        self.assertFalse(any(path.startswith(".github/") for path in changed))
        self.assertIn("AGENTS.md", changed)
        self.assertIn(".fleet/version", changed)

    def test_the_sync_never_writes_under_github_workflows(self) -> None:
        for pins, content in (("v1.1.0", "v1.0.0"), ("v1.0.1", "v1.0.0"), ("v2.3.0", "v2.0.0")):
            with self.subTest(pins=pins):
                root, changed = self.changed_by_a_sync(pins, content)
                self.assertEqual([path for path in changed if path.startswith(".github/")], [])
                self.assertEqual(git(root, "status", "--porcelain", "--", ".github"), "")

    def test_nothing_a_sync_writes_lives_under_github(self) -> None:
        shipped = vendor.shipped_at(VERSION)
        assert shipped
        for target in [*shipped, Path(agents_md.AGENTS), agents_md.VERSION_FILE]:
            self.assertNotEqual(target.parts[0], ".github", target)
        for source in vendor.FILES.values():
            self.assertNotEqual(source.parts[0], ".github", source)
        self.assertIsNone(vendor.target_of(Path(".github/workflows/sync.yml")))
        for pattern in vendor.RESERVED:
            self.assertFalse(pattern.startswith(".github"), pattern)


class Fixture(unittest.TestCase):
    def test_the_built_consumer_has_the_pins_and_the_content_asked_for(self) -> None:
        root = consumer(self, "v1.1.0", "v1.0.0")
        workflows = sorted(path.name for path in (root / ".github/workflows").glob("*.yml"))
        self.assertEqual(workflows, ["ci.yml", "merge-gate.yml", "sync.yml"])
        pinned, problems = drift.pins(root)
        self.assertEqual((problems, pinned), ([], {"v1.1.0": [".github/workflows/merge-gate.yml", ".github/workflows/sync.yml"]}))
        self.assertEqual((root / ".fleet/version").read_text(encoding="utf-8"), f"v1.0.0\n{release.FIXTURE_COMMIT}\n")
        text = (root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual({block.version for block in agents_md.blocks(text)}, {"v1.0.0"})
        self.assertEqual(len(agents_md.blocks(text)), len(agents_md.ORDER))
        for target, vendored in (vendor.shipped_at(VERSION) or {}).items():
            with self.subTest(target=target):
                self.assertIn("vendored from fleet v1.0.0 ", (root / target).read_text(encoding="utf-8"))
                self.assertEqual((root / target).stat().st_mode & 0o100 != 0, vendored.executable)
        self.assertEqual(git(root, "status", "--porcelain"), "")

    def test_the_spec_is_checked(self) -> None:
        self.assertEqual(sync_fixture.parse_spec("pins=v1.1.0 content=v1.0.0"), {"pins": "v1.1.0", "content": "v1.0.0"})
        for spec in ("pins=v1.1.0", "pins=1.1.0 content=v1.0.0", "pins=v1.1.0 content=v1.0.0 extra=x", "pins=$(x) content=v1.0.0"):
            with self.subTest(spec=spec), self.assertRaises(SystemExit):
                sync_fixture.parse_spec(spec)

    def test_expected_lists_what_a_sync_owns(self) -> None:
        expected = sync_fixture.expected()
        self.assertEqual(expected, sorted(expected))
        self.assertIn("AGENTS.md", expected)
        self.assertIn(".fleet/version", expected)
        self.assertIn("tools/fleet", expected)
        self.assertTrue(any(path.startswith(".claude/skills/fleet-") for path in expected))
        self.assertFalse(any(path.startswith(".github/") for path in expected))

    def test_the_drifted_fixture_drifts_and_passes_the_checks(self) -> None:
        problems, _ = drift.drift(DRIFTED)
        self.assertIn(f"AGENTS.md: the mistakes block differs from fleet's at {VERSION}; edit it in fleet "
                      "(agents-md/sections/mistakes.md), not here, or run `fleet sync`", problems,
                      "regenerate tests/fixtures/drifted at this VERSION: fleet's blocks with one line added inside mistakes")
        self.assertEqual((DRIFTED / ".fleet/version").read_text(encoding="utf-8"), f"{VERSION}\n{release.FIXTURE_COMMIT}\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["checks", "--root", str(DRIFTED)]), 0)
        self.assertFalse((DRIFTED / ".github").exists())


class SyncWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SYNC.read_text(encoding="utf-8")
        self.workflow = parsed(SYNC)
        self.steps = step_sources(self.text, "sync")

    def test_the_inputs_the_secret_and_the_outputs(self) -> None:
        call = mapping(mapping(self.workflow["on"])["workflow_call"])
        inputs = mapping(call["inputs"])
        self.assertEqual(sorted(inputs), ["app-id", "dry-run", "fixture", "fleet-ref"])
        self.assertEqual(mapping(inputs["dry-run"])["default"], "false")
        self.assertEqual(mapping(inputs["app-id"])["default"], "")
        secrets = mapping(call["secrets"])
        self.assertEqual(list(secrets), ["FLEET_BOT_PRIVATE_KEY"])
        self.assertEqual(mapping(secrets["FLEET_BOT_PRIVATE_KEY"])["required"], "false")
        self.assertEqual(sorted(call["outputs"]), ["action", "body", "changed", "title"])
        self.assertEqual(self.workflow["env"], {"FLEET_REF": self_check.expected_ref(cli.version())})
        self.assertEqual(self.workflow["permissions"], "{}")

    def test_the_job_holds_the_least_permissions_and_runs_hosted(self) -> None:
        job = mapping(mapping(self.workflow["jobs"])["sync"])
        self.assertEqual(job["permissions"], {"contents": "read", "issues": "write"})
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertIn("# hosted:", self.text.split("runs-on: ubuntu-latest", 1)[1].split("\n", 1)[0])
        self.assertEqual(mapping(job["concurrency"])["group"], "fleet-sync-${{ github.repository }}-${{ inputs.fixture }}")
        self.assertEqual(mapping(job["concurrency"])["cancel-in-progress"], "false")

    def test_the_concurrency_group_is_per_repository_not_per_ref(self) -> None:
        group = str(mapping(mapping(mapping(self.workflow["jobs"])["sync"])["concurrency"])["group"])
        self.assertNotIn("github.ref", group)
        self.assertIn("github.repository", group)
        self.assertIn("inputs.fixture", group)

    def test_every_checkout_keeps_no_credentials_and_is_pinned(self) -> None:
        for step in steps_of(self.workflow, "sync"):
            uses = step.get("uses")
            if isinstance(uses, str):
                self.assertRegex(uses, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$", uses)
            if isinstance(uses, str) and uses.startswith("actions/checkout@"):
                self.assertEqual(mapping(step["with"])["persist-credentials"], "false")

    def test_every_action_pin_carries_its_version(self) -> None:
        for line in self.text.splitlines():
            if line.strip().startswith(("uses: ", "- uses: ")):
                self.assertRegex(line, SHA_PIN, line)

    def test_the_private_key_is_read_by_the_token_step_alone(self) -> None:
        holders = [name for name, source in self.steps.items() if "secrets.FLEET_BOT_PRIVATE_KEY" in source]
        self.assertEqual(sorted(holders), ["The App is configured", "actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510   # v3.3.0"])
        guard = self.steps["The App is configured"]
        self.assertIn("HAS_KEY: ${{ secrets.FLEET_BOT_PRIVATE_KEY != '' }}", guard)
        self.assertNotIn("${{ secrets.FLEET_BOT_PRIVATE_KEY }}", guard)

    def test_the_token_is_narrowed_and_used_by_the_push_step_alone(self) -> None:
        token = next(source for name, source in self.steps.items() if name.startswith("actions/create-github-app-token@"))
        self.assertIn("permission-contents: write", token)
        self.assertIn("permission-pull-requests: write", token)
        self.assertEqual(sorted(re.findall(r"permission-[\w-]+", token)), ["permission-contents", "permission-pull-requests"])
        self.assertNotIn("skip-token-revoke", token)
        self.assertNotIn("owner:", token)
        users = [name for name, source in self.steps.items() if "steps.token.outputs" in source]
        self.assertEqual(users, ["Push the branch and open or update the pull request"])
        push = self.steps["Push the branch and open or update the pull request"]
        self.assertIn("GH_TOKEN: ${{ steps.token.outputs.token }}", push)

    def test_the_push_step_runs_only_for_a_real_sync_and_nothing_of_the_consumers(self) -> None:
        push = self.steps["Push the branch and open or update the pull request"]
        self.assertIn("if: steps.plan.outputs.action == 'sync' && !inputs.dry-run && inputs.fixture == ''", push)
        script = push.split("run: |\n", 1)[1]
        self.assertNotIn("${{", script)
        self.assertNotIn("merge", script)
        self.assertNotIn("python3 -E -s -m fleet_tools", script)
        self.assertNotIn("make ", script)
        self.assertIn('git push --force --quiet "https://github.com/$REPO" "HEAD:refs/heads/$BRANCH"', script)
        self.assertIn("GIT_CONFIG_KEY_0=http.https://github.com/.extraheader", script)
        self.assertNotIn("x-access-token:$GH_TOKEN@", script)
        self.assertIn('[[ "$existing" =~ ^[0-9]+$ ]]', script)
        self.assertIn('[[ "$bot_id" =~ ^[0-9]+$ ]]', script)
        self.assertIn("--input", script)
        for step in ("Open the migration issue unless one is open", "Push the branch and open or update the pull request", "actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510   # v3.3.0", "The App is configured"):
            self.assertIn("inputs.fixture == ''", self.steps[step], step)
            self.assertIn("!inputs.dry-run", self.steps[step], step)

    def test_the_sync_step_refuses_a_change_under_github_workflows(self) -> None:
        script = self.steps["Sync the content"].split("run: |\n", 1)[1]
        self.assertIn('git diff --cached --name-only -- .github/workflows', script)
        self.assertIn("The sync changed files under .github/workflows/", script)
        self.assertIn('fleet_tools.cli sync --to "$PIN"', script)
        self.assertIn("cd .fleet-pin", script)

    def test_no_expression_reaches_a_shell(self) -> None:
        for body in run_bodies(self.text):
            self.assertNotIn("${{", body)
        self.assertNotIn("secrets.", "\n".join(run_bodies(self.text)))

    def test_the_planner_runs_from_fleets_checkout_and_the_sync_from_a_clone_at_the_pin(self) -> None:
        plan = self.steps["Plan"].split("run: |\n", 1)[1]
        self.assertIn("cd .fleet || exit 1", plan)
        self.assertIn('fleet_tools.cli sync-plan --root "$GITHUB_WORKSPACE/app" | tee -a "$GITHUB_OUTPUT"', plan)
        fleet_checkouts = [source for source in self.steps.values() if "repository: AnthonyPluth/fleet" in source]
        self.assertEqual(len(fleet_checkouts), 1)
        self.assertIn("ref: ${{ inputs.fleet-ref || env.FLEET_REF }}", fleet_checkouts[0])
        pin = self.steps["fleet at the pins' version"]
        self.assertIn("if: steps.plan.outputs.action != 'none'", pin)
        self.assertIn("COMMIT: ${{ steps.plan.outputs.commit }}", pin)
        script = pin.split("run: |\n", 1)[1]
        self.assertIn('[[ "$PIN" =~ ^v[0-9]+\\.[0-9]+\\.[0-9]+$ ]]', script)
        self.assertIn('[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]]', script)
        self.assertIn('git clone --quiet --depth 1 --branch "$PIN" -- https://github.com/AnthonyPluth/fleet .fleet-pin', script)
        self.assertIn('head=$(git -C .fleet-pin rev-parse HEAD)', script)
        self.assertIn('if [ "$head" != "$COMMIT" ]; then', script)
        self.assertIn("cp -a .fleet .fleet-pin", script)
        self.assertNotIn("GH_TOKEN", pin)
        self.assertNotIn("secrets.", pin)

    def test_the_callers_default_branch_is_checked_out_before_fleet_and_the_fixture_replaces_it(self) -> None:
        names = list(self.steps)
        self.assertTrue(names[0].startswith("actions/checkout@") and names[0].endswith(" path: app"), names[0])
        self.assertTrue(names[1].startswith("actions/checkout@") and names[1].endswith(" path: .fleet"), names[1])
        app = self.steps[names[0]]
        self.assertIn("if: inputs.fixture == ''", app)
        self.assertIn("ref: ${{ github.event.repository.default_branch }}", app)
        self.assertNotIn("repository:", app)
        self.assertIn("if: inputs.fixture != ''", self.steps["Build the fixture consumer (self-test)"])
        self.assertLess(names.index(names[1]), names.index("Build the fixture consumer (self-test)"))
        self.assertLess(names.index("Build the fixture consumer (self-test)"), names.index("Plan"))


def script_of(chunk: str) -> str:
    if "run: |\n" in chunk:
        lines = []
        for line in chunk.split("run: |\n", 1)[1].splitlines():
            if line.strip() and not line.startswith("          "):
                break
            lines.append(line[10:])
        return "\n".join(lines)
    found = re.search(r"^ *run: (.+)$", chunk, re.MULTILINE)
    assert found, chunk
    return found.group(1)


def sparse_paths(chunk: str) -> list[str]:
    block = chunk.split("sparse-checkout: |\n", 1)[1]
    paths = []
    for line in block.splitlines():
        if not line.startswith("            "):
            break
        paths.append(line.strip())
    return paths


class FixtureMode(unittest.TestCase):
    NEEDED = ("fleet_tools", "agents-md", "plugins", "shim", "VERSION", "CHANGELOG.md", "tests/consumers.py", "tests/sync_fixture.py",
              "tests/fixtures/consumer-waypoint")

    def setUp(self) -> None:
        self.steps = step_sources(SYNC.read_text(encoding="utf-8"), "sync")
        self.fleet_checkout = next(source for source in self.steps.values() if "repository: AnthonyPluth/fleet" in source)

    def test_the_sparse_checkout_holds_what_the_planner_the_fixture_copy_and_the_body_need(self) -> None:
        paths = sparse_paths(self.fleet_checkout)
        self.assertLessEqual(set(self.NEEDED), set(paths))
        for path in paths:
            self.assertTrue((ROOT / path).exists(), path)
        body = script_of(self.steps["Write the pull request's or the issue's body"])
        self.assertIn('--changelog "$GITHUB_WORKSPACE/.fleet-pin/CHANGELOG.md"', body)

    def sparse_fleet(self, workspace: Path) -> Path:
        fleet = workspace / ".fleet"
        for path in sparse_paths(self.fleet_checkout):
            source, target = ROOT / path, fleet / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(source, target)
        return fleet

    def run_step(self, name: str, workspace: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        done = subprocess.run(["bash", "-eo", "pipefail", "-c", script_of(self.steps[name])], cwd=workspace, env=env,
                              capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, f"{name}\n{done.stdout}{done.stderr}")
        return done

    def outputs(self, env: dict[str, str]) -> dict[str, str]:
        return dict(line.split("=", 1) for line in Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8").splitlines() if "=" in line)

    def test_the_fixture_mode_steps_run_from_the_sparse_checkout_alone(self) -> None:
        for spec, action in (("pins=v1.1.0 content=v1.0.0", "sync"), ("pins=v2.0.0 content=v1.0.0", "migration"),
                             ("pins=v1.0.0 content=v1.0.0", "none")):
            with self.subTest(spec=spec):
                workspace = temporary(self)
                self.sparse_fleet(workspace)
                (workspace / "temp").mkdir()
                env = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "HOME": str(workspace), "GITHUB_WORKSPACE": str(workspace),
                       "RUNNER_TEMP": str(workspace / "temp"), "GITHUB_OUTPUT": str(workspace / "temp" / "output"), "SPEC": spec, "FIXTURE": spec}
                Path(env["GITHUB_OUTPUT"]).touch()
                self.run_step("Build the fixture consumer (self-test)", workspace, env)
                self.run_step("Plan", workspace, env)
                found = self.outputs(env)
                self.assertEqual(found["action"], action)
                if action == "none":
                    continue
                env["PIN"] = found["pin"]
                env["COMMIT"] = found["commit"]
                self.assertEqual(found["commit"], release.FIXTURE_COMMIT)
                self.run_step("fleet at the pins' version", workspace, env)
                self.assertEqual((workspace / ".fleet-pin/VERSION").read_text(encoding="utf-8").strip(), found["pin"][1:])
                self.run_step("Write the pull request's or the issue's body", workspace, env)
                body = (workspace / "temp/body.md").read_text(encoding="utf-8")
                self.assertIn(f"## CHANGELOG v1.0.0 → {found['pin']}", body)
                self.assertEqual(found["title"], sync_workflow.plan(workspace / "app").title)
                if action == "sync":
                    self.run_step("Sync the content", workspace, env)
                    changed = base64.b64decode(self.outputs(env)["changed"]).decode().split()
                    self.assertEqual(sorted(changed), sync_fixture.expected())
                    self.assertEqual((workspace / "app/.fleet/version").read_text(encoding="utf-8"),
                                     f"v1.1.0\n{release.FIXTURE_COMMIT}\n")

    def test_a_real_sync_refuses_a_tag_off_the_pins_commit(self) -> None:
        workspace = temporary(self)
        fleet = workspace / "upstream"
        write(fleet, "VERSION", "1.1.0\n")
        git(fleet, "init", "-q", "-b", "main")
        commit(fleet, "chore: fleet at the tag")
        git(fleet, "tag", "v1.1.0")
        script = script_of(self.steps["fleet at the pins' version"]).replace("https://github.com/AnthonyPluth/fleet", str(fleet))
        env = {"PATH": "/usr/bin:/bin", "HOME": str(workspace), "PIN": "v1.1.0", "FIXTURE": "", "COMMIT": release.FIXTURE_COMMIT}
        done = subprocess.run(["bash", "-eo", "pipefail", "-c", script], cwd=workspace, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(f"The tag v1.1.0 is at {git(fleet, 'rev-parse', 'HEAD').strip()}, but the workflow pins name {release.FIXTURE_COMMIT}",
                      done.stdout)
        shutil.rmtree(workspace / ".fleet-pin")
        env["COMMIT"] = git(fleet, "rev-parse", "HEAD").strip()
        done = subprocess.run(["bash", "-eo", "pipefail", "-c", script], cwd=workspace, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("fleet 1.1.0 in .fleet-pin", done.stdout)


class ChecksWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        self.text = CHECKS.read_text(encoding="utf-8")
        self.workflow = parsed(CHECKS)
        self.steps = step_sources(self.text, "checks")

    def test_the_inputs_and_the_permissions(self) -> None:
        call = mapping(mapping(self.workflow["on"])["workflow_call"])
        inputs = mapping(call["inputs"])
        self.assertEqual(sorted(inputs), ["expect-drift", "fleet-ref", "runs-on", "working-directory"])
        self.assertEqual(mapping(inputs["runs-on"])["default"], '"ubuntu-latest"')
        self.assertEqual(mapping(inputs["working-directory"])["default"], ".")
        self.assertEqual(mapping(inputs["expect-drift"])["default"], "false")
        self.assertNotIn("secrets", call)
        job = mapping(mapping(self.workflow["jobs"])["checks"])
        self.assertEqual(job["permissions"], {"contents": "read"})
        self.assertEqual(job["runs-on"], "${{ fromJSON(inputs.runs-on) }}")
        self.assertEqual(self.workflow["env"], {"FLEET_REF": self_check.expected_ref(cli.version())})
        self.assertEqual(self.workflow["permissions"], "{}")

    def test_the_consumer_is_checked_out_into_app_with_history_before_fleet_at_the_pinned_ref(self) -> None:
        steps = steps_of(self.workflow, "checks")
        first = mapping(steps[0]["with"])
        self.assertEqual((first["path"], first["fetch-depth"], first["persist-credentials"]), ("app", "0", "false"))
        self.assertNotIn("repository", first)
        self.assertNotIn("ref", first)
        fleet = next(step for step in steps if "repository" in mapping(step.get("with", {})))
        inputs = mapping(fleet["with"])
        self.assertEqual(inputs["ref"], "${{ inputs.fleet-ref || env.FLEET_REF }}")
        self.assertEqual((inputs["path"], inputs["fetch-depth"], inputs["fetch-tags"], inputs["persist-credentials"]), (".fleet", "0", "true", "false"))
        self.assertLess(steps.index(fleet), [step_name(step) for step in steps].index("fleet checks"))
        self.assertEqual([step_name(step) for step in steps if not str(step.get("uses", "")).startswith("actions/checkout@")],
                         ["fleet checks", "fleet drift"])
        for step in steps:
            if isinstance(step.get("uses"), str):
                self.assertRegex(str(step["uses"]), r"@[0-9a-f]{40}$")
        self.assertNotIn(".fleet/version", self.text.split("    steps:\n", 1)[1])

    def test_the_tools_run_against_the_consumer_in_app(self) -> None:
        for name in ("fleet checks", "fleet drift"):
            script = self.steps[name].split("run: |\n", 1)[1]
            self.assertIn('root=$(cd "app/$DIR" && pwd)', script)
            self.assertIn("cd .fleet || exit 1", script)

    def workspace(self, working_directory: str) -> tuple[Path, dict[str, str]]:
        root = temporary(self)
        app = root / "app"
        if working_directory == ".":
            copy_to = app
        else:
            copy_to = app / working_directory
            copy_to.parent.mkdir(parents=True)
        shutil.copytree(WAYPOINT, copy_to)
        git(app, "init", "-q", "-b", "main")
        commit(app, "chore: the consumer")
        (root / ".fleet").symlink_to(ROOT, target_is_directory=True)
        env = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "GITHUB_WORKSPACE": str(root), "DIR": working_directory,
               "EVENT": "push", "BASE": "", "HEAD": "", "EXPECT_DRIFT": "false", "HOME": str(root)}
        return root, env

    def run_step(self, name: str, root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", "-eo", "pipefail", "-c", script_of(self.steps[name])], cwd=root, env=env, capture_output=True,
                              text=True, check=False)

    def test_the_steps_pass_on_a_consumer_whose_fleet_version_is_at_its_root(self) -> None:
        for working_directory in (".", "apps/one"):
            with self.subTest(working_directory=working_directory):
                root, env = self.workspace(working_directory)
                version = root / "app" / working_directory / ".fleet/version"
                self.assertTrue(version.is_file())
                for name in ("fleet checks", "fleet drift"):
                    done = self.run_step(name, root, env)
                    self.assertEqual(done.returncode, 0, name + "\n" + done.stdout + done.stderr)
                self.assertIn("Fleet checks passed", self.run_step("fleet checks", root, env).stdout)
                self.assertIn("fleet drift: no drift", self.run_step("fleet drift", root, env).stdout)
                self.assertTrue(version.is_file())

    def test_expect_drift_passes_only_when_drift_fails(self) -> None:
        root, env = self.workspace(".")
        env["EXPECT_DRIFT"] = "true"
        self.assertEqual(self.run_step("fleet drift", root, env).returncode, 1)
        (root / "app/.fleet/version").unlink()
        done = self.run_step("fleet drift", root, env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("as it should", done.stdout)
        env["EXPECT_DRIFT"] = "false"
        self.assertEqual(self.run_step("fleet drift", root, env).returncode, 1)

    def test_the_commit_range_comes_from_the_event_and_is_checked(self) -> None:
        checks = self.steps["fleet checks"]
        self.assertIn("BASE: ${{ github.event.pull_request.base.sha }}", checks)
        self.assertIn("HEAD: ${{ github.event.pull_request.head.sha }}", checks)
        script = checks.split("run: |\n", 1)[1]
        self.assertIn('[[ "$BASE" =~ ^[0-9a-f]{40}$ && "$HEAD" =~ ^[0-9a-f]{40}$ ]]', script)
        self.assertIn('args=(--commits "$BASE..$HEAD" --pr-event "$GITHUB_EVENT_PATH")', script)
        self.assertIn('fleet_tools.cli checks --root "$root" "${args[@]}"', script)
        self.assertNotIn("github.event.pull_request.body", checks)

    def test_expect_drift_inverts_the_drift_step_only(self) -> None:
        script = self.steps["fleet drift"].split("run: |\n", 1)[1]
        self.assertIn('if [ "$EXPECT_DRIFT" = true ]; then', script)
        self.assertIn("fleet drift passed on the drifted fixture", script)
        self.assertNotIn("EXPECT_DRIFT", self.steps["fleet checks"])

    def test_no_expression_reaches_a_shell(self) -> None:
        for body in run_bodies(self.text):
            self.assertNotIn("${{", body)


def step_name(step: Node) -> str:
    return str(step.get("name") or step.get("uses") or "")


class SelfTest(unittest.TestCase):
    def setUp(self) -> None:
        self.jobs = mapping(parsed(SELF_TEST)["jobs"])

    def test_the_checks_run_on_a_clean_and_a_drifted_fixture(self) -> None:
        clean, drifted = mapping(self.jobs["checks-clean"]), mapping(self.jobs["checks-drifted"])
        self.assertEqual(clean["uses"], "./.github/workflows/checks.yml")
        self.assertEqual(mapping(clean["with"])["working-directory"], "tests/fixtures/consumer-waypoint")
        self.assertNotIn("expect-drift", mapping(clean["with"]))
        self.assertEqual(mapping(drifted["with"])["working-directory"], "tests/fixtures/drifted")
        self.assertEqual(mapping(drifted["with"])["expect-drift"], "true")
        for job in (clean, drifted):
            self.assertEqual(job["permissions"], {"contents": "read"})

    def test_the_sync_cases_differ_by_fixture_and_a_verdict_job_checks_them(self) -> None:
        cases = {name: mapping(job) for name, job in self.jobs.items() if name.startswith("sync-") and name != "sync-verdicts"}
        self.assertEqual(sorted(cases), ["sync-equal", "sync-major", "sync-minor"])
        fixtures = {name: mapping(job["with"])["fixture"] for name, job in cases.items()}
        self.assertEqual(fixtures, {"sync-minor": "pins=v1.1.0 content=v1.0.0", "sync-equal": "pins=v1.0.0 content=v1.0.0",
                                    "sync-major": "pins=v2.0.0 content=v1.0.0"})
        for job in cases.values():
            self.assertEqual(job["uses"], "./.github/workflows/sync.yml")
            self.assertNotIn("app-id", mapping(job["with"]))
            self.assertNotIn("secrets", job)
        verdicts = mapping(self.jobs["sync-verdicts"])
        self.assertEqual(verdicts["needs"], "[sync-minor, sync-equal, sync-major]")
        source = step_sources(SELF_TEST.read_text(encoding="utf-8"), "sync-verdicts")["Each case reached the plan it should"]
        for phrase in ("tests/sync_fixture.py --expected", "chore: fleet content v1.1.0", "fleet v2 needs a migration: see CHANGELOG",
                       "^\\.github/workflows/", "another major version"):
            self.assertIn(phrase, source)


if __name__ == "__main__":
    unittest.main()
