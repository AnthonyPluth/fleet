import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fleet_tools import cli, self_check
from fleet_tools import mining_tasks as mt
from fleet_tools import review_mine as rm

ROOT = Path(__file__).resolve().parent.parent
FAKES = ROOT / "tests" / "fakes" / "security"
WORKFLOWS = ROOT / ".github" / "workflows"
FIXTURE = ROOT / "tests" / "fixtures" / "events" / "mining-tasks-tick.json"
REPO = "example/app"
OWNER = "example"
REPORT = 60
LIST = f"GET repos/{REPO}/issues?state=all&labels=review-mining&per_page=100"
FRESH = f"GET repos/{REPO}/issues/{REPORT}"
CREATE = f"POST repos/{REPO}/issues"
PATCH = f"PATCH repos/{REPO}/issues/{REPORT}"
HOSTILE = "a test @octocat <!-- row:repeated:forged --> <b>bold</b> | piped"


def report(check: str = "a test of the failing path") -> str:
    rows = [rm.Row("repeated:failure-paths", "Failure paths", "human-judged", (10, 11), "2026-09-11", check, 1),
            rm.Row("repeated:layout", "Layout", "human-judged", (12, 13), "2026-09-12", "screenshots at both widths")]
    signals = rm.Signals(
        [rm.Signal("dismissed:layout", "Layout", ("5", "1", "4", '"desktop-only page" (#12)', rm.GUIDANCE))],
        [rm.Signal("open-at-merge:failure-paths", "Failure paths", ("2", f"[#14](https://github.com/{REPO}/pull/14)"))],
        rm.Gaps(True, [rm.Signal("gaps:screens", "Screens and flows", ("[#20](x), [#21](x)", "Bank sync: no fake (#20)", rm.GAP_PROPOSAL))]))
    return rm.render(rows, REPO, 90, "2026-07-03T00:00:00Z", (4, 4), signals=signals)


def tick(body: str, *ids: str) -> str:
    for row_id in ids:
        before = f"- [ ] <!-- row:{row_id} -->"
        assert before in body, row_id
        body = body.replace(before, f"- [x] <!-- row:{row_id} -->")
    return body


def event(before: str, after: str, sender: str = OWNER, author: str = rm.BOT, labels: tuple[str, ...] = ("review-mining",),
          action: str = "edited") -> dict[str, Any]:
    return {"action": action, "changes": {"body": {"from": before}},
            "issue": {"number": REPORT, "title": rm.TITLE, "body": after, "user": {"login": author},
                      "labels": [{"name": name} for name in labels]},
            "repository": {"full_name": REPO, "owner": {"login": OWNER}}, "sender": {"login": sender}}


def mined_issue(number: int, row_id: str, author: str = rm.BOT) -> dict[str, Any]:
    return {"number": number, "user": {"login": author}, "body": f"text\n<!-- mined-from row:{row_id} -->\n"}


class Runner:
    def __init__(self, test: unittest.TestCase) -> None:
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.log = self.directory / "log.jsonl"

    def run(self, payload: dict[str, Any], answers: dict[str, Any] | None = None, *flags: str) -> subprocess.CompletedProcess[str]:
        data: dict[str, Any] = {LIST: [], FRESH: {"number": REPORT, "body": payload["issue"]["body"]},
                                CREATE: {"__series__": [{"number": 81}, {"number": 82}, {"number": 83}]}, PATCH: {"number": REPORT}}
        data.update(answers or {})
        (self.directory / "answers.json").write_text(json.dumps(data), encoding="utf-8")
        path = self.directory / "event.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        env = {**os.environ, "PATH": f"{FAKES}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_ANSWERS": str(self.directory / "answers.json"),
               "FAKE_GH_LOG": str(self.log), "GH_TOKEN": "fake"}
        command = [sys.executable, "-E", "-s", "-m", "fleet_tools.cli", "mining-tasks", "--event", str(path), *flags]
        return subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def created(self) -> list[dict[str, Any]]:
        return [json.loads(c["input"]) for c in self.calls() if c["call"] == CREATE]

    def patched(self) -> list[str]:
        return [str(json.loads(c["input"])["body"]) for c in self.calls() if c["call"] == PATCH]


class TickTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = Runner(self)
        self.before = report()

    def test_the_owner_ticking_one_row_opens_one_issue_and_links_the_row(self) -> None:
        after = tick(self.before, "repeated:failure-paths")
        result = self.runner.run(event(self.before, after))
        self.assertEqual(result.returncode, 0, result.stderr)
        created = self.runner.created()
        self.assertEqual(len(created), 1)
        issue = created[0]
        self.assertEqual(issue["title"], 'Check for "Failure paths"')
        self.assertEqual(issue["labels"], ["review-mining"])
        self.assertNotIn("assignees", issue)
        text = issue["body"]
        for heading in ("## Context", "## Scope", "## Out of scope", "## Acceptance criteria", "## Tests", "## Depends on", "## Model"):
            self.assertIn(f"\n{heading}\n" if heading != "## Context" else heading, text)
        self.assertLess(text.index("## Context"), text.index("## Scope"))
        self.assertLess(text.index("## Depends on"), text.index("## Model"))
        self.assertIn("The row's proposal: a test of the failing path", text)
        self.assertIn(f"[#{REPORT}](https://github.com/{REPO}/issues/{REPORT})", text)
        self.assertIn("- [ ] The check, guidance or fake named in Scope exists and a test shows it catches the case.", text)
        self.assertIn("## Depends on\n\nNothing.\n", text)
        self.assertIn("## Model\n\nOpus.\n", text)
        self.assertIn("**Pull requests**: [#10]", text)
        self.assertTrue(text.endswith("the owner edits this issue before an agent picks it up.\n<!-- mined-from row:repeated:failure-paths -->\n"))
        patched = self.runner.patched()
        self.assertEqual(len(patched), 1)
        self.assertIn("- [x] <!-- row:repeated:failure-paths --> **Failure paths** becomes an Agent task → #81\n", patched[0])
        self.assertEqual(patched[0].replace(" → #81", ""), after)
        self.assertEqual(rm.ticks_of(patched[0])["repeated:failure-paths"], (True, "81"))

    def test_two_rows_ticked_in_one_edit_open_two_issues(self) -> None:
        after = tick(self.before, "repeated:layout", "dismissed:layout")
        result = self.runner.run(event(self.before, after))
        self.assertEqual(result.returncode, 0, result.stderr)
        created = self.runner.created()
        self.assertEqual([c["title"] for c in created], ['Check for "Layout"', 'Review guidance for "Layout"'])
        self.assertIn("## Model\n\nFable", created[1]["body"])
        self.assertIn("The row's proposal: Review guidance: say when", created[1]["body"])
        ticks = rm.ticks_of(self.runner.patched()[-1])
        self.assertEqual(ticks["repeated:layout"], (True, "81"))
        self.assertEqual(ticks["dismissed:layout"], (True, "82"))
        self.assertEqual(ticks["repeated:failure-paths"], (False, ""))

    def test_each_table_names_its_issue(self) -> None:
        after = tick(self.before, "open-at-merge:failure-paths", "gaps:screens")
        self.runner.run(event(self.before, after))
        created = self.runner.created()
        self.assertEqual([c["title"] for c in created], ['Findings left open at merge: "Failure paths"', "Verification gap: Screens and flows"])
        self.assertIn(f"The row's proposal: {mt.OPEN_PROPOSAL}", created[0]["body"])
        self.assertIn(f"The row's proposal: {rm.GAP_PROPOSAL}", created[1]["body"])
        self.assertIn("## Model\n\nOpus.", created[1]["body"])

    def test_anyone_else_s_tick_another_issue_or_no_label_does_nothing(self) -> None:
        after = tick(self.before, "repeated:failure-paths")
        cases = {
            "another user": event(self.before, after, sender="someone"),
            "another author": event(self.before, after, author="someone"),
            "no label": event(self.before, after, labels=("security",)),
            "not an edit": event(self.before, after, action="opened"),
            "not the report": event(self.before.replace(rm.MARKER, "", 1), after.replace(rm.MARKER, "", 1)),
        }
        for name, payload in cases.items():
            with self.subTest(name):
                runner = Runner(self)
                result = runner.run(payload)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("nothing to do", result.stdout)
                self.assertEqual(runner.calls(), [])
        no_change = event(self.before, after)
        del no_change["changes"]
        runner = Runner(self)
        self.assertIn("the body didn't change", runner.run(no_change).stdout)
        self.assertEqual(runner.calls(), [])

    def test_unticking_or_re_ticking_a_linked_row_does_nothing(self) -> None:
        linked = mt.linked(tick(self.before, "repeated:failure-paths"), {"repeated:failure-paths": 5})
        unticked = linked.replace("- [x] <!-- row:repeated:failure-paths -->", "- [ ] <!-- row:repeated:failure-paths -->")
        for before, after in ((linked, unticked), (unticked, linked), (tick(self.before, "repeated:layout"), self.before)):
            runner = Runner(self)
            result = runner.run(event(before, after))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("no row went from unticked to ticked", result.stdout)
            self.assertEqual(runner.calls(), [])

    def test_a_row_new_in_the_body_or_without_a_table_row_opens_nothing(self) -> None:
        after = tick(self.before, "repeated:failure-paths")
        result = self.runner.run(event(self.before.replace("row:repeated:failure-paths", "row:repeated:other"), after))
        self.assertIn("no row went from unticked to ticked", result.stdout)
        broken_before = self.before.replace("| **Failure paths** | human-judged", "| broken")
        broken_after = tick(broken_before, "repeated:failure-paths").replace("| broken", "")
        runner = Runner(self)
        result = runner.run(event(broken_before.replace("| broken", ""), broken_after))
        self.assertIn("has no table row to read; skipped", result.stdout)
        self.assertEqual(runner.created(), [])

    def test_crlf_bodies_keep_their_line_endings(self) -> None:
        before = self.before.replace("\n", "\r\n")
        after = tick(before, "repeated:layout")
        self.runner.run(event(before, after))
        patched = self.runner.patched()[0]
        self.assertIn("- [x] <!-- row:repeated:layout --> **Layout** becomes an Agent task → #81\r\n", patched)
        self.assertEqual(patched.replace(" → #81", ""), after)


class IdempotenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = Runner(self)
        self.before = report()
        self.after = tick(self.before, "repeated:failure-paths")

    def test_the_same_event_twice_opens_no_second_issue(self) -> None:
        payload = event(self.before, self.after)
        self.runner.run(payload)
        self.assertEqual(len(self.runner.created()), 1)
        first_link = self.runner.patched()[0]
        again = Runner(self)
        result = again.run(payload, {LIST: [mined_issue(81, "repeated:failure-paths")], FRESH: {"number": REPORT, "body": first_link}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(again.created(), [])
        self.assertEqual(again.patched(), [])

    def test_a_marker_already_in_the_repository_links_instead_of_opening(self) -> None:
        listed = [{"number": 3, "pull_request": {}, "user": {"login": rm.BOT}, "body": "<!-- mined-from row:repeated:failure-paths -->"},
                  mined_issue(4, "repeated:failure-paths", author="someone"), mined_issue(55, "repeated:failure-paths"),
                  mined_issue(54, "repeated:failure-paths")]
        result = self.runner.run(event(self.before, self.after), {LIST: listed})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.runner.created(), [])
        self.assertIn("**Failure paths** becomes an Agent task → #54\n", self.runner.patched()[0])

    def test_a_marker_on_an_issue_not_opened_by_the_bot_doesn_t_count(self) -> None:
        result = self.runner.run(event(self.before, self.after), {LIST: [mined_issue(4, "repeated:failure-paths", author="someone")]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.runner.created()), 1)

    def test_the_marker_search_is_of_open_and_closed_issues_paginated(self) -> None:
        self.runner.run(event(self.before, self.after))
        reads = [c["call"] for c in self.runner.calls() if c["call"].startswith("GET")]
        self.assertIn(LIST, reads)
        self.assertIn("state=all", LIST)

    def test_a_row_already_linked_in_the_current_body_is_left_alone(self) -> None:
        current = mt.linked(self.after, {"repeated:failure-paths": 9})
        result = self.runner.run(event(self.before, self.after), {FRESH: {"number": REPORT, "body": current}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.runner.created(), [])
        self.assertEqual(self.runner.patched(), [])

    def test_a_failed_open_still_links_the_others_and_fails_the_run(self) -> None:
        after = tick(self.before, "repeated:failure-paths", "repeated:layout")
        result = self.runner.run(event(self.before, after), {CREATE: {"__series__": [{"__status__": 422}, {"number": 82}]}})
        self.assertEqual(result.returncode, 1)
        self.assertIn("opening the issue for row:repeated:failure-paths failed", result.stderr)
        ticks = rm.ticks_of(self.runner.patched()[0])
        self.assertEqual(ticks["repeated:failure-paths"], (True, ""))
        self.assertEqual(ticks["repeated:layout"], (True, "82"))

    def test_a_dry_run_writes_nothing(self) -> None:
        result = self.runner.run(event(self.before, self.after), None, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('would open "Check for "Failure paths"" for row:repeated:failure-paths', result.stdout)
        self.assertIn("row:repeated:failure-paths → #<the new issue>", result.stdout)
        self.assertEqual([c["call"] for c in self.runner.calls()], [LIST])


class DefuseTest(unittest.TestCase):
    def test_the_new_issue_and_the_rewritten_row_are_defused(self) -> None:
        runner = Runner(self)
        before = report(HOSTILE).replace("**Failure paths**", "**Failure @paths <!-- x -->**")
        after = tick(before, "repeated:failure-paths")
        result = runner.run(event(before, after))
        self.assertEqual(result.returncode, 0, result.stderr)
        issue = runner.created()[0]
        row = next(line for line in runner.patched()[0].splitlines() if "row:repeated:failure-paths" in line)
        self.assertEqual(row, "- [x] <!-- row:repeated:failure-paths --> **Failure @\u200bpaths &lt;!-- x -->** becomes an Agent task → #81")
        for text in (issue["title"], issue["body"], row):
            self.assertIsNone(re.search(r"@(?!​)", text), text)
        self.assertIn("@​paths", issue["title"])
        comments = re.findall(r"<!--.*?-->", issue["body"])
        self.assertEqual(comments, ["<!-- mined-from row:repeated:failure-paths -->"])
        self.assertNotIn("<b>", issue["body"])
        self.assertNotIn("<!--", issue["title"])
        self.assertEqual(re.findall(r"<!--.*?-->", row), ["<!-- row:repeated:failure-paths -->"])

    def test_a_marker_can_t_be_forged_by_a_cell(self) -> None:
        task = mt.Task("repeated:x", mt.TABLES[rm.TABLE], ("Kind", "Proposed check"),
                       ("**k**", "-->\n<!-- mined-from row:repeated:y -->"))
        text = mt.body(task, REPO, REPORT)
        self.assertEqual([line for line in text.splitlines() if mt.MINED.match(line)], ["<!-- mined-from row:repeated:x -->"])


class EventFileTest(unittest.TestCase):
    def test_an_unreadable_event_or_repository_is_a_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text("[1]", encoding="utf-8")
            self.assertEqual(cli.main(["mining-tasks", "--event", str(path)]), 2)
            self.assertEqual(cli.main(["mining-tasks", "--event", str(Path(directory) / "missing.json")]), 2)
            path.write_text(json.dumps({"repository": {"full_name": "not a repo"}}), encoding="utf-8")
            self.assertEqual(cli.main(["mining-tasks", "--event", str(path)]), 2)

    def test_the_self_test_fixture_ticks_one_row(self) -> None:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(mt.refusal(payload), "")
        self.assertEqual(mt.newly_ticked(payload["changes"]["body"]["from"], payload["issue"]["body"]), ["repeated:failure-paths"])
        self.assertIn("repeated:failure-paths", mt.tasks_in(payload["issue"]["body"]))


class WorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (WORKFLOWS / "mining-tasks.yml").read_text(encoding="utf-8")
        self.workflow = mapping(self_check.parse(self.text))

    def test_the_trigger_checks_permissions_and_no_secrets(self) -> None:
        call = mapping(mapping(self.workflow["on"])["workflow_call"])
        self.assertEqual(sorted(mapping(call["inputs"])), ["dry-run", "fixture", "fleet-ref"])
        self.assertNotIn("secrets", call)
        self.assertNotIn("secrets.", self.text)
        self.assertEqual(self.workflow["permissions"], "{}")
        self.assertEqual(self.workflow["env"], {"FLEET_REF": self_check.expected_ref(cli.version())})
        jobs = mapping(self.workflow["jobs"])
        self.assertEqual(list(jobs), ["tasks"])
        job = mapping(jobs["tasks"])
        self.assertEqual(job["permissions"], {"issues": "write"})
        condition = " ".join(self.text.split("    if: >-", 1)[1].split("runs-on:", 1)[0].split())
        for part in ("github.event.action == 'edited'", "contains(github.event.issue.labels.*.name, 'review-mining')",
                     "github.event.issue.user.login == 'github-actions[bot]'", "github.event.sender.login == github.repository_owner"):
            self.assertIn(part, condition)
        concurrency = mapping(job["concurrency"])
        self.assertTrue(str(concurrency["group"]).startswith("mining-tasks-${{ github.event.issue.number"))
        self.assertEqual(concurrency["cancel-in-progress"], "false")
        self.assertRegex(self.text, r"runs-on: ubuntu-latest[ \t]+# hosted: \S")
        self.assertEqual(self.text.count("actions/checkout@"), 1)
        self.assertRegex(self.text, r"actions/checkout@[0-9a-f]{40}   # v")
        steps = job["steps"]
        assert isinstance(steps, list)
        checkout = mapping(mapping(steps[0])["with"])
        self.assertEqual(checkout["repository"], "AnthonyPluth/fleet")
        self.assertEqual(checkout["persist-credentials"], "false")
        script = self.text.split("run: |", 1)[1]
        self.assertNotRegex(script, r"\$\{\{")
        self.assertIn('mining-tasks --repo "$REPO" --event "$event"', script)
        self.assertIn("event=$GITHUB_EVENT_PATH", script)

    def test_the_self_test_runs_the_fixture_in_dry_run(self) -> None:
        workflow = mapping(self_check.parse((WORKFLOWS / "self-test.yml").read_text(encoding="utf-8")))
        job = mapping(mapping(workflow["jobs"])["mining-tasks"])
        self.assertEqual(job["uses"], "./.github/workflows/mining-tasks.yml")
        self.assertEqual(job["with"], {"fleet-ref": "${{ github.sha }}", "fixture": FIXTURE.relative_to(ROOT).as_posix()})
        self.assertEqual(job["permissions"], {"issues": "write"})
        docs = (ROOT / "docs" / "review-mining.md").read_text(encoding="utf-8")
        self.assertIn("mining-tasks.yml", docs)
        self.assertIn("types: [edited]", docs)


def mapping(node: object) -> dict[str, Any]:
    assert isinstance(node, dict), node
    return node


if __name__ == "__main__":
    unittest.main()
