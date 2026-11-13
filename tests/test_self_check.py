import contextlib
import io
import shutil
import subprocess
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


def ok_text() -> str:
    return (WORKFLOW_FIXTURES / "reusable-ok.yml").read_text(encoding="utf-8")


def guard_lines() -> str:
    text = ok_text()
    start = text.index("      - name: Check fleet's commit\n")
    return text[start:text.index("      - uses: actions/checkout@", start)]


def moved_after_checkout(text: str, guard: str) -> str:
    text = text.replace(guard, "")
    return text.replace("      - name: Say so\n", guard + "      - name: Say so\n")


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
        self.assertIn("checks out AnthonyPluth/fleet at '${{ steps.version.outputs.ref }}', not `${{ inputs.fleet-ref || job.workflow_sha }}`", errors[0])
        self.assertIn("checks out AnthonyPluth/fleet at None", errors[1])
        self.assertTrue(all(".github/workflows/reusable-fleet-at-another-ref.yml: job run" in error for error in errors))

    def test_a_fleet_checkout_without_the_guard_step_fails(self) -> None:
        guard = guard_lines()
        for case, text in (("missing", ok_text().replace(guard, "")),
                           ("after the checkout", moved_after_checkout(ok_text(), guard)),
                           ("another run", ok_text().replace("exit 1; }", "true; }")),
                           ("a condition", ok_text().replace("      - name: Check fleet's commit\n",
                                                             "      - name: Check fleet's commit\n        if: false\n")),
                           ("another env value", ok_text().replace("WORKFLOW_SHA: ${{ job.workflow_sha }}",
                                                                   "WORKFLOW_SHA: ${{ github.workflow_sha }}")),
                           ("another name", ok_text().replace("name: Check fleet's commit", "name: Check fleet"))):
            with self.subTest(case=case):
                repo = Repo(self)
                (repo.workflows / "reusable.yml").write_text(text, encoding="utf-8")
                errors = self_check.check(repo.root)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("job run checks out AnthonyPluth/fleet with no `Check fleet's commit` step before it", errors[0])

    def test_one_guard_step_covers_the_later_fleet_checkouts_of_its_job_only(self) -> None:
        again = ("      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1   # v7.0.1\n"
                 "        with:\n"
                 "          repository: AnthonyPluth/fleet\n"
                 "          ref: ${{ inputs.fleet-ref || job.workflow_sha }}\n"
                 "          path: .fleet\n"
                 "          persist-credentials: false\n")
        repo = Repo(self)
        (repo.workflows / "reusable.yml").write_text(ok_text() + again, encoding="utf-8")
        self.assertEqual(self_check.check(repo.root), [])
        (repo.workflows / "reusable.yml").write_text(
            ok_text() + "  other:\n    runs-on: ubuntu-latest\n    steps:\n" + again, encoding="utf-8")
        errors = self_check.check(repo.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("job other checks out AnthonyPluth/fleet with no `Check fleet's commit` step", errors[0])

    def test_reading_fleet_ref_fails_and_naming_it_in_a_comment_passes(self) -> None:
        repo = Repo(self)
        text = ok_text().replace("      - name: Say so\n",
                                 "      - name: Read it\n"
                                 "        env:\n"
                                 "          REF: ${{ env.FLEET_REF }}   # env.FLEET_REF in a comment is fine\n"
                                 "        run: |\n"
                                 "          # $FLEET_REF in a comment is fine\n"
                                 "          echo \"${FLEET_REF}\"\n"
                                 "      - name: Say so\n")
        (repo.workflows / "reusable.yml").write_text(text, encoding="utf-8")
        errors = self_check.check(repo.root)
        lines = text.splitlines()
        env_line = lines.index("          REF: ${{ env.FLEET_REF }}   # env.FLEET_REF in a comment is fine") + 1
        run_line = lines.index('          echo "${FLEET_REF}"') + 1
        self.assertEqual(errors, [
            f".github/workflows/reusable.yml:{env_line}: reads FLEET_REF (REF: ${{{{ env.FLEET_REF }}}}), the release's "
            "stamp only: fleet's files come from `${{ inputs.fleet-ref || job.workflow_sha }}`",
            f".github/workflows/reusable.yml:{run_line}: reads FLEET_REF (echo \"${{FLEET_REF}}\"), the release's stamp "
            "only: fleet's files come from `${{ inputs.fleet-ref || job.workflow_sha }}`"])

    def test_fleets_own_fleet_checkouts_are_at_the_workflows_own_commit(self) -> None:
        checkouts = 0
        for path in sorted((cli.ROOT / ".github" / "workflows").glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("env.FLEET_REF", text, path.name)
            checkouts += text.count("ref: ${{ inputs.fleet-ref || job.workflow_sha }}")
        self.assertGreater(checkouts, 0)

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


class GuardScriptTest(unittest.TestCase):
    def run_guard(self, fleet_ref: str, workflow_sha: str) -> subprocess.CompletedProcess[str]:
        if shutil.which("bash") is None:
            self.skipTest("bash isn't installed")
        return subprocess.run(["bash", "-eo", "pipefail", "-c", self_check.GUARD_RUN], capture_output=True, text=True,
                              check=False, env={"FLEET_REF_INPUT": fleet_ref, "WORKFLOW_SHA": workflow_sha})

    def test_a_commit_in_job_workflow_sha_passes_and_names_it(self) -> None:
        done = self.run_guard("", "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual((done.returncode, done.stdout), (0, "fleet at 0123456789abcdef0123456789abcdef01234567\n"))

    def test_fleet_ref_wins_whatever_job_workflow_sha_holds(self) -> None:
        for sha in ("", "0123456789abcdef0123456789abcdef01234567"):
            with self.subTest(sha=sha):
                done = self.run_guard("abcdef", sha)
                self.assertEqual((done.returncode, done.stdout), (0, "fleet at abcdef\n"))

    def test_no_commit_and_no_fleet_ref_fails_naming_the_runner(self) -> None:
        for sha in ("", "main", "0123456789abcdef", "0123456789ABCDEF0123456789ABCDEF01234567",
                    "0123456789abcdef0123456789abcdef01234567\n"):
            with self.subTest(sha=sha):
                done = self.run_guard("", sha)
                self.assertEqual(done.returncode, 1)
                self.assertIn("::error::job.workflow_sha is", done.stdout)
                self.assertIn("this runner doesn't report the commit of the workflow it runs", done.stdout)
                self.assertNotIn("fleet at", done.stdout)


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


class UnreadableTest(unittest.TestCase):
    def test_a_workflow_outside_the_yaml_subset_is_reported_not_skipped(self) -> None:
        repo = Repo(self)
        (repo.workflows / "two.yml").write_text("on: workflow_call\n---\nenv:\n  FLEET_REF: main\n", encoding="utf-8")
        self.assertEqual(self_check.check(repo.root), [f".github/workflows/two.yml: line 2: {self_check.DOCUMENT}"])

    def test_a_self_test_outside_the_yaml_subset_is_reported(self) -> None:
        repo = Repo(self)
        (repo.workflows / "self-test.yml").write_text("jobs:\n  a:\n    uses: ./.github/workflows/x.yml\n    with:\n"
                                                      "      fleet-ref: ${{ github.sha }\n        }\n", encoding="utf-8")
        self.assertEqual(self_check.check(repo.root), [f".github/workflows/self-test.yml: line 6: {self_check.CONTINUED}"])


class SelfTestCallsTest(unittest.TestCase):
    def test_calls_without_the_head_commit_as_fleet_ref_fail(self) -> None:
        repo = Repo(self)
        repo.add("self-test-no-fleet-ref.yml", "self-test.yml")
        errors = self_check.check(repo.root)
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("fleet-ref: ${{ github.sha }}" in error for error in errors))
        self.assertIn("job reusable", errors[0])
        self.assertIn("job pinned", errors[1])

    def test_the_own_commit_call_must_pass_no_fleet_ref(self) -> None:
        repo = Repo(self)
        text = (WORKFLOW_FIXTURES / "self-test-ok.yml").read_text(encoding="utf-8")
        own = "  checks-own-commit:\n    uses: ./.github/workflows/reusable.yml\n    with:\n"
        (repo.workflows / "self-test.yml").write_text(text.replace(own, own + "      fleet-ref: ${{ github.sha }}\n"),
                                                     encoding="utf-8")
        self.assertEqual(self_check.check(repo.root), [
            ".github/workflows/self-test.yml: job checks-own-commit passes fleet-ref; it is the one call without it, which "
            "runs ./.github/workflows/reusable.yml at its own commit (job.workflow_sha)"])

    def test_a_self_test_without_the_own_commit_call_fails(self) -> None:
        repo = Repo(self)
        text = (WORKFLOW_FIXTURES / "self-test-ok.yml").read_text(encoding="utf-8")
        for case, changed in (("missing", text.replace("checks-own-commit:", "checks-at-head:")),
                              ("not a local call", text.replace("  checks-own-commit:\n    uses: ./.github/workflows/",
                                                                "  checks-own-commit:\n    uses: AnthonyPluth/fleet/.github/workflows/"))):
            with self.subTest(case=case):
                (repo.workflows / "self-test.yml").write_text(changed, encoding="utf-8")
                errors = self_check.check(repo.root)
                self.assertIn(".github/workflows/self-test.yml: no job checks-own-commit calling a reusable workflow "
                              "without fleet-ref, so nothing runs one at its own commit (job.workflow_sha)", errors)

    def test_a_missing_self_test_fails(self) -> None:
        repo = Repo(self)
        (repo.workflows / "self-test.yml").unlink()
        self.assertEqual(self_check.check(repo.root), [".github/workflows/self-test.yml: missing"])

    def test_a_self_test_without_jobs_fails(self) -> None:
        repo = Repo(self)
        (repo.workflows / "self-test.yml").write_text("name: Self-test\non: pull_request\n", encoding="utf-8")
        self.assertEqual(self_check.check(repo.root), [".github/workflows/self-test.yml: no jobs"])


def review_text() -> str:
    return (WORKFLOW_FIXTURES / "agent-review-environment-ok.yml").read_text(encoding="utf-8")


REVIEW_ENVIRONMENT_LINE = ("    environment: ${{ github.event.pull_request.head.repo.full_name == github.repository && "
                           "inputs.environment || '' }}\n")
SECOND_ENVIRONMENT_LINE = ("    environment: ${{github.event.pull_request.head.repo.full_name == github.repository && "
                           "inputs.environment || ''}}\n")


class ReviewEnvironmentTest(unittest.TestCase):
    def errors_for(self, text: str, name: str = "agent-review.yml") -> list[str]:
        repo = Repo(self)
        (repo.workflows / name).write_text(text, encoding="utf-8")
        return self_check.check(repo.root)

    def test_the_review_jobs_in_the_caller_s_environment_pass(self) -> None:
        self.assertIn(REVIEW_ENVIRONMENT_LINE, review_text())
        self.assertIn(SECOND_ENVIRONMENT_LINE, review_text())
        self.assertEqual(self.errors_for(review_text()), [])

    def test_the_rule_is_the_agent_review_s_only(self) -> None:
        text = review_text().replace(REVIEW_ENVIRONMENT_LINE, "    environment: production\n")
        self.assertEqual(self.errors_for(text, "deploy.yml"), [])

    def test_a_review_job_outside_the_environment_or_in_another_one_fails(self) -> None:
        wanted = ("job {} needs `environment: ${{{{ github.event.pull_request.head.repo.full_name == github.repository && "
                  "inputs.environment || '' }}}}`: the caller's environment, for a pull request from this repository's "
                  "branches only, and none when the input is empty")
        cases = (
            ("no environment", "review", REVIEW_ENVIRONMENT_LINE, ""),
            ("the input unguarded", "second-review", SECOND_ENVIRONMENT_LINE, "    environment: ${{ inputs.environment }}\n"),
            ("a fixed name", "review", REVIEW_ENVIRONMENT_LINE, "    environment: agent-review\n"),
            ("the guard reversed", "review", REVIEW_ENVIRONMENT_LINE,
             REVIEW_ENVIRONMENT_LINE.replace("full_name == github.repository", "full_name != github.repository")),
            ("the object form", "second-review", SECOND_ENVIRONMENT_LINE,
             "    environment:\n      name: ${{ inputs.environment }}\n"),
        )
        for case, job, old, new in cases:
            with self.subTest(case=case):
                errors = self.errors_for(review_text().replace(old, new))
                self.assertEqual(errors[0], f".github/workflows/agent-review.yml: {wanted.format(job)}")
                if "inputs.environment" in new:
                    self.assertEqual(len(errors), 2, errors)
                    self.assertIn(": reads inputs.environment (", errors[1])
                else:
                    self.assertEqual(len(errors), 1, errors)

    def test_another_job_naming_an_environment_fails(self) -> None:
        text = review_text().replace("  report:\n    needs: review\n",
                                     "  report:\n    needs: review\n    environment: agent-review\n")
        self.assertEqual(self.errors_for(text), [
            ".github/workflows/agent-review.yml: job report names an environment; only review and second-review, the "
            "jobs that read the review secrets, run in one"])

    def test_reading_the_input_anywhere_else_fails_and_naming_it_in_a_comment_passes(self) -> None:
        text = review_text().replace("        run: echo \"report\"\n",
                                     "        env:\n"
                                     "          NAME: ${{ inputs['environment'] }}   # inputs.environment\n"
                                     "        run: echo \"${{ inputs.environment }}\"\n")
        lines = text.splitlines()
        env_line = lines.index("          NAME: ${{ inputs['environment'] }}   # inputs.environment") + 1
        run_line = lines.index("        run: echo \"${{ inputs.environment }}\"") + 1
        tail = "; it reaches the `environment:` of review and second-review only, never a script or another key"
        self.assertEqual(self.errors_for(text), [
            f".github/workflows/agent-review.yml:{env_line}: reads inputs.environment (NAME: ${{{{ inputs['environment'] "
            f"}}}}   # inputs.environment){tail}",
            f".github/workflows/agent-review.yml:{run_line}: reads inputs.environment (run: echo \"${{{{ inputs.environment "
            f"}}}}\"){tail}"])

    def test_the_input_must_default_to_empty_and_the_review_jobs_must_exist(self) -> None:
        wanted = (".github/workflows/agent-review.yml: needs the input `environment` as a string defaulting to \"\", so a "
                  "caller that passes none keeps its review jobs out of any environment")
        for case, text in (("a default", review_text().replace('        default: ""\n', "        default: agent-review\n")),
                           ("a boolean", review_text().replace("        type: string\n", "        type: boolean\n")),
                           ("missing", review_text().replace("      environment:\n        description: The caller's "
                                                             "environment for the review secrets.\n        type: string\n"
                                                             '        default: ""\n', "      other:\n        type: string\n"))):
            with self.subTest(case=case):
                self.assertEqual(self.errors_for(text), [wanted])
        gone = review_text().split("  second-review:\n", 1)[0]
        self.assertEqual(self.errors_for(gone), [
            ".github/workflows/agent-review.yml: no job second-review, which runs in the caller's environment"])

    def test_fleets_own_review_passes_and_its_input_reaches_nothing_else(self) -> None:
        text = (cli.ROOT / ".github" / "workflows" / "agent-review.yml").read_text(encoding="utf-8")
        self.assertEqual(self_check.check_review_environment(".github/workflows/agent-review.yml", text), [])
        self.assertEqual(text.count("inputs.environment"), 2)
        self.assertEqual(text.count(REVIEW_ENVIRONMENT_LINE), 2)


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
        self.assertEqual(len(steps), 3)
        guard, first, second = steps
        assert isinstance(guard, dict) and isinstance(first, dict) and isinstance(second, dict)
        self.assertEqual(guard, {"name": "Check fleet's commit",
                                 "env": {"FLEET_REF_INPUT": "${{ inputs.fleet-ref }}", "WORKFLOW_SHA": "${{ job.workflow_sha }}"},
                                 "run": self_check.GUARD_RUN})
        self.assertEqual(first["uses"], "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1")
        self.assertEqual(first["with"], {
            "repository": "AnthonyPluth/fleet",
            "ref": "${{ inputs.fleet-ref || job.workflow_sha }}",
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

    def assert_unreadable(self, text: str, line: int, reason: str) -> None:
        with self.subTest(text=text):
            with self.assertRaises(self_check.Unreadable) as raised:
                self_check.parse(text)
            self.assertEqual((raised.exception.number + 1, raised.exception.reason), (line, reason))

    def test_a_node_property_on_a_mapping_key_is_read_past(self) -> None:
        self.assertEqual(self_check.parse("&job-map jobs:\n  build:\n    runs-on: x\n"), {"jobs": {"build": {"runs-on": "x"}}})
        self.assertEqual(self_check.parse("on: push\n!!str jobs:\n  &b build:\n    - &s !!map run: x\n"),
                         {"on": "push", "jobs": {"build": [{"run": "x"}]}})

    def test_an_explicit_key_in_a_list_item_is_unreadable(self) -> None:
        for text in ("steps:\n  - ? a\n    : b\n", "steps:\n  - ?\n", "steps:\n  - &a ? b\n"):
            self.assert_unreadable(text, 2, self_check.ELSEWHERE)

    def test_a_mapping_line_without_a_colon_is_unreadable(self) -> None:
        for text in ("a: 1\nb\n", "jobs:\n  build\n", "a: 1\nkey:value\n", "l:\n  - : x\n"):
            self.assert_unreadable(text, 2, self_check.NO_KEY)
        self.assert_unreadable("a:\n  b: 1\n  c\n", 3, self_check.NO_KEY)

    def test_a_multi_line_plain_scalar_is_unreadable(self) -> None:
        self.assert_unreadable("a: one\n  two\n", 2, self_check.CONTINUED)
        self.assert_unreadable("a: one\ntwo\n", 2, self_check.NO_KEY)
        self.assert_unreadable("l:\n  - one\n    two\n", 3, self_check.CONTINUED)
        self.assert_unreadable("a: 'one\n  two'\n", 1, self_check.QUOTED)
        self.assert_unreadable('a: "one" two\n', 1, self_check.QUOTED)
        self.assert_unreadable("run: echo a: b\n", 1, self_check.PLAIN_COLON)
        self.assertEqual(self_check.parse("a: 'it''s: fine'\nb: x:y\n"), {"a": "it's: fine", "b": "x:y"})

    def test_an_escaped_quote_does_not_end_a_quoted_value(self) -> None:
        text = ("a: 'it''s # data'\nb: 'it''s # data'   # a comment\nc: \"say \\\"hi\\\" # x\"\n"
                "d: \"say \\\"hi\\\" # x\"  # a comment\ne: \"back\\\\\" # a comment\nf: ['it''s # x]']  # a comment\n")
        self.assertEqual(self_check.parse(text), {"a": "it's # data", "b": "it's # data", "c": 'say \\"hi\\" # x',
                                                  "d": 'say \\"hi\\" # x', "e": "back\\\\", "f": "['it''s # x]']"})
        for unclosed in ("a: 'it''s # data\n", "a: \"say \\\" # x\n", "a: \"back\\\\\" x\" # c\n"):
            self.assert_unreadable(unclosed, 1, self_check.QUOTED)

    def test_a_multi_line_flow_collection_is_unreadable(self) -> None:
        self.assert_unreadable("with: {a: b,\n  c: d}\n", 1, self_check.FLOW)
        self.assert_unreadable("needs: [a,\n  b]\n", 1, self_check.FLOW)
        self.assert_unreadable("steps:\n  - {run: x,\n     shell: bash}\n", 2, self_check.FLOW)
        self.assert_unreadable("needs: ['a,\n  b']\n", 1, self_check.FLOW)
        self.assertEqual(self_check.parse("needs: [a, 'b]']\nsteps:\n  - {run: x}\n"),
                         {"needs": "[a, 'b]']", "steps": ["{run: x}"]})

    def test_a_document_after_the_first_is_unreadable(self) -> None:
        for text, line in (("a: 1\n---\nb: 2\n", 2), ("---\na: 1\n...\n", 3), ("---\n---\na: 1\n", 2), ("--- a: 1\n", 1),
                           ("a: 1\n--- # second\njobs: {}\n", 2)):
            self.assert_unreadable(text, line, self_check.DOCUMENT)
        self.assert_unreadable("%YAML 1.2\n---\na: 1\n", 1, self_check.DIRECTIVE)
        self.assertEqual(self_check.parse("# a comment\n---\na: |\n  ---\n  ...\n"), {"a": ""})

    def test_other_shapes_outside_the_subset_are_unreadable(self) -> None:
        self.assert_unreadable("a:\n    b: 1\n  c: 2\n", 3, self_check.MISPLACED)
        self.assert_unreadable("l:\n  - a: 1\n   b: 2\n", 3, self_check.MISPLACED)
        self.assert_unreadable("a:\n  - x\n  b: y\n", 3, self_check.MISPLACED)
        self.assert_unreadable("a:\n\tb: 1\n", 2, self_check.TAB)
        self.assert_unreadable("jobs: x\njobs: y\n", 2, self_check.DUPLICATE)
        self.assert_unreadable("l:\n  - - a\n", 2, self_check.NESTED_LIST)
        self.assert_unreadable("a: |x\n  t\n", 1, self_check.BAD_HEADER)
        self.assert_unreadable("[a]: b\n", 1, self_check.COMPLEX_KEY)
        for text in ("a: *x\n", "<<: *x\n", "*x : 1\n", "- *x\n", "? a\n"):
            self.assert_unreadable(text, 1, self_check.ELSEWHERE)

    def test_a_block_scalar_list_item_is_marked_and_its_text_skipped(self) -> None:
        tree = self_check.parse("l:\n  - |\n    x: y\n    - z\n  - >-2\n    w\n  - v\n")
        self.assertEqual(tree, {"l": ["", "", "v"]})
        assert isinstance(tree, dict) and isinstance(tree["l"], list)
        self.assertEqual([isinstance(item, self_check.BlockScalar) for item in tree["l"]], [True, True, False])
        self.assertEqual(self_check.parse("-foo: bar\n"), {"-foo": "bar"})

    def test_flow_reads_collections_and_refuses_what_it_cannot(self) -> None:
        self.assertEqual(self_check.flow("{a, 'b': [c, \"d,e\"], f: {g: 1}, h: }"),
                         {"a": "", "b": ["c", "d,e"], "f": {"g": "1"}, "h": ""})
        self.assertEqual(self_check.flow("[a, b: c]"), None)
        for refused in ("{a: b", "{a: b}}", "[a,,b]", "{: a}", "[a", "{'a: b}", "{{a: b}: c}", "{a b c d}x"):
            with self.subTest(refused=refused):
                self.assertIsNone(self_check.flow(refused))


if __name__ == "__main__":
    unittest.main()
