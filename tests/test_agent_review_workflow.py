import json
import re
import subprocess
import unittest
from typing import Any

from fleet_tools import agent_review as ar
from fleet_tools import cli, self_check

WORKFLOWS = cli.ROOT / ".github" / "workflows"
REVIEW = WORKFLOWS / "agent-review.yml"
CALLER = WORKFLOWS / "agent-review-caller.yml"
SELF_TEST = WORKFLOWS / "self-test.yml"
PR_CHECKOUT = "review/pr"
REVIEW_JOBS = ("review", "second-review")
REPORT_JOBS = {"review": "report", "second-review": "second-report"}
SECRET = re.compile(r"\$\{\{\s*secrets\.")
Node = dict[str, Any]


def mapping(node: object) -> Node:
    assert isinstance(node, dict), node
    return node


def steps_of(workflow: Node, job: str) -> list[Node]:
    steps = mapping(workflow["jobs"])[job]["steps"]
    assert isinstance(steps, list)
    return [mapping(step) for step in steps]


def job_source(text: str, job: str) -> str:
    found = re.search(rf"^  {re.escape(job)}:\n(.*?)(?=^  \S|\Z)", text, re.MULTILINE | re.DOTALL)
    assert found, job
    return found.group(1)


def step_sources(text: str, job: str) -> list[str]:
    source = job_source(text, job)
    steps = source.split("    steps:\n", 1)[1]
    return [chunk for chunk in re.split(r"^      - ", steps, flags=re.MULTILINE) if chunk.strip()]


def step_name(step: Node) -> str:
    return str(step.get("name") or step.get("uses") or "")


class ReusableWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = REVIEW.read_text(encoding="utf-8")
        self.workflow = mapping(self_check.parse(self.text))

    def test_the_inputs_the_secrets_and_the_outputs(self) -> None:
        call = mapping(mapping(self.workflow["on"])["workflow_call"])
        self.assertEqual(sorted(call["inputs"]), sorted([
            "generated", "first-review", "verification", "budget-usd", "max-turns", "merge-gate-workflow", "fleet-ref", "dry-run", "fake-review",
            "second-review-labels", "second-review-paths", "second-review-always", "second-review-budget-usd", "second-review-max-turns", "fake-event",
        ]))
        inputs = mapping(call["inputs"])
        self.assertEqual((mapping(inputs["first-review"])["type"], mapping(inputs["first-review"])["default"]), ("boolean", "false"))
        self.assertEqual((mapping(inputs["verification"])["type"], mapping(inputs["verification"])["default"]), ("boolean", "false"))
        self.assertEqual(mapping(inputs["budget-usd"])["default"], "3")
        self.assertEqual(mapping(inputs["max-turns"])["default"], "60")
        self.assertEqual(mapping(inputs["second-review-labels"])["default"], "safety-critical,security")
        self.assertEqual(mapping(inputs["second-review-paths"])["default"], "")
        self.assertEqual(mapping(inputs["second-review-budget-usd"])["default"], "10")
        self.assertEqual(mapping(inputs["second-review-max-turns"])["default"], "120")
        self.assertEqual(mapping(inputs["merge-gate-workflow"])["default"], "merge-gate.yml")
        self.assertEqual(mapping(inputs["dry-run"])["default"], "false")
        self.assertEqual(mapping(inputs["fake-event"])["default"], "")
        self.assertNotIn("model", inputs)
        self.assertNotIn("second-review-model", inputs)
        secrets = mapping(call["secrets"])
        self.assertEqual(sorted(secrets), ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"])
        for secret in secrets.values():
            self.assertEqual(mapping(secret)["required"], "false")
        self.assertEqual(sorted(call["outputs"]), ["description", "record", "second-description", "second-verdict", "verdict"])
        outputs = mapping(call["outputs"])
        self.assertEqual(mapping(outputs["second-verdict"])["value"], "${{ jobs.second-review.outputs.verdict }}")
        self.assertEqual(mapping(outputs["verdict"])["value"], "${{ jobs.review.outputs.verdict }}")
        self.assertEqual(mapping(outputs["record"])["value"], "${{ jobs.review.outputs.record }}")
        self.assertEqual(mapping(mapping(mapping(self.workflow["jobs"])["review"])["outputs"])["record"], "${{ steps.plan.outputs.record }}")
        self.assertEqual(self.workflow["env"], {"FLEET_REF": self_check.expected_ref(cli.version())})
        self.assertEqual(self.workflow["permissions"], "{}")
        self.assertNotIn("concurrency", self.workflow)

    def test_every_job_runs_hosted_with_the_reason_and_the_least_permissions(self) -> None:
        jobs = mapping(self.workflow["jobs"])
        self.assertEqual(sorted(jobs), ["report", "review", "second-pending", "second-report", "second-review"])
        for job in jobs:
            self.assertRegex(job_source(self.text, job), r"runs-on: ubuntu-latest[ \t]+# hosted: \S")
        for job in REVIEW_JOBS:
            self.assertEqual(mapping(jobs[job])["permissions"], {"contents": "read", "pull-requests": "read", "statuses": "read"})
        for job in REPORT_JOBS.values():
            self.assertEqual(mapping(jobs[job])["permissions"],
                             {"contents": "read", "pull-requests": "write", "statuses": "write", "actions": "write"})
            self.assertNotIn("concurrency", mapping(jobs[job]))
        self.assertEqual(mapping(mapping(jobs["review"])["concurrency"]),
                         {"group": "agent-review-${{ github.event.pull_request.number }}-${{ inputs.fake-review }}", "cancel-in-progress": "true"})
        second = mapping(mapping(jobs["second-review"])["concurrency"])
        self.assertEqual(second["cancel-in-progress"], "true")
        self.assertTrue(second["group"].startswith("second-review-${{ github.event.pull_request.number }}-${{ inputs.fake-review }}-${{ inputs.fake-event }}-"))
        self.assertTrue(second["group"].endswith("-${{ !contains(fromJSON('[\"labeled\", \"unlabeled\"]'), github.event.action) && 'push' || "
                                                 "needs.second-pending.outputs.event-class || format('unknown-{0}', github.run_id) }}"))
        self.assertNotIn("label.name", second["group"])
        self.assertEqual(mapping(jobs["second-review"])["needs"], "second-pending")
        self.assertNotEqual(second["group"][:13], "agent-review-")

    def test_second_pending_classifies_the_event_with_split_list_before_anything_can_exit(self) -> None:
        job = mapping(mapping(self.workflow["jobs"])["second-pending"])
        self.assertEqual(mapping(job["outputs"]), {"event-class": "${{ steps.class.outputs.class }}"})
        steps = steps_of(self.workflow, "second-pending")
        self.assertEqual([step_name(s) for s in steps][:2], ["actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                                                             "Classify the event for the second review's concurrency group"])
        step = steps[1]
        self.assertEqual(step["id"], "class")
        self.assertNotIn("if", step)
        self.assertEqual(mapping(step["env"]), {"ACTION": "${{ github.event.action }}", "LABEL": "${{ github.event.label.name }}",
                                                "TRIGGER_LABELS": "${{ inputs.second-review-labels }}"})
        self.assertEqual(step["run"], "python3 -I .fleet/fleet_tools/agent_review.py second event-class")

    def test_the_pull_request_checkout_keeps_no_credentials_and_every_checkout_says_so(self) -> None:
        for job in REVIEW_JOBS:
            checkouts = [step for step in steps_of(self.workflow, job) if str(step.get("uses", "")).startswith("actions/checkout@")]
            self.assertEqual(len(checkouts), 3, job)
            for step in checkouts:
                self.assertEqual(mapping(step["with"])["persist-credentials"], "false")
            pr = [step for step in checkouts if mapping(step["with"]).get("path") == PR_CHECKOUT]
            self.assertEqual(len(pr), 1)
            self.assertEqual(mapping(pr[0]["with"])["ref"], "${{ github.event.pull_request.head.sha }}")
            self.assertEqual(pr[0]["if"], "steps.who.outputs.review == 'true'")
            trusted = [mapping(step["with"]) for step in checkouts if mapping(step["with"]).get("path") in ("trusted", ".fleet")]
            self.assertEqual(len(trusted), 2)
            for inputs in trusted:
                self.assertIn(inputs["ref"], ("${{ github.event.pull_request.base.sha }}", "${{ inputs.fleet-ref || env.FLEET_REF }}"))
        self.assertEqual(self.text.count("actions/checkout@"), 9)
        self.assertEqual(self.text.count("persist-credentials: false"), 9)

    def test_the_secrets_appear_only_in_the_review_steps_which_run_nothing_from_the_pull_request(self) -> None:
        for job in REVIEW_JOBS:
            with_secret = [chunk for chunk in step_sources(self.text, job) if SECRET.search(chunk)]
            self.assertEqual(len(with_secret), 1, job)
            step = with_secret[0]
            self.assertTrue(step.startswith("name: Review\n"), step[:40])
            self.assertNotIn("uses:", step)
            self.assertNotIn(PR_CHECKOUT, step)
            self.assertNotIn("npm", step)
            self.assertNotIn("node ", step)
            for executable in re.findall(r"(\S+\.(?:sh|py))\b", step.split("run: |", 1)[1]):
                self.assertTrue(executable.startswith("../.fleet/"), executable)
        self.assertEqual(len(SECRET.findall(self.text)), 4)
        self.assertEqual(len(re.findall(r"secrets\.ANTHROPIC_API_KEY", self.text)), 2)
        self.assertEqual(len(re.findall(r"secrets\.CLAUDE_CODE_OAUTH_TOKEN", self.text)), 2)
        env = mapping(next(s for s in steps_of(self.workflow, "review") if s.get("id") == "review")["env"])
        self.assertEqual(env["MODEL"], "sonnet")
        self.assertEqual(env["BUDGET"], "${{ inputs.budget-usd }}")
        self.assertEqual(env["MAX_TURNS"], "${{ inputs.max-turns }}")

    def test_the_models_are_fixed_and_each_review_s_secrets_stay_in_its_own_job(self) -> None:
        second = mapping(next(s for s in steps_of(self.workflow, "second-review") if s.get("id") == "review")["env"])
        self.assertEqual(second["MODEL"], "claude-opus-5-5")
        self.assertEqual(second["BUDGET"], "${{ inputs.second-review-budget-usd }}")
        self.assertEqual(second["MAX_TURNS"], "${{ inputs.second-review-max-turns }}")
        stub = mapping(next(s for s in steps_of(self.workflow, "second-review") if step_name(s).startswith("Review with the stub"))["env"])
        self.assertEqual(stub["MODEL"], "claude-opus-5-5")
        self.assertNotIn("claude-opus-5-5", job_source(self.text, "review"))
        self.assertNotIn("MODEL: sonnet", job_source(self.text, "second-review"))
        self.assertEqual(len(re.findall(r"^\s+MODEL: claude-opus-5-5$", self.text, re.MULTILINE)), 2)
        self.assertEqual(len(re.findall(r"^\s+MODEL: sonnet$", self.text, re.MULTILINE)), 2)
        self.assertNotIn("inputs.model", self.text)
        self.assertNotIn("fable", self.text.lower())
        for job in (*REPORT_JOBS.values(),):
            self.assertIsNone(SECRET.search(job_source(self.text, job)), job)
            self.assertNotIn("MODEL", job_source(self.text, job))

    def test_every_step_with_a_token_runs_before_the_pull_request_is_checked_out(self) -> None:
        for job in REVIEW_JOBS:
            steps = steps_of(self.workflow, job)
            checkout = next(i for i, step in enumerate(steps) if mapping(step.get("with") or {}).get("path") == PR_CHECKOUT)
            for i, step in enumerate(steps):
                env = mapping(step.get("env") or {})
                if "GH_TOKEN" in env:
                    self.assertLess(i, checkout, f"{job}: {step_name(step)}")
                else:
                    self.assertNotIn("github.token", json.dumps(step), f"{job}: {step_name(step)}")

    def test_nothing_from_the_pull_request_is_run_installed_or_imported(self) -> None:
        for job in REVIEW_JOBS:
            for chunk in step_sources(self.text, job):
                if "run:" not in chunk:
                    continue
                script = chunk.split("run:", 1)[1]
                for line in script.splitlines():
                    if "python3" in line:
                        self.assertIn("python3 -I .fleet/fleet_tools/agent_review.py", line)
                self.assertNotRegex(script, r"(^|\s)(cd|source|\.)\s+review/pr", "nothing runs in the pull request's checkout")
                self.assertNotIn("review/pr/", script)
            install = [chunk for chunk in step_sources(self.text, job) if "npm ci" in chunk]
            self.assertEqual(len(install), 1)
            self.assertIn("$RUNNER_TEMP/claude-code", install[0])
            self.assertIn(".fleet/.github/agent-review/package-lock.json", install[0])

    def test_the_report_jobs_have_no_secret_and_check_out_only_fleet_s_tools(self) -> None:
        for review, job in REPORT_JOBS.items():
            report = job_source(self.text, job)
            self.assertIsNone(SECRET.search(report))
            self.assertNotIn(PR_CHECKOUT, report)
            self.assertNotIn("head.sha }}\n          path", report)
            steps = steps_of(self.workflow, job)
            checkouts = [step for step in steps if "uses" in step]
            self.assertEqual(len(checkouts), 1)
            self.assertTrue(str(checkouts[0]["uses"]).startswith("actions/checkout@"))
            self.assertEqual(mapping(checkouts[0]["with"]), {"repository": "AnthonyPluth/fleet", "ref": "${{ inputs.fleet-ref || env.FLEET_REF }}",
                                                             "path": ".fleet", "sparse-checkout": "fleet_tools", "persist-credentials": "false"})
            self.assertEqual(steps.index(checkouts[0]), 0)
            for line in report.splitlines():
                if "python3" in line:
                    self.assertRegex(line, r"python3 -I \.fleet/fleet_tools/review_outcomes\.py --finish comment\.md"
                                     + (r"|python3 -I \.fleet/fleet_tools/agent_review\.py second (runs|publish|settle) " if job == "second-report" else ""))
            self.assertIn('--repo "$REPO" --pr "$PR" --sha "$SHA"', report)
            self.assertIn('--incremental "$INCREMENTAL" --touched touched.txt --findings findings.json', report)
            self.assertIn('base64 -d <<< "$TOUCHED" > touched.txt', report)
            self.assertIn('base64 -d <<< "$FINDINGS" > findings.json', report)
            self.assertLess(report.index("review_outcomes.py --finish"), report.index("(dry run) the comment:"))
            self.assertLess(report.index("review_outcomes.py --finish"), report.index("-X PATCH"))
            env = mapping(next(s for s in steps if step_name(s).startswith("Post"))["env"])
            self.assertEqual(env["INCREMENTAL"], f"${{{{ needs.{review}.outputs.incremental }}}}")
            self.assertEqual(env["FINDINGS"], f"${{{{ needs.{review}.outputs.findings }}}}")
            self.assertEqual(env["TOUCHED"], f"${{{{ needs.{review}.outputs.touched }}}}")
            outputs = mapping(mapping(mapping(self.workflow["jobs"])[review])["outputs"])
            self.assertEqual(outputs["incremental"], "${{ steps.plan.outputs.incremental }}")
            self.assertEqual(outputs["findings"], "${{ steps.result.outputs.findings }}")
            self.assertEqual(outputs["touched"], "${{ steps.plan.outputs.touched }}")
            self.assertIn("gh api -X POST \"repos/$REPO/statuses/$SHA\"", report)
            self.assertIn("success:skip)", report)
            self.assertIn("DRY_RUN", report)
            self.assertIn('if [ -n "$MERGE_GATE" ]', report)
            self.assertEqual(mapping(mapping(self.workflow["jobs"])[job])["needs"], review)

    def test_each_report_posts_only_its_own_status_and_comment(self) -> None:
        first, second = job_source(self.text, "report"), job_source(self.text, "second-report")
        self.assertIn('-f context="Agent review"', first)
        self.assertIn('startswith("<!-- agent-review -->")', first)
        self.assertNotIn("Second review", first.split("name: Report", 1)[1])
        self.assertNotIn("second-review", first.split("name: Report", 1)[1])
        self.assertIn('-f context="Second review"', second)
        self.assertIn('startswith("<!-- second-review -->")', second)
        self.assertIn("review_outcomes.py --finish comment.md --second ", second)
        self.assertNotIn("--second", first)
        self.assertNotIn('"Agent review"', second)
        self.assertNotIn("<!-- agent-review -->", second)
        self.assertNotIn("needs.review.", second)
        self.assertNotIn("needs.second-review.", first)
        self.assertEqual(self.text.count('-f context="Agent review"'), 1)
        self.assertEqual(self.text.count('-f context="Second review"'), 4)
        self.assertEqual(second.count('-f context="Second review"'), 2)
        self.assertIn('-f context="Second review"', job_source(self.text, "second-pending"))

    def test_label_events_dependabot_and_first_review_off_run_neither_first_review_job(self) -> None:
        for job in ("review", "report"):
            found = re.search(r"^    if: >-\n((?:      .*\n)+)", job_source(self.text, job), re.MULTILINE)
            assert found, job
            condition = found.group(1)
            self.assertIn("inputs.first-review &&", condition)
            self.assertIn("github.event.pull_request.user.login != 'dependabot[bot]' &&", condition)
            self.assertIn("!contains(fromJSON('[\"labeled\", \"unlabeled\"]'), github.event.action)", condition)
            if job == "report":
                self.assertIn("!cancelled() &&", condition)

    def test_the_second_review_runs_on_label_events_but_not_for_dependabot_or_a_non_owner_s_unlabeled(self) -> None:
        for job in ("second-review", "second-report"):
            found = re.search(r"^    if: >-\n((?:      .*\n)+)", job_source(self.text, job), re.MULTILINE)
            assert found, job
            condition = found.group(1)
            self.assertNotIn("first-review", condition)
            self.assertNotIn("contains(fromJSON", condition)
            self.assertIn("github.event.pull_request.user.login != 'dependabot[bot]' &&", condition)
            self.assertIn("!(github.event.action == 'unlabeled' && github.event.sender.login != github.repository_owner)", condition)
            self.assertIn("!cancelled() &&", condition)
        self.assertEqual(mapping(mapping(self.workflow["jobs"])["second-review"])["needs"], "second-pending")

    def test_a_label_event_marks_the_second_review_pending_before_the_review_runs(self) -> None:
        job = mapping(mapping(self.workflow["jobs"])["second-pending"])
        self.assertEqual(job["permissions"], {"contents": "read", "statuses": "write", "actions": "write"})
        self.assertNotIn("needs", job)
        found = re.search(r"^    if: >-\n((?:      .*\n)+)", job_source(self.text, "second-pending"), re.MULTILINE)
        assert found
        condition = found.group(1)
        self.assertIn("github.event.pull_request.user.login != 'dependabot[bot]' &&", condition)
        self.assertIn("!(github.event.action == 'unlabeled' && github.event.sender.login != github.repository_owner) &&", condition)
        self.assertIn("contains(fromJSON('[\"labeled\", \"unlabeled\"]'), github.event.action)", condition)
        self.assertIn("inputs.fake-review != '' && inputs.fake-event != ''", condition)
        source = job_source(self.text, "second-pending")
        self.assertIsNone(SECRET.search(source))
        self.assertNotIn(PR_CHECKOUT, source)
        self.assertNotIn("head.sha }}\n          path", source)
        checkouts = [step for step in steps_of(self.workflow, "second-pending") if str(step.get("uses", "")).startswith("actions/checkout@")]
        self.assertEqual(len(checkouts), 1)
        self.assertEqual(mapping(checkouts[0]["with"])["repository"], "AnthonyPluth/fleet")
        self.assertEqual(mapping(checkouts[0]["with"])["sparse-checkout"], "fleet_tools")
        step = next(c for c in step_sources(self.text, "second-pending") if c.startswith("name: Mark"))
        self.assertIn("agent_review.py second wanted \"$ACTION\"", step)
        self.assertIn("grep -qx 'pending=true'", step)
        self.assertIn("-f state=pending", step)
        self.assertIn('-f context="Second review"', step)
        self.assertIn('if [ -n "$MERGE_GATE" ]', step)
        self.assertIn("DRY_RUN", step)
        env = mapping(next(s for s in steps_of(self.workflow, "second-pending") if step_name(s).startswith("Mark"))["env"])
        self.assertEqual(env["FAKE_EVENT"], "${{ inputs.fake-review != '' && inputs.fake-event || '' }}")
        self.assertEqual(env["HEAD"], "${{ github.event.pull_request.head.sha }}")
        for name in ("SENDER", "OWNER", "LABEL", "TRIGGER_LABELS", "MERGE_GATE"):
            self.assertIn(name, env)

    def test_the_second_review_s_decisions_come_from_data_in_the_environment(self) -> None:
        who = next(s for s in steps_of(self.workflow, "second-review") if s.get("id") == "who")
        env = mapping(who["env"])
        self.assertEqual(env["SENDER"], "${{ github.event.sender.login }}")
        self.assertEqual(env["OWNER"], "${{ github.repository_owner }}")
        self.assertEqual(env["LABEL"], "${{ github.event.label.name }}")
        self.assertEqual(env["LABELS"], "${{ toJSON(github.event.pull_request.labels.*.name) }}")
        self.assertEqual(env["TRIGGER_LABELS"], "${{ inputs.second-review-labels }}")
        self.assertEqual(env["RISKY_PATHS"], "${{ inputs.second-review-paths }}")
        self.assertEqual(env["FAKE_EVENT"], "${{ inputs.fake-review != '' && inputs.fake-event || '' }}")
        chunk = next(c for c in step_sources(self.text, "second-review") if c.startswith("name: Is this an agent"))
        self.assertIn("agent_review.py second wanted \"$ACTION\"", chunk)
        self.assertIn("invariants.md", chunk)
        self.assertNotRegex(chunk.split("run: |", 1)[1], r"\$\{\{")
        risky = next(s for s in steps_of(self.workflow, "second-review") if s.get("id") == "risky")
        self.assertEqual(risky["run"], "python3 -I .fleet/fleet_tools/agent_review.py second risky review/files.txt review/reason.txt")
        self.assertEqual(mapping(risky["env"])["FAKE_EVENT"], "${{ inputs.fake-review != '' && inputs.fake-event || '' }}")
        verdict = next(c for c in step_sources(self.text, "second-review") if c.startswith("name: Verdict"))
        self.assertIn("verdict=skip", verdict)
        self.assertIn('[ "$RISKY" != true ]', verdict)
        self.assertIn("No .github/agent-review/invariants.md", verdict)
        self.assertIn("agent_review.py second report", verdict)
        self.assertIsNone(SECRET.search(verdict))
        plan = next(c for c in step_sources(self.text, "second-review") if c.startswith("name: Gather"))
        self.assertIn("agent_review.py second plan", plan)
        self.assertIn("first-review.md", plan)
        for job in ("review",):
            self.assertNotIn("agent_review.py second", job_source(self.text, job))

    def test_the_second_reviewer_runs_only_on_a_risky_change_with_the_invariants(self) -> None:
        steps = {step_name(step): step for step in steps_of(self.workflow, "second-review")}
        real = "steps.plan.outputs.mode == 'review' && steps.risky.outputs.risky == 'true' && steps.who.outputs.invariants == 'true' && inputs.fake-review == ''"
        for name in ("Review", "Install Claude Code"):
            self.assertEqual(steps[name]["if"], real, name)
        stub = steps["Review with the stub reviewer (self-test)"]
        self.assertEqual(stub["if"], real.replace("== ''", "!= ''"))
        self.assertEqual(mapping(stub["env"])["CLAUDE"], "${{ github.workspace }}/.fleet/tests/fakes/claude")
        self.assertIsNone(SECRET.search(json.dumps(stub["env"])))

    def test_the_decisions_the_who_step_feeds_the_verdict(self) -> None:
        who = next(s for s in steps_of(self.workflow, "review") if s.get("id") == "who")
        env = mapping(who["env"])
        self.assertEqual(env["ACTION"], "${{ github.event.action }}")
        self.assertEqual(env["HEAD"], "${{ github.event.pull_request.head.sha }}")
        chunk = next(c for c in step_sources(self.text, "review") if c.startswith("name: Is this an agent"))
        self.assertIn("agent_review.py wanted \"$ACTION\"", chunk)
        self.assertIn("agent-review/context.md", chunk)
        verdict = next(c for c in step_sources(self.text, "review") if c.startswith("name: Verdict"))
        self.assertIn("verdict=skip", verdict)
        self.assertIn("No .github/agent-review/context.md", verdict)
        self.assertIsNone(SECRET.search(verdict))

    def test_the_verification_record_reaches_plan_as_data_and_only_its_hash_leaves(self) -> None:
        for job in REVIEW_JOBS:
            steps = steps_of(self.workflow, job)
            plan = mapping(next(s for s in steps if s.get("id") == "plan")["env"])
            self.assertEqual(plan["VERIFICATION"], "${{ inputs.verification }}")
            self.assertEqual(plan["BODY"], "${{ github.event.pull_request.body }}")
            self.assertEqual(plan["PREV_RECORD"], "${{ steps.prev.outputs.record }}")
            verdict = mapping(next(s for s in steps if s.get("id") == "result")["env"])
            self.assertEqual(verdict["RECORD_HASH"], "${{ steps.plan.outputs.record }}")
            prev = next(c for c in step_sources(self.text, job) if c.startswith("name: The last review"))
            self.assertIn("read -r sha verdict record < <(", prev)
            self.assertIn('echo "record=${record:-}"', prev)
            for chunk in step_sources(self.text, job):
                if "run:" in chunk:
                    self.assertNotRegex(chunk.split("run:", 1)[1], r"\$\{\{[^}]*body")
                    self.assertNotIn("verification.md", chunk.split("run:", 1)[1])
        for job in REPORT_JOBS.values():
            self.assertNotIn("BODY", job_source(self.text, job))
            self.assertNotIn("pull_request.body", job_source(self.text, job))
        self.assertEqual(self.text.count("BODY: ${{ github.event.pull_request.body }}"), 4)

    def test_the_stub_review_runs_only_when_asked_and_the_real_one_only_otherwise(self) -> None:
        steps = {step_name(step): step for step in steps_of(self.workflow, "review")}
        self.assertEqual(steps["Review"]["if"], "steps.plan.outputs.mode == 'review' && inputs.fake-review == ''")
        self.assertEqual(steps["Install Claude Code"]["if"], "steps.plan.outputs.mode == 'review' && inputs.fake-review == ''")
        stub = steps["Review with the stub reviewer (self-test)"]
        self.assertEqual(stub["if"], "steps.plan.outputs.mode == 'review' && inputs.fake-review != ''")
        env = mapping(stub["env"])
        self.assertEqual(env["CLAUDE"], "${{ github.workspace }}/.fleet/tests/fakes/claude")
        self.assertEqual(env["FAKE_REVIEW"], "${{ inputs.fake-review }}")
        self.assertIsNone(SECRET.search(json.dumps(env)))


class NoFableTest(unittest.TestCase):
    def test_no_file_names_fable_but_the_changelog_and_the_tests_that_refuse_it(self) -> None:
        allowed = {"CHANGELOG.md", "tests/test_agent_review_workflow.py", "tests/test_checks_commits.py"}
        listed = subprocess.run(["git", "ls-files", "-z"], cwd=cli.ROOT, check=True, capture_output=True).stdout
        for name in filter(None, listed.decode().split("\0")):
            path = cli.ROOT / name
            if name in allowed or not path.is_file():
                continue
            self.assertIsNone(re.search(r"\bfable\b", path.read_bytes().decode("utf-8", "replace"), re.IGNORECASE), name)


class CallerTest(unittest.TestCase):
    def test_fleet_dogfoods_the_review_on_pull_request_target(self) -> None:
        text = CALLER.read_text(encoding="utf-8")
        workflow = mapping(self_check.parse(text))
        trigger = mapping(mapping(workflow["on"])["pull_request_target"])
        self.assertEqual(trigger["branches"], "[main]")
        self.assertEqual(trigger["types"], "[opened, synchronize, reopened, ready_for_review, labeled, unlabeled]")
        self.assertIn("zizmor: ignore[dangerous-triggers]", text)
        job = mapping(mapping(workflow["jobs"])["review"])
        self.assertEqual(job["uses"], "./.github/workflows/agent-review.yml")
        self.assertEqual(mapping(job["secrets"]), {
            "ANTHROPIC_API_KEY": "${{ secrets.ANTHROPIC_API_KEY }}",
            "CLAUDE_CODE_OAUTH_TOKEN": "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}",
        })
        self.assertNotIn("inherit", text)
        inputs = mapping(job["with"])
        self.assertEqual(inputs["budget-usd"], "${{ vars.AGENT_REVIEW_BUDGET_USD || '3' }}")
        self.assertEqual(inputs["max-turns"], "${{ vars.AGENT_REVIEW_MAX_TURNS || '60' }}")
        self.assertNotIn("first-review", inputs)
        self.assertEqual(job["permissions"], {"contents": "read", "pull-requests": "write", "statuses": "write", "actions": "write"})
        self.assertTrue((cli.ROOT / ".github" / "agent-review" / "context.md").is_file())


class SelfTestTest(unittest.TestCase):
    def test_the_self_test_runs_the_stub_in_dry_run_for_each_answer(self) -> None:
        workflow = mapping(self_check.parse(SELF_TEST.read_text(encoding="utf-8")))
        jobs = mapping(workflow["jobs"])
        calls = {name: mapping(job) for name, job in jobs.items() if mapping(job).get("uses") == "./.github/workflows/agent-review.yml"}
        first = {name: job for name, job in calls.items() if name.startswith("agent-review-")}
        self.assertEqual(sorted(first), ["agent-review-blocking", "agent-review-malformed", "agent-review-pass", "agent-review-verification"])
        answers = []
        for name, job in calls.items():
            inputs = mapping(job["with"])
            self.assertEqual(inputs["dry-run"], "true")
            self.assertEqual(inputs["fleet-ref"], "${{ github.sha }}")
            self.assertEqual(inputs["merge-gate-workflow"], "")
            self.assertNotIn("secrets", job)
            self.assertNotEqual(inputs["fake-review"], "")
            if name == "agent-review-verification":
                self.assertEqual(inputs["verification"], "true")
            else:
                self.assertNotIn("verification", inputs, name)
        for job in first.values():
            inputs = mapping(job["with"])
            self.assertEqual(inputs["first-review"], "true")
            self.assertNotIn("fake-event", inputs)
            answers.append(inputs["fake-review"])
        self.assertEqual(sorted(answers), ["blocking", "malformed", "pass", "verification"])
        verdicts = mapping(jobs["agent-review-verdicts"])
        self.assertEqual(sorted(str(verdicts["needs"]).strip("[]").split(", ")), sorted(first))
        source = job_source(SELF_TEST.read_text(encoding="utf-8"), "agent-review-verdicts")
        self.assertIn('[ "$VERIFICATION" = blocking ]', source)
        self.assertIn("needs.agent-review-verification.outputs.record", source)
        self.assertIn("::warning::", source)

    def test_the_self_test_fakes_each_second_review_case_the_issue_lists(self) -> None:
        workflow = mapping(self_check.parse(SELF_TEST.read_text(encoding="utf-8")))
        jobs = mapping(workflow["jobs"])
        second = {name: mapping(job) for name, job in jobs.items()
                  if mapping(job).get("uses") == "./.github/workflows/agent-review.yml" and name.startswith("second-review-")}
        self.assertEqual(sorted(second), ["second-review-github", "second-review-label", "second-review-not-risky", "second-review-path",
                                          "second-review-unlabeled-by-another", "second-review-unlabeled-by-owner"])
        events = {}
        for name, job in second.items():
            inputs = mapping(job["with"])
            self.assertNotIn("first-review", inputs, name)
            self.assertNotIn("second-review-labels", inputs)
            events[name] = {k: v for k, _, v in (token.partition("=") for token in inputs["fake-event"].split())}
        self.assertEqual(events["second-review-label"]["labels"], "security,docs")
        self.assertEqual(mapping(second["second-review-label"]["with"])["fake-review"], "blocking")
        self.assertIn("app/photos/", events["second-review-path"]["files"])
        self.assertIn("second-review-paths", mapping(second["second-review-path"]["with"]))
        self.assertIn("        app/photos/**\n", SELF_TEST.read_text(encoding="utf-8"))
        self.assertTrue(events["second-review-github"]["files"].startswith(".github/"))
        self.assertNotIn("second-review-paths", mapping(second["second-review-github"]["with"]))
        self.assertEqual(events["second-review-not-risky"]["labels"], "docs")
        self.assertFalse(any(f.startswith((".github/", ".claude/")) for f in events["second-review-not-risky"]["files"].split(",")))
        for name in ("second-review-unlabeled-by-another", "second-review-unlabeled-by-owner"):
            self.assertEqual(events[name]["action"], "unlabeled")
            self.assertEqual(events[name]["label"], "security")
        self.assertEqual(events["second-review-unlabeled-by-another"]["sender"], "octocat")
        self.assertIn("sender=${{ github.repository_owner }} ", mapping(second["second-review-unlabeled-by-owner"]["with"])["fake-event"])
        self.assertEqual(len({mapping(job["with"])["fake-event"] for job in second.values()}), len(second))
        verdicts = mapping(jobs["second-review-verdicts"])
        self.assertEqual(sorted(str(verdicts["needs"]).strip("[]").split(", ")), sorted(second))
        source = SELF_TEST.read_text(encoding="utf-8")
        self.assertIn("outputs.second-verdict", source)
        self.assertIn('[ "$LABEL" = blocking ]', source)
        self.assertIn('"Not a risky pull request"', source)
        self.assertIn('[ "$OTHER" = skip ]', source)


class FindingOutcomesSelfTestTest(unittest.TestCase):
    def test_the_self_test_runs_the_outcomes_script_from_the_pull_request_s_checkout(self) -> None:
        text = SELF_TEST.read_text(encoding="utf-8")
        workflow = mapping(self_check.parse(text))
        job = mapping(mapping(workflow["jobs"])["finding-outcomes"])
        self.assertEqual(job["permissions"], {"contents": "read"})
        self.assertRegex(job_source(text, "finding-outcomes"), r"runs-on: ubuntu-latest[ \t]+# hosted: \S")
        steps = [mapping(s) for s in job["steps"]]
        self.assertEqual(mapping(steps[0]["with"])["persist-credentials"], "false")
        self.assertEqual(steps[1]["run"], ".github/scripts/finding-outcomes-self-test.sh")
        script = cli.ROOT / ".github" / "scripts" / "finding-outcomes-self-test.sh"
        self.assertTrue(script.is_file())
        self.assertTrue(script.stat().st_mode & 0o111)

    def test_the_self_test_runs_the_second_review_order_cases_from_the_pull_request_s_checkout(self) -> None:
        text = SELF_TEST.read_text(encoding="utf-8")
        job = mapping(mapping(mapping(self_check.parse(text))["jobs"])["second-review-order"])
        self.assertEqual(job["permissions"], {"contents": "read"})
        steps = [mapping(s) for s in job["steps"]]
        self.assertEqual(mapping(steps[0]["with"])["persist-credentials"], "false")
        self.assertEqual(steps[1]["run"], "python3 -m unittest -v tests.test_second_review_order")
        fake = cli.ROOT / "tests" / "fakes" / "github"
        self.assertTrue(fake.stat().st_mode & 0o111)


class LockedClaudeCodeTest(unittest.TestCase):
    def test_the_lockfile_locks_the_version_package_json_names(self) -> None:
        directory = cli.ROOT / ".github" / "agent-review"
        package = json.loads((directory / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((directory / "package-lock.json").read_text(encoding="utf-8"))
        version = package["dependencies"]["@anthropic-ai/claude-code"]
        self.assertEqual(lock["packages"]["node_modules/@anthropic-ai/claude-code"]["version"], version)
        self.assertEqual(lock["packages"][""]["dependencies"]["@anthropic-ai/claude-code"], version)
        self.assertEqual(lock["name"], package["name"])
        self.assertEqual(lock["packages"][""]["name"], package["name"])
        self.assertIn("node", package["engines"])

    def test_dependabot_bumps_it_weekly_after_a_cooldown(self) -> None:
        dependabot = mapping(self_check.parse((cli.ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")))
        updates = [mapping(u) for u in dependabot["updates"]]
        npm = [u for u in updates if u.get("package-ecosystem") == "npm"]
        self.assertEqual(len(npm), 1)
        self.assertEqual(npm[0]["directory"], "/.github/agent-review")
        self.assertEqual(mapping(npm[0]["schedule"])["interval"], "weekly")
        self.assertEqual(mapping(npm[0]["cooldown"])["default-days"], "7")


def always_risky_in_caller() -> list[str]:
    text = CALLER.read_text(encoding="utf-8")
    found = re.search(r"^      second-review-always: \|\n((?:        .*\n)+)", text, re.MULTILINE)
    assert found
    return [line.strip() for line in found.group(1).splitlines() if line.strip()]


class FleetsOwnSecondReviewScopeTest(unittest.TestCase):
    def test_every_listed_path_exists(self) -> None:
        for path in always_risky_in_caller():
            if "*" not in path:
                self.assertTrue((cli.ROOT / path).exists(), path)

    def test_every_reusable_workflow_is_covered(self) -> None:
        listed = always_risky_in_caller()
        reusable = [path for path in sorted(WORKFLOWS.glob("*.yml")) if self_check.is_reusable(self_check.parse(path.read_text(encoding="utf-8")))]
        self.assertTrue(reusable)
        for path in reusable:
            relative = path.relative_to(cli.ROOT).as_posix()
            self.assertTrue(any(ar.matches(relative, pattern) for pattern in listed), relative)

    def test_the_release_workflow_is_covered(self) -> None:
        listed = always_risky_in_caller()
        self.assertIn(".github/workflows/release.yml", listed)
        self.assertTrue(any(ar.matches(".github/workflows/release.yml", pattern) for pattern in listed))

    def test_the_exempt_workflows_are_fleets_own_ci_only(self) -> None:
        listed = always_risky_in_caller()
        exempt = [path.name for path in sorted(WORKFLOWS.glob("*.yml"))
                  if not any(ar.matches(path.relative_to(cli.ROOT).as_posix(), pattern) for pattern in listed)]
        self.assertEqual(exempt, ["ci.yml", "self-test.yml"])


if __name__ == "__main__":
    unittest.main()
