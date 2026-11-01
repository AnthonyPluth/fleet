import json
import re
import unittest
from typing import Any

from fleet_tools import cli, self_check

WORKFLOWS = cli.ROOT / ".github" / "workflows"
REVIEW = WORKFLOWS / "agent-review.yml"
CALLER = WORKFLOWS / "agent-review-caller.yml"
SELF_TEST = WORKFLOWS / "self-test.yml"
PR_CHECKOUT = "review/pr"
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
            "generated", "first-review", "budget-usd", "max-turns", "merge-gate-workflow", "fleet-ref", "dry-run", "fake-review",
        ]))
        inputs = mapping(call["inputs"])
        self.assertEqual(mapping(inputs["first-review"])["default"], "true")
        self.assertEqual(mapping(inputs["budget-usd"])["default"], "3")
        self.assertEqual(mapping(inputs["max-turns"])["default"], "60")
        self.assertEqual(mapping(inputs["merge-gate-workflow"])["default"], "merge-gate.yml")
        self.assertEqual(mapping(inputs["dry-run"])["default"], "false")
        self.assertNotIn("model", inputs)
        secrets = mapping(call["secrets"])
        self.assertEqual(sorted(secrets), ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"])
        for secret in secrets.values():
            self.assertEqual(mapping(secret)["required"], "false")
        self.assertEqual(sorted(call["outputs"]), ["description", "verdict"])
        self.assertEqual(self.workflow["env"], {"FLEET_REF": "main"})
        self.assertEqual(self.workflow["permissions"], "{}")
        self.assertNotIn("concurrency", self.workflow)

    def test_both_jobs_run_hosted_with_the_reason_and_the_least_permissions(self) -> None:
        jobs = mapping(self.workflow["jobs"])
        self.assertEqual(sorted(jobs), ["report", "review"])
        for job in ("review", "report"):
            self.assertRegex(job_source(self.text, job), r"runs-on: ubuntu-latest[ \t]+# hosted: \S")
        self.assertEqual(mapping(jobs["review"])["permissions"], {"contents": "read", "pull-requests": "read", "statuses": "read"})
        self.assertEqual(mapping(jobs["report"])["permissions"], {"pull-requests": "write", "statuses": "write", "actions": "write"})
        self.assertEqual(mapping(mapping(jobs["review"])["concurrency"]),
                         {"group": "agent-review-${{ github.event.pull_request.number }}", "cancel-in-progress": "true"})
        self.assertNotIn("concurrency", mapping(jobs["report"]))

    def test_the_pull_request_checkout_keeps_no_credentials_and_every_checkout_says_so(self) -> None:
        checkouts = [step for step in steps_of(self.workflow, "review") if str(step.get("uses", "")).startswith("actions/checkout@")]
        self.assertEqual(len(checkouts), 3)
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

    def test_the_secrets_appear_only_in_the_review_step_which_runs_nothing_from_the_pull_request(self) -> None:
        with_secret = [chunk for chunk in step_sources(self.text, "review") if SECRET.search(chunk)]
        self.assertEqual(len(with_secret), 1)
        step = with_secret[0]
        self.assertTrue(step.startswith("name: Review\n"), step[:40])
        self.assertNotIn("uses:", step)
        self.assertNotIn(PR_CHECKOUT, step)
        self.assertNotIn("npm", step)
        self.assertNotIn("node ", step)
        for executable in re.findall(r"(\S+\.(?:sh|py))\b", step.split("run: |", 1)[1]):
            self.assertTrue(executable.startswith("../.fleet/"), executable)
        self.assertEqual(len(SECRET.findall(self.text)), 2)
        self.assertEqual(len(re.findall(r"secrets\.ANTHROPIC_API_KEY", self.text)), 1)
        self.assertEqual(len(re.findall(r"secrets\.CLAUDE_CODE_OAUTH_TOKEN", self.text)), 1)
        env = mapping(next(s for s in steps_of(self.workflow, "review") if s.get("id") == "review")["env"])
        self.assertEqual(env["MODEL"], "sonnet")
        self.assertEqual(env["BUDGET"], "${{ inputs.budget-usd }}")
        self.assertEqual(env["MAX_TURNS"], "${{ inputs.max-turns }}")

    def test_every_step_with_a_token_runs_before_the_pull_request_is_checked_out(self) -> None:
        steps = steps_of(self.workflow, "review")
        checkout = next(i for i, step in enumerate(steps) if mapping(step.get("with") or {}).get("path") == PR_CHECKOUT)
        for i, step in enumerate(steps):
            env = mapping(step.get("env") or {})
            if "GH_TOKEN" in env:
                self.assertLess(i, checkout, step_name(step))

    def test_nothing_from_the_pull_request_is_run_installed_or_imported(self) -> None:
        for chunk in step_sources(self.text, "review"):
            if "run: |" not in chunk:
                continue
            script = chunk.split("run: |", 1)[1]
            for line in script.splitlines():
                if "python3" in line:
                    self.assertIn("python3 -I .fleet/fleet_tools/agent_review.py", line)
            self.assertNotRegex(script, r"(^|\s)(cd|source|\.)\s+review/pr", "nothing runs in the pull request's checkout")
            self.assertNotIn("review/pr/", script)
        install = [chunk for chunk in step_sources(self.text, "review") if "npm ci" in chunk]
        self.assertEqual(len(install), 1)
        self.assertIn("$RUNNER_TEMP/claude-code", install[0])
        self.assertIn(".fleet/.github/agent-review/package-lock.json", install[0])

    def test_the_report_job_has_no_secret_and_no_checkout(self) -> None:
        report = job_source(self.text, "report")
        self.assertIsNone(SECRET.search(report))
        self.assertNotIn("actions/checkout", report)
        for step in steps_of(self.workflow, "report"):
            self.assertNotIn("uses", step)
        self.assertIn("gh api -X POST \"repos/$REPO/statuses/$SHA\"", report)
        self.assertIn('-f context="Agent review"', report)
        self.assertIn("success:skip)", report)
        self.assertIn("DRY_RUN", report)
        self.assertIn('if [ -n "$MERGE_GATE" ]', report)

    def test_label_events_dependabot_and_first_review_off_run_neither_job(self) -> None:
        for job in ("review", "report"):
            found = re.search(r"^    if: >-\n((?:      .*\n)+)", job_source(self.text, job), re.MULTILINE)
            assert found, job
            condition = found.group(1)
            self.assertIn("inputs.first-review &&", condition)
            self.assertIn("github.event.pull_request.user.login != 'dependabot[bot]' &&", condition)
            self.assertIn("!contains(fromJSON('[\"labeled\", \"unlabeled\"]'), github.event.action)", condition)
            if job == "report":
                self.assertIn("!cancelled() &&", condition)

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
        self.assertEqual(job["permissions"], {"contents": "read", "pull-requests": "write", "statuses": "write", "actions": "write"})
        self.assertTrue((cli.ROOT / ".github" / "agent-review" / "context.md").is_file())


class SelfTestTest(unittest.TestCase):
    def test_the_self_test_runs_the_stub_in_dry_run_for_each_answer(self) -> None:
        workflow = mapping(self_check.parse(SELF_TEST.read_text(encoding="utf-8")))
        jobs = mapping(workflow["jobs"])
        calls = {name: mapping(job) for name, job in jobs.items() if mapping(job).get("uses") == "./.github/workflows/agent-review.yml"}
        self.assertEqual(sorted(calls), ["agent-review-blocking", "agent-review-malformed", "agent-review-pass"])
        answers = []
        for job in calls.values():
            inputs = mapping(job["with"])
            self.assertEqual(inputs["dry-run"], "true")
            self.assertEqual(inputs["fleet-ref"], "${{ github.sha }}")
            self.assertEqual(inputs["merge-gate-workflow"], "")
            self.assertNotIn("secrets", job)
            answers.append(inputs["fake-review"])
        self.assertEqual(sorted(answers), ["blocking", "malformed", "pass"])
        verdicts = mapping(jobs["agent-review-verdicts"])
        self.assertEqual(sorted(str(verdicts["needs"]).strip("[]").split(", ")), sorted(calls))


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


if __name__ == "__main__":
    unittest.main()
