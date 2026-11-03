import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from fleet_tools import agents_md, cli, self_check

WORKFLOW_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "workflows"


def stamps(root: Path, version: str) -> None:
    released = "" if version.startswith("0.") else f"## [{version}] - 2026-11-02\n\n- A change.\n"
    (root / "CHANGELOG.md").write_text(f"# Changelog\n\n## [Unreleased]\n\n{released}", encoding="utf-8")
    (root / "pyproject.toml").write_text(f'[project]\nname = "fleet"\nversion = "{version}"\n', encoding="utf-8")
    plugin = root / "plugins" / "fleet" / ".claude-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(f'{{"name": "fleet", "version": "{version}"}}\n', encoding="utf-8")
    (root / "AGENTS.md").write_text(agents_md.sync("# Agent guide\n", agents_md.sections(), f"v{version}")[0], encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "adopting.md").write_text(f"# Adopting fleet\n\n```sh\ntools/fleet sync --to v{version}\n```\n", encoding="utf-8")


class Repo:
    def __init__(self, test: unittest.TestCase, version: str = "0.1.0") -> None:
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workflows = self.root / ".github" / "workflows"
        self.workflows.mkdir(parents=True)
        (self.root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
        stamps(self.root, version)
        self.add("self-test-ok.yml", "self-test.yml")

    def add(self, fixture: str, name: str | None = None) -> None:
        shutil.copy(WORKFLOW_FIXTURES / fixture, self.workflows / (name or fixture))


class FleetItselfTest(unittest.TestCase):
    def test_fleets_own_workflows_pass(self) -> None:
        self.assertEqual(self_check.check(cli.ROOT), [])


class FleetRefTest(unittest.TestCase):
    def test_a_stamped_reusable_workflow_passes(self) -> None:
        repo = Repo(self)
        repo.add("reusable-ok.yml", "reusable.yml")
        repo.add("plain.yml")
        self.assertEqual(self_check.check(repo.root), [])

    def test_a_reusable_workflow_without_fleet_ref_fails(self) -> None:
        repo = Repo(self)
        repo.add("reusable-no-fleet-ref.yml")
        errors = self_check.check(repo.root)
        self.assertEqual(len(errors), 1)
        self.assertIn(".github/workflows/reusable-no-fleet-ref.yml", errors[0])
        self.assertIn("FLEET_REF", errors[0])

    def test_a_fleet_ref_that_disagrees_with_version_fails(self) -> None:
        repo = Repo(self)
        repo.add("reusable-wrong-ref.yml")
        errors = self_check.check(repo.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("'v0.0.9'", errors[0])
        self.assertIn("'main'", errors[0])

    def test_main_while_version_is_1_x_fails(self) -> None:
        repo = Repo(self, version="1.2.0")
        repo.add("reusable-ok.yml")
        errors = self_check.check(repo.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("'v1.2.0'", errors[0])

    def test_a_tag_matching_version_passes(self) -> None:
        repo = Repo(self, version="1.2.0")
        repo.add("reusable-tagged.yml")
        self.assertEqual(self_check.check(repo.root), [])

    def test_a_tag_while_version_is_0_x_fails(self) -> None:
        repo = Repo(self)
        repo.add("reusable-tagged.yml", "reusable-tagged.yaml")
        errors = self_check.check(repo.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("reusable-tagged.yaml", errors[0])

    def test_a_fleet_checkout_at_any_other_ref_fails(self) -> None:
        repo = Repo(self)
        repo.add("reusable-fleet-at-another-ref.yml")
        errors = self_check.check(repo.root)
        self.assertEqual(len(errors), 2)
        self.assertIn("checks out AnthonyPluth/fleet at '${{ steps.version.outputs.ref }}', not `${{ inputs.fleet-ref || env.FLEET_REF }}`", errors[0])
        self.assertIn("checks out AnthonyPluth/fleet at None", errors[1])
        self.assertTrue(all(".github/workflows/reusable-fleet-at-another-ref.yml: job run" in error for error in errors))

    def test_a_workflow_that_isnt_reusable_needs_no_fleet_ref(self) -> None:
        repo = Repo(self)
        repo.add("plain.yml")
        self.assertEqual(self_check.check(repo.root), [])

    def test_version_must_exist_and_be_a_version(self) -> None:
        repo = Repo(self)
        (repo.root / "VERSION").write_text("one\n", encoding="utf-8")
        self.assertIn("isn't a version", self_check.check(repo.root)[0])
        (repo.root / "VERSION").unlink()
        self.assertEqual(self_check.check(repo.root), ["VERSION: missing"])

    def test_expected_ref(self) -> None:
        self.assertEqual(self_check.expected_ref("0.1.0"), "main")
        self.assertEqual(self_check.expected_ref("0.12.3"), "main")
        self.assertEqual(self_check.expected_ref("1.0.0"), "v1.0.0")
        self.assertEqual(self_check.expected_ref("10.0.1"), "v10.0.1")


class EventNamesTest(unittest.TestCase):
    def test_a_doc_or_workflow_naming_the_review_thread_webhook_fails(self) -> None:
        for path in ("docs/adopting.md", "README.md", ".github/workflows/merge-gate-threads.yml",
                     "plugins/fleet/skills/x/SKILL.md"):
            with self.subTest(path=path):
                repo = Repo(self)
                (repo.root / path).parent.mkdir(parents=True, exist_ok=True)
                (repo.root / path).write_text("on:\n  pull_request_review_thread:\n    types: [resolved]\n", encoding="utf-8")
                self.assertEqual(self_check.check(repo.root), [f"{path}: names `pull_request_review_thread`, a webhook "
                                                               "GitHub Actions can't subscribe to (a workflow naming it "
                                                               "is invalid and never runs)"])

    def test_the_events_actions_has_pass(self) -> None:
        repo = Repo(self)
        (repo.root / "docs" / "adopting.md").write_text(
            "# Adopting fleet\n\npull_request_review and pull_request_review_comment start it.\n", encoding="utf-8")
        self.assertEqual(self_check.check(repo.root), [])


class SelfTestCallsTest(unittest.TestCase):
    def test_calls_without_the_head_commit_as_fleet_ref_fail(self) -> None:
        repo = Repo(self)
        repo.add("self-test-no-fleet-ref.yml", "self-test.yml")
        errors = self_check.check(repo.root)
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("fleet-ref: ${{ github.sha }}" in error for error in errors))
        self.assertIn("job reusable", errors[0])
        self.assertIn("job pinned", errors[1])

    def test_a_missing_self_test_fails(self) -> None:
        repo = Repo(self)
        (repo.workflows / "self-test.yml").unlink()
        self.assertEqual(self_check.check(repo.root), [".github/workflows/self-test.yml: missing"])

    def test_a_self_test_without_jobs_fails(self) -> None:
        repo = Repo(self)
        (repo.workflows / "self-test.yml").write_text("name: Self-test\non: pull_request\n", encoding="utf-8")
        self.assertEqual(self_check.check(repo.root), [".github/workflows/self-test.yml: no jobs"])


class CommandTest(unittest.TestCase):
    def test_exit_codes_and_output(self) -> None:
        repo = Repo(self)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["self-check", "--root", str(repo.root)]), 0)
        self.assertEqual(out.getvalue(), "fleet self-check: ok\n")
        repo.add("reusable-no-fleet-ref.yml")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["self-check", "--root", str(repo.root)]), 1)
        self.assertIn("fleet self-check: .github/workflows/reusable-no-fleet-ref.yml", err.getvalue())


class ParseTest(unittest.TestCase):
    def test_the_workflow_subset(self) -> None:
        workflow = self_check.parse((WORKFLOW_FIXTURES / "reusable-ok.yml").read_text(encoding="utf-8"))
        assert isinstance(workflow, dict)
        self.assertEqual(workflow["env"], {"FLEET_REF": "main"})
        self.assertEqual(workflow["permissions"], "{}")
        trigger = workflow["on"]
        assert isinstance(trigger, dict)
        call = trigger["workflow_call"]
        assert isinstance(call, dict)
        self.assertIn("fleet-ref", call["inputs"])
        jobs = workflow["jobs"]
        assert isinstance(jobs, dict)
        run = jobs["run"]
        assert isinstance(run, dict)
        steps = run["steps"]
        assert isinstance(steps, list)
        self.assertEqual(len(steps), 2)
        first, second = steps
        assert isinstance(first, dict) and isinstance(second, dict)
        self.assertEqual(first["uses"], "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1")
        self.assertEqual(first["with"], {
            "repository": "AnthonyPluth/fleet",
            "ref": "${{ inputs.fleet-ref || env.FLEET_REF }}",
            "path": ".fleet",
            "sparse-checkout": "fleet_tools",
            "persist-credentials": "false",
        })
        self.assertEqual(second, {"name": "Say so", "run": ""})

    def test_scalars_quotes_and_comments(self) -> None:
        workflow = self_check.parse(
            "---\n"
            "# a comment\n"
            "a: 'x # not a comment'\n"
            "b: \"y\"   # a comment\n"
            "c: don't # trailing\n"
            "list:\n"
            "- one\n"
            "- 'two'\n"
            "-\n"
            "  nested: yes\n"
            "folded: >\n"
            "  text: inside\n"
            "after: here\n"
        )
        self.assertEqual(workflow, {
            "a": "x # not a comment",
            "b": "y",
            "c": "don't",
            "list": ["one", "two", {"nested": "yes"}],
            "folded": "",
            "after": "here",
        })

    def test_triggers(self) -> None:
        self.assertTrue(self_check.is_reusable(self_check.parse("on: workflow_call\n")))
        self.assertTrue(self_check.is_reusable(self_check.parse("on: [push, workflow_call]\n")))
        self.assertTrue(self_check.is_reusable(self_check.parse("on:\n  - workflow_call\n")))
        self.assertTrue(self_check.is_reusable(self_check.parse("'on':\n  workflow_call:\n")))
        self.assertFalse(self_check.is_reusable(self_check.parse("on:\n  push:\n")))
        self.assertFalse(self_check.is_reusable(self_check.parse("- workflow_call\n")))
        self.assertEqual(self_check.parse(""), {})

    def test_a_block_scalar_is_marked_not_read(self) -> None:
        tree = self_check.parse("a: |\n  x\nb: >-\n  y\nc: ''\nd:\n")
        self.assertEqual(tree, {"a": "", "b": "", "c": "", "d": ""})
        assert isinstance(tree, dict)
        self.assertEqual([isinstance(tree[key], self_check.BlockScalar) for key in "abcd"], [True, True, False, False])

    def test_where_names_the_line_of_each_key_by_its_path(self) -> None:
        where: self_check.Where = {}
        self_check.parse("jobs:\n  with:\n    runs-on: x\n  b:\n    steps:\n      - run: y\n        with:\n          runs-on: z\n", where)
        self.assertEqual(where[("jobs", "with", "runs-on")], 2)
        self.assertEqual(where[("jobs", "b", "steps", 0, "with", "runs-on")], 7)

    def test_flow_reads_collections_and_refuses_what_it_cannot(self) -> None:
        self.assertEqual(self_check.flow("{a, 'b': [c, \"d,e\"], f: {g: 1}, h: }"),
                         {"a": "", "b": ["c", "d,e"], "f": {"g": "1"}, "h": ""})
        self.assertEqual(self_check.flow("[a, b: c]"), None)
        for refused in ("{a: b", "{a: b}}", "[a,,b]", "{: a}", "[a", "{'a: b}", "{{a: b}: c}", "{a b c d}x"):
            with self.subTest(refused=refused):
                self.assertIsNone(self_check.flow(refused))


if __name__ == "__main__":
    unittest.main()
