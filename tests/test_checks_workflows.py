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


def with_on(value: str) -> str:
    return re.sub(r"^on:\n(?:  .*\n)+", lambda _: value, CALLER, flags=re.MULTILINE)


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
                    "on: pull_request_review_thread\n", "on: {push: null, pull_request_review_thread: null}\n",
                    "on: {push: {branches: [main]}, 'pull_request_review_thread': {types: [resolved]}}\n"):
            with self.subTest(bad=bad):
                text = re.sub(r"^on:\n(?:  .*\n)+", bad, CALLER, flags=re.MULTILINE)
                self.assertEqual(wf.check_workflow("x.yml", text), ["x.yml: `pull_request_review_thread` isn't a GitHub "
                                                                    "Actions event (the file would be invalid and never run)"])
        for fine in ("on: [push, pull_request]\n", "on: workflow_dispatch\n", '"on":\n  schedule:\n    - cron: "1 2 * * *"\n',
                     "on: {push: null, workflow_dispatch: null}\n", "on: {push, pull_request}\n",
                     "on: {push, 'pull_request': {types: [opened]}, workflow_dispatch}\n",
                     'on: {"push": {branches: ["a,b", \'c}\']}, \'schedule\': [{cron: "1 2 * * *"}]}\n',
                     "on: image_version\n", "on:\n  image_version:\n    names: [X]\n",
                     'on: {push: {branches: [main]}, "schedule": [{cron: "1 2 * * *"}], pull_request: }\n'):
            with self.subTest(fine=fine):
                text = re.sub(r"^on:\n(?:  .*\n)+", fine, CALLER, flags=re.MULTILINE)
                self.assertEqual(wf.check_workflow("x.yml", text), [])

    def test_an_on_value_that_cant_be_read_is_reported_not_accepted(self) -> None:
        for unreadable in ("on: {}\n", "on: []\n", "on: {push: {a: b}}}\n", "on: {push,, pull_request}\n",
                           "on: [push, {pull_request: null}]\n", "on: [push,,pull_request]\n", "on: {: push}\n",
                           "on: >-\n  pull_request_review_thread\n", "on: >\n  push\n", "on: |\n  push\n",
                           "on: |-\n  push\n  pull_request\n", "on:\n  - push: null\n"):
            with self.subTest(unreadable=unreadable):
                self.assertEqual(wf.check_workflow("x.yml", with_on(unreadable)), [
                    "x.yml: the events under `on:` can't be read (write them as a block mapping or a list)"])
        for unclosed in ("on: {push: null\n", "on: {push: [a}\n", "on: [push, pull_request\n", "on: {push\n}\n",
                         "on: {'push: null}\n"):
            with self.subTest(unclosed=unclosed):
                self.assertEqual(wf.check_workflow("x.yml", with_on(unclosed)), [
                    "x.yml: line 2: a `[...]` or `{...}` value that doesn't close on its line can't be read (write it as "
                    "block mappings, lists and one-line values)"])

    def test_a_bare_key_in_a_flow_mapping_is_an_event_too(self) -> None:
        for bad in ("on: {unsupported, push: null}\n", "on: {push: null, unsupported}\n", "on: {push, unsupported: }\n"):
            with self.subTest(bad=bad):
                self.assertEqual(wf.check_workflow("x.yml", with_on(bad)), ["x.yml: `unsupported` isn't a GitHub Actions "
                                                                           "event (the file would be invalid and never run)"])

    def test_the_events_are_the_ones_githubs_documentation_lists(self) -> None:
        documented = {
            "branch_protection_rule", "check_run", "check_suite", "create", "delete", "deployment", "deployment_status",
            "discussion", "discussion_comment", "fork", "gollum", "image_version", "issue_comment", "issues", "label",
            "merge_group", "milestone", "page_build", "public", "pull_request", "pull_request_review",
            "pull_request_review_comment", "pull_request_target", "push", "registry_package", "release",
            "repository_dispatch", "schedule", "status", "watch", "workflow_call", "workflow_dispatch", "workflow_run"}
        self.assertEqual(set(wf.EVENTS), documented)
        for gone in ("pull_request_review_thread", "projects", "repository_import", "team_add", "member", "star"):
            self.assertNotIn(gone, wf.EVENTS)

    def test_a_block_scalar_that_is_not_the_on_value_is_no_problem(self) -> None:
        text = GOOD + "      - run: |\n          echo on: {nope}\n          runs-on: nope\n"
        self.assertEqual(wf.check_workflow("x.yml", text), [])

    def test_a_uses_line_inside_a_block_scalar_is_text_not_a_step(self) -> None:
        for block in ("- run: |\n          uses: x/y@v1\n", "- run: >\n          uses: x/y@v1\n",
                      "- run: |-\n\n          - uses: x/y@v1   # v1\n          echo done\n",
                      "- name: s\n        run: >-\n          uses: x/y@v1\n",
                      "- uses: ./.github/actions/local\n        with:\n          script: |\n            uses: x/y@v1\n"):
            with self.subTest(block=block):
                self.assertEqual(wf.check_workflow("x.yml", GOOD + "      " + block), [])

    def test_a_real_unpinned_step_is_still_flagged_after_a_block_scalar(self) -> None:
        text = GOOD + "      - run: |\n          uses: x/y@v1\n      - uses: x/z@v2\n"
        self.assertEqual(wf.check_workflow("x.yml", text), ["x.yml: x/z@v2 isn't pinned by its commit SHA"])

    def test_a_key_after_a_block_scalar_in_the_same_item_is_still_checked(self) -> None:
        for header in ("- if: >-\n", "- if: |\n", "- name: s\n        if: >-\n", "- &step if: >-\n"):
            item = "      " + header + "          github.event_name == 'push'\n\n          && true\n        uses: x/y@v1\n"
            pinned = item.replace("x/y@v1", "x/y@" + "a" * 40 + "   # v1.0.0")
            with self.subTest(header=header):
                self.assertEqual(wf.check_workflow("x.yml", GOOD + item), ["x.yml: x/y@v1 isn't pinned by its commit SHA"])
                self.assertEqual(wf.check_workflow("x.yml", GOOD + pinned), [])
        inside = GOOD + "      - if: >-\n          uses: x/y@v1\n        run: echo ok\n"
        self.assertEqual(wf.check_workflow("x.yml", inside), [])
        unplaced = GOOD + "      - if: >-\n          uses: x/y@v1\n        run: |\n          uses: x/z@v2\n"
        self.assertEqual(wf.check_workflow("x.yml", unplaced), ["x.yml: x/z@v2 isn't pinned by its commit SHA"])

    def test_a_block_scalar_whose_end_cant_be_placed_is_not_masked(self) -> None:
        for header in ("- run: |2\n", "- run: >-4\n"):
            with self.subTest(header=header):
                text = GOOD + "      " + header + "          uses: x/y@v1\n"
                self.assertEqual(wf.check_workflow("x.yml", text), ["x.yml: x/y@v1 isn't pinned by its commit SHA"])

    def test_text_the_parser_cannot_place_is_still_checked(self) -> None:
        for block in ("- uses: >\n          x/y@v1\n", "- uses: |-\n          x/y@" + "a" * 40 + "\n"):
            with self.subTest(block=block):
                self.assertEqual(wf.check_workflow("x.yml", GOOD + "      " + block),
                                 ["x.yml: a `uses:` written as a block scalar can't be read (write it on one line)"])
        listed = GOOD + "      - uses: ./.github/actions/local\n        with:\n          args:\n            - |\n" \
                        "              uses: x/y@v1\n"
        self.assertEqual(wf.check_workflow("x.yml", listed), ["x.yml: x/y@v1 isn't pinned by its commit SHA"])

    def test_a_block_scalar_under_env_is_still_read_as_its_value(self) -> None:
        self.assertIn("`runner` isn't available", wf.check_workflow("x.yml", job_env("|\n        ${{ runner.temp }}/x"))[0])
        top = GOOD.replace("jobs:\n", "env:\n  A: >-\n    ${{ steps.s.outputs.o }}\njobs:\n")
        self.assertIn("`steps` isn't available", wf.check_workflow("x.yml", top)[0])

    def test_a_shell_line_inside_a_block_scalar_is_not_the_bash_default(self) -> None:
        text = GOOD.replace("defaults:\n  run:\n    shell: bash\n", "defaults:\n  run:\n    note: |\n      shell: bash\n")
        self.assertIn("no top-level `defaults: run: shell: bash`", wf.check_workflow("x.yml", text)[0])

    def test_a_list_read_inside_a_run_block_is_still_checked(self) -> None:
        text = GOOD + "      - run: |\n          gh api \"repos/$R/pulls?per_page=100\"\n"
        self.assertIn("without --paginate", wf.check_workflow("x.yml", text)[0])

    def test_a_job_is_told_from_an_input_by_its_place_not_its_name(self) -> None:
        def jobs(body: str) -> str:
            return "name: X\non: push\ndefaults:\n  run:\n    shell: bash\njobs:\n" + body

        step = "    steps:\n      - run: true\n"
        bare = wf.check_workflow("x.yml", jobs("  with:\n    runs-on: windows-latest\n" + step))
        self.assertEqual(len(bare), 1)
        self.assertIn("x.yml: `runs-on: windows-latest` should be fleet's RUNS_ON line", bare[0])
        self.assertEqual(wf.check_workflow("x.yml", jobs("  with:\n    " + wf.RUNS_ON + "\n" + step)), [])
        for key in ("with", '"with"', "'with'"):
            call = "  a:\n    uses: ./.github/workflows/x.yml\n    " + key + ":\n      runs-on: windows-latest\n"
            with self.subTest(key=key):
                self.assertEqual(wf.check_workflow("x.yml", jobs(call)), [])
        step_input = "  a:\n    " + wf.RUNS_ON + "\n    steps:\n      - uses: ./x\n        with:\n          runs-on: windows-latest\n"
        self.assertEqual(wf.check_workflow("x.yml", jobs(step_input)), [])
        for key in ('"runs-on"', "'runs-on'", "runs-on"):
            own = "  with:\n    " + key + ": windows-latest\n    uses: ./.github/workflows/x.yml\n    with:\n      runs-on: x\n"
            with self.subTest(own=key):
                self.assertEqual(len(wf.check_workflow("x.yml", jobs(own))), 1)
        quoted_fleet = "  a:\n    " + wf.RUNS_ON.replace("runs-on:", '"runs-on":') + "\n" + step
        self.assertEqual(wf.check_workflow("x.yml", jobs(quoted_fleet)), [])
        hosted = "  a:\n    'runs-on': ubuntu-latest  # hosted: publishes\n" + step
        self.assertEqual(wf.check_workflow("x.yml", jobs(hosted)), [])
        block = "  a:\n    runs-on:\n      group: big\n" + step
        self.assertEqual(len(wf.check_workflow("x.yml", jobs(block))), 1)
        scalar = "  a:\n    runs-on: >-\n      ubuntu-latest\n" + step
        self.assertEqual(len(wf.check_workflow("x.yml", jobs(scalar))), 1)

    def test_a_node_property_does_not_hide_the_mapping_it_marks(self) -> None:
        def jobs(body: str) -> str:
            return "name: X\non: push\ndefaults:\n  run:\n    shell: bash\njobs:\n" + body

        step = "    steps:\n      - run: true\n"
        for marker in ("&build", "!!map", "&build !!map", "!tag &build"):
            bad = "  build: " + marker + "\n    runs-on: windows-latest\n" + step
            good = "  build: " + marker + "\n    " + wf.RUNS_ON + "\n" + step
            with self.subTest(marker=marker):
                found = wf.check_workflow("x.yml", jobs(bad))
                self.assertEqual(len(found), 1)
                self.assertIn("`runs-on: windows-latest` should be fleet's RUNS_ON line", found[0])
                self.assertEqual(wf.check_workflow("x.yml", jobs(good)), [])
        anchored_steps = "  a:\n    " + wf.RUNS_ON + "\n    steps: &s\n      - run: true\n"
        self.assertEqual(wf.check_workflow("x.yml", jobs(anchored_steps)), [])

    def test_a_node_property_on_the_jobs_key_does_not_hide_the_jobs(self) -> None:
        head = "name: X\non: push\ndefaults:\n  run:\n    shell: bash\n"
        step = "    steps:\n      - run: true\n"
        for key in ("&job-map jobs:", "!!map jobs:", "&j !!str jobs:"):
            with self.subTest(key=key):
                found = wf.check_workflow("x.yml", head + key + "\n  build:\n    runs-on: windows-latest\n" + step)
                self.assertEqual(len(found), 1)
                self.assertIn("`runs-on: windows-latest` should be fleet's RUNS_ON line", found[0])
                self.assertEqual(wf.check_workflow("x.yml", head + key + "\n  build:\n    " + wf.RUNS_ON + "\n" + step), [])

    def test_an_escaped_quote_in_a_quoted_value_is_read_not_reported(self) -> None:
        for step in ("- run: 'it''s # data'   # a comment\n", '- run: "say \\"hi\\" # x"  # a comment\n'):
            with self.subTest(step=step):
                self.assertEqual(wf.check_workflow("x.yml", GOOD + "      " + step), [])
                self.assertEqual(wf.check_workflow("x.yml", GOOD + "      " + step + "      - uses: x/y@v1\n"),
                                 ["x.yml: x/y@v1 isn't pinned by its commit SHA"])

    def test_yaml_outside_the_subset_reports_the_file_unreadable(self) -> None:
        bad = "    runs-on: windows-latest\n"
        cases = (
            (GOOD + "      - ? uses\n        : x/y@v1\n", 13, "explicit `?` keys"),
            (GOOD + "      - run: true\n        uses\n", 14, "neither `key: value` nor a list item"),
            (GOOD + "      - run: echo one\n          uses: x/y@v1\n", 14, "goes on to the next line"),
            (GOOD + "      - with: {a: b,\n          uses: x/y@v1}\n", 13, "doesn't close on its line"),
            (GOOD + "---\njobs:\n  b:\n" + bad, 13, "document marker"),
            (GOOD + "...\n", 13, "document marker"),
            (GOOD.replace("jobs:\n  a:\n", "jobs:\n  a:\n" + bad.replace("    ", "      ", 1)), 9, "indented to none"),
            (GOOD + "  a:\n" + bad, 13, "a key given twice"),
        )
        for text, line, reason in cases:
            with self.subTest(text=text):
                found = wf.check_workflow("x.yml", text)
                self.assertEqual(len(found), 1, found)
                self.assertTrue(found[0].startswith(f"x.yml: line {line}: "), found[0])
                self.assertIn(reason, found[0])

    def test_an_alias_or_a_merge_key_is_reported_not_accepted(self) -> None:
        def jobs(body: str) -> str:
            return "name: X\non: push\ndefaults:\n  run:\n    shell: bash\njobs:\n" + body

        step = "    steps:\n      - run: true\n"
        base = "  base: &base\n    runs-on: windows-latest\n" + step
        cases = (base + "  build:\n    <<: *base\n", base + "  build: *base\n",
                 "  a:\n    " + wf.RUNS_ON + "\n    steps:\n      - *step\n",
                 "  a:\n    " + wf.RUNS_ON + "\n    needs: *deps\n" + step)
        for case in cases:
            with self.subTest(case=case):
                self.assertIn("uses YAML aliases, merge keys or explicit `?` keys", " ".join(wf.check_workflow("x.yml", jobs(case))))

    def test_an_explicit_key_is_reported_not_skipped(self) -> None:
        head = "name: X\non: push\ndefaults:\n  run:\n    shell: bash\njobs:\n"
        step = "      steps:\n        - run: true\n"
        for text in (head + "  ? build\n  : \n      runs-on: windows-latest\n" + step,
                     head + "  ?\n    build\n  :\n      runs-on: windows-latest\n" + step):
            with self.subTest(text=text):
                self.assertIn("explicit `?` keys", " ".join(wf.check_workflow("x.yml", text)))
        plain = head + "  ?build:\n    " + wf.RUNS_ON + "\n    steps:\n      - run: true\n"
        self.assertEqual(wf.check_workflow("x.yml", plain), [])

    def test_a_hash_inside_a_quoted_runner_is_not_a_comment(self) -> None:
        head = "name: X\non: push\ndefaults:\n  run:\n    shell: bash\njobs:\n  a:\n"
        tail = "\n    steps:\n      - run: true\n"
        for runner in ('"ubuntu-latest # hosted: reason"', "'ubuntu-latest # hosted: reason'"):
            with self.subTest(runner=runner):
                found = wf.check_workflow("x.yml", head + "    runs-on: " + runner + tail)
                self.assertEqual(len(found), 1)
                self.assertIn("should be fleet's RUNS_ON line", found[0])
        quoted_ok = head + '    runs-on: "ubuntu-latest"   # hosted: reason' + tail
        self.assertEqual(wf.check_workflow("x.yml", quoted_ok), [])

    def test_jobs_that_cant_be_read_are_reported_not_accepted(self) -> None:
        for unreadable in ("{a: {runs-on: windows-latest, steps: []}}", ">-\n  a: b", "|\n  a: b"):
            with self.subTest(unreadable=unreadable):
                text = "name: X\non: push\ndefaults:\n  run:\n    shell: bash\njobs: " + unreadable + "\n"
                self.assertEqual(wf.check_workflow("x.yml", text), ["x.yml: the jobs under `jobs:` can't be read (write "
                                                                    "them as a block mapping)"])

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
