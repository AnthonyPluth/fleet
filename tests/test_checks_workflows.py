import re
import unittest
from pathlib import Path

from consumers import RUNWAY, WAYPOINT, checks, copy, empty_consumer, ran, write

from fleet_tools.checks import workflows as wf

GOOD = ("name: X\non: push\ndefaults:\n  run:\n    shell: bash\njobs:\n  a:\n    " + wf.RUNS_ON + "\n    steps:\n"
        "      - uses: actions/checkout@" + "a" * 40 + "   # v7.0.1\n"
        "      - uses: ./.github/actions/local\n"
        "      - run: gh api --paginate \"repos/$R/pulls?per_page=100\"\n")


ROOT = Path(__file__).resolve().parent.parent
CALLER = ("name: X\non:\n  pull_request_review:\n    types: [submitted]\n  pull_request_review_comment:\n"
          "    types: [created]\npermissions: {}\njobs:\n  a:\n    permissions:\n      actions: write\n"
          "    uses: AnthonyPluth/fleet/.github/workflows/x.yml@" + "a" * 40 + "   # v1.1.0\n    with:\n"
          "      runs-on: ${{ vars.RUNS_ON || '\"ubuntu-latest\"' }}\n")


def snippets() -> list[tuple[str, str]]:
    found = []
    for doc in sorted([*(ROOT / "docs").glob("*.md"), ROOT / "README.md"]):
        for block in re.finditer(r"^```yaml\n(.*?)^```", doc.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL):
            if re.search(r"^jobs:", block.group(1), re.MULTILINE):
                found.append((doc.name, block.group(1).replace("@<commit>", "@" + "0" * 40)))
    return found


def job_env(expression: str) -> str:
    return GOOD.replace("  a:\n", "  a:\n    env:\n      X: " + expression + "\n")


class Workflow(unittest.TestCase):
    def test_a_workflow_following_the_rules_passes(self) -> None:
        self.assertEqual(wf.check_workflow("x.yml", GOOD), [])

    def test_no_bash_default_fails(self) -> None:
        text = GOOD.replace("defaults:\n  run:\n    shell: bash\n", "")
        self.assertIn("no top-level `defaults: run: shell: bash`", wf.check_workflow("x.yml", text)[0])

    def test_an_action_needs_no_bash_default(self) -> None:
        text = GOOD.replace("defaults:\n  run:\n    shell: bash\n", "")
        self.assertEqual(wf.check_workflow("action.yml", text, workflow=False), [])

    def test_an_unpinned_action_or_a_pin_without_its_version_fails(self) -> None:
        self.assertIn("isn't pinned", wf.check_workflow("x.yml", GOOD.replace("@" + "a" * 40, "@v7"))[0])
        self.assertIn("no version in a comment", wf.check_workflow("x.yml", GOOD.replace("   # v7.0.1", ""))[0])

    def test_a_docker_image_and_a_local_action_need_no_pin(self) -> None:
        text = GOOD.replace("./.github/actions/local", "docker://alpine:3")
        self.assertEqual(wf.check_workflow("x.yml", text), [])

    def test_a_list_read_without_paginate_fails(self) -> None:
        self.assertIn("without --paginate", wf.check_workflow("x.yml", GOOD.replace("--paginate ", ""))[0])

    def test_a_job_runs_on_runs_on_or_says_why_it_needs_githubs_runners(self) -> None:
        hosted = GOOD.replace(wf.RUNS_ON, "runs-on: ubuntu-latest   # hosted: publishes from GitHub's runners only")
        self.assertEqual(wf.check_workflow("x.yml", hosted), [])
        for bare in ("runs-on: ubuntu-latest", "runs-on: self-hosted", "runs-on: ${{ fromJSON(vars.RUNS_ON) }}",
                     "runs-on: ubuntu-latest   # hosted:"):
            with self.subTest(bare=bare):
                self.assertIn("should be fleet's RUNS_ON line", wf.check_workflow("x.yml", GOOD.replace(wf.RUNS_ON, bare))[0])

    def test_a_caller_with_no_run_steps_needs_no_bash_default(self) -> None:
        self.assertEqual(wf.check_workflow("x.yml", CALLER), [])
        for step in ("- run: |\n          true\n", '- "run": echo ok\n', "- {run: echo ok}\n", "- 'run': echo ok\n",
                     "- uses: ./.github/actions/local\n"):
            with self.subTest(step=step):
                with_steps = CALLER + "  b:\n    " + wf.RUNS_ON + "\n    steps:\n      " + step
                self.assertIn("no top-level `defaults: run: shell: bash`", wf.check_workflow("x.yml", with_steps)[0])
        for unreadable in ("name: X\non: push\n", "name: X\non: push\njobs: [a]\n", "name: X\non: push\njobs: {a: {uses: x}}\n"):
            with self.subTest(unreadable=unreadable):
                self.assertIn("no top-level `defaults: run: shell: bash`", wf.check_workflow("x.yml", unreadable)[0])

    def test_a_runs_on_input_quoted_or_not_is_an_input_not_a_jobs_runner(self) -> None:
        self.assertEqual(wf.check_workflow("x.yml", CALLER), [])
        quoted = CALLER.replace("      runs-on:", '      "runs-on":')
        self.assertEqual(wf.check_workflow("x.yml", quoted), [])
        job = CALLER.replace("    uses:", "    runs-on: ubuntu-latest\n    uses:")
        self.assertIn("should be fleet's RUNS_ON line", wf.check_workflow("x.yml", job)[0])

    def test_an_event_outside_the_ones_actions_has_fails(self) -> None:
        for bad in ("on:\n  pull_request_review_thread:\n    types: [resolved]\n", "on: [push, pull_request_review_thread]\n",
                    "on: pull_request_review_thread\n"):
            with self.subTest(bad=bad):
                text = re.sub(r"^on:\n(?:  .*\n)+", bad, CALLER, flags=re.MULTILINE)
                self.assertEqual(wf.check_workflow("x.yml", text), ["x.yml: `pull_request_review_thread` isn't a GitHub "
                                                                    "Actions event (the file would be invalid and never run)"])
        for fine in ("on: [push, pull_request]\n", "on: workflow_dispatch\n", '"on":\n  schedule:\n    - cron: "1 2 * * *"\n'):
            with self.subTest(fine=fine):
                text = re.sub(r"^on:\n(?:  .*\n)+", fine, CALLER, flags=re.MULTILINE)
                self.assertEqual(wf.check_workflow("x.yml", text), [])

    def test_fleets_documented_callers_pass(self) -> None:
        found = snippets()
        self.assertGreater(len(found), 10)
        for doc, text in found:
            with self.subTest(doc=doc, snippet=text.splitlines()[0]):
                self.assertEqual(wf.check_workflow("x.yml", text), [])

    def test_a_context_a_job_env_cannot_use_fails_but_a_steps_env_may(self) -> None:
        job = GOOD.replace("  a:\n", "  a:\n    env:\n      OUT: ${{ runner.temp }}/x\n")
        self.assertIn("`runner` isn't available in a workflow's or job's `env:`", wf.check_workflow("x.yml", job)[0])
        top = GOOD.replace("jobs:\n", "env:\n  A: ${{ github.sha }}\n  B: ${{ steps.s.outputs.o }}\njobs:\n")
        self.assertIn("`steps` isn't available", wf.check_workflow("x.yml", top)[0])
        ok_job = GOOD.replace("  a:\n", "  a:\n    env:\n      OUT: ${{ github.workspace }}/x\n      T: ${{ secrets.T }}\n")
        self.assertEqual(wf.check_workflow("x.yml", ok_job), [])
        step = GOOD.replace("      - run:", "      - env:\n          OUT: ${{ runner.temp }}/x\n        run:")
        self.assertEqual(wf.check_workflow("x.yml", step), [])

    def test_only_a_context_of_its_own_counts_not_a_property_with_the_same_name(self) -> None:
        for fine in ("${{ matrix.runner.os }}", "${{ needs.job.outputs.x }}", "${{ matrix.env.name }}", "${{ vars.steps.n }}"):
            with self.subTest(fine=fine):
                self.assertEqual(wf.check_workflow("x.yml", job_env(fine)), [])
        for bad, context in (("${{ runner.temp }}/x", "runner"), ("${{ steps.x.outputs.y }}", "steps"),
                             ("${{ job.status }}", "job"), ("${{ env.X }}", "env"),
                             ("${{ format('{0}/x', runner.temp) }}", "runner"),
                             ("${{ matrix.a && steps.s.outputs.o }}", "steps")):
            with self.subTest(bad=bad):
                self.assertIn(f"`{context}` isn't available", wf.check_workflow("x.yml", job_env(bad))[0])


class FixtureConsumers(unittest.TestCase):
    def test_both_consumers_pass(self) -> None:
        for fixture in (WAYPOINT, RUNWAY):
            with self.subTest(fixture=fixture.name):
                root = copy(self, fixture, repository=False)
                self.assertIn("workflows", ran(root))
                self.assertEqual(wf.check_workflows(root), [])

    def test_an_early_env_fires_in_waypoint(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        path = root / ".github/workflows/ci.yml"
        path.write_text(path.read_text().replace("    permissions:\n      contents: read\n",
                                                 "    permissions:\n      contents: read\n    env:\n"
                                                 "      OUT: ${{ runner.temp }}/out\n", 1))
        found = checks(root)
        self.assertEqual(len(found), 1)
        self.assertIn(".github/workflows/ci.yml: `runner` isn't available", found[0])

    def test_a_bare_runner_fires_in_runway(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        path = root / ".github/workflows/ci.yml"
        path.write_text(path.read_text().replace("   # hosted: Runway runs on GitHub's runners only", ""))
        self.assertEqual(checks(root), [".github/workflows/ci.yml: `runs-on: ubuntu-latest` should be fleet's RUNS_ON "
                                        "line (fleet_tools/checks/workflows.py), or `runs-on: ubuntu-latest   # hosted: "
                                        "<why it needs GitHub's runners>`"])

    def test_a_composite_action_is_checked_too(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        write(root, ".github/actions/setup/action.yml",
              "runs:\n  using: composite\n  steps:\n    - uses: actions/setup-python@v6\n")
        self.assertEqual(checks(root), [".github/actions/setup/action.yml: actions/setup-python@v6 isn't pinned by its "
                                        "commit SHA"])

    def test_silent_where_there_are_no_workflows(self) -> None:
        root = empty_consumer(self)
        self.assertNotIn("workflows", ran(root))
        self.assertEqual(checks(root), [])


if __name__ == "__main__":
    unittest.main()
