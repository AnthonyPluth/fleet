import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fleet_tools import agent_review as ar
from fleet_tools import cli, self_check
from fleet_tools import review_mine as rm

ROOT = Path(__file__).resolve().parent.parent
FAKES = ROOT / "tests" / "fakes" / "security"
WORKFLOWS = ROOT / ".github" / "workflows"
REPO = "example/app"
NOW = "2026-10-01T00:00:00Z"
SINCE = "2026-07-03T00:00:00Z"
PULLS = f"GET repos/{REPO}/pulls?state=all&sort=updated&direction=desc&per_page=100&page=1"
COMMENTS = f"GET repos/{REPO}/issues/comments?since={SINCE}&per_page=100"
OPEN = f"GET repos/{REPO}/issues?state=open&labels=review-mining&per_page=100"
CREATE = f"POST repos/{REPO}/issues"
AGENTS = (ROOT / "AGENTS.md").read_text(encoding="utf-8")


def finding(kind: str, title: str = "t", check: str = "", severity: str = "advisory", file: str = "") -> dict[str, Any]:
    return {"severity": severity, "kind": kind, "title": title, "check": check, "file": file}


def encoded(findings: list[dict[str, Any]]) -> str:
    return base64.b64encode(json.dumps(findings).encode()).decode()


def review_comment(number: int, findings: list[dict[str, Any]], review: ar.Review = ar.FIRST, when: str = "2026-09-01T00:00:00Z",
                   login: str = rm.BOT, body: str | None = None) -> dict[str, Any]:
    if body is None:
        body = ar.render(findings, "summary", "run", "a" * 40, review)
    return {"user": {"login": login}, "body": body, "updated_at": when, "created_at": when,
            "issue_url": f"https://api.github.com/repos/{REPO}/issues/{number}"}


def pull(number: int, updated: str = "2026-09-01T00:00:00Z") -> dict[str, Any]:
    return {"number": number, "updated_at": updated}


def answers(pulls: list[dict[str, Any]], comments: list[dict[str, Any]], open_issues: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {PULLS: pulls, COMMENTS: comments, OPEN: open_issues or [], CREATE: {"number": 77}, f"PATCH repos/{REPO}/issues/60": {"number": 60}}


class Miner:
    def __init__(self, test: unittest.TestCase) -> None:
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.answers = self.directory / "answers.json"
        self.log = self.directory / "log.jsonl"
        self.summary = self.directory / "summary.md"

    def run(self, data: dict[str, Any], *flags: str, agents: str | None = AGENTS) -> subprocess.CompletedProcess[str]:
        self.answers.write_text(json.dumps(data), encoding="utf-8")
        agents_md = self.directory / "AGENTS.md"
        if agents is not None:
            agents_md.write_text(agents, encoding="utf-8")
        env = {**os.environ, "PATH": f"{FAKES}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_ANSWERS": str(self.answers),
               "FAKE_GH_LOG": str(self.log), "GH_TOKEN": "fake", "RUN_URL": "https://example.invalid/run/1"}
        command = [sys.executable, "-E", "-s", "-m", "fleet_tools.cli", "review-mine", "--repo", REPO, "--now", NOW,
                   "--agents-md", str(agents_md), "--summary", str(self.summary), *flags]
        return subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def writes(self) -> list[tuple[str, dict[str, Any]]]:
        return [(c["call"], json.loads(c["input"])) for c in self.calls() if c["call"].split(" ")[0] in ("POST", "PATCH")]


class MistakesTest(unittest.TestCase):
    def test_reads_each_item_s_state_from_its_parenthesis(self) -> None:
        items = rm.mistakes(AGENTS)
        self.assertEqual(sorted(items), ["access that outlives the person", "failure paths", "github actions", "layout", "undo and restore"])
        self.assertEqual(items["failure paths"].state, rm.HUMAN)
        self.assertEqual(items["github actions"].state, rm.PARTLY)
        self.assertEqual(items["failure paths"].title, "Failure paths")

    def test_a_checked_item_names_its_check_and_other_sections_dont_count(self) -> None:
        text = "## Other\n\n- **Not this** (checked: x)\n\n## Mistakes that keep coming back\n\n- **Visibility** (checked: waypoint-trip-visibility). Text.\n- **Odd** (Checked: tests/test_odd.py; and more). Text.\n- **Loose** . No state.\n- **Human** (human-judged: why). Text.\n"
        items = rm.mistakes(text)
        self.assertEqual(sorted(items), ["human", "loose", "odd", "visibility"])
        self.assertEqual(items["loose"], rm.Item("Loose", rm.UNSTATED, ""))
        self.assertEqual(items["visibility"], rm.Item("Visibility", rm.CHECKED, "waypoint-trip-visibility"))
        self.assertEqual(items["odd"], rm.Item("Odd", rm.CHECKED, "tests/test_odd.py"))
        self.assertEqual(items["human"].state, rm.HUMAN)
        self.assertNotIn("not this", items)

    def test_a_missing_file_gives_no_items(self) -> None:
        self.assertEqual(rm.read_mistakes(Path(tempfile.gettempdir()) / "no-such-agents.md"), {})
        self.assertEqual(rm.read_mistakes(None), {})


class RecordTest(unittest.TestCase):
    def test_only_the_bot_s_review_comments_third_line_is_read(self) -> None:
        findings = [finding("Failure paths", "Drops rows", "a test")]
        parsed = rm.findings_of(review_comment(1, findings))
        assert parsed is not None
        self.assertIs(parsed[0], ar.FIRST)
        self.assertEqual(parsed[1], [{"severity": "advisory", "kind": "Failure paths", "title": "Drops rows", "file": "", "check": "a test"}])
        second = rm.findings_of(review_comment(1, findings, ar.SECOND))
        assert second is not None
        self.assertIs(second[0], ar.SECOND)
        self.assertIsNone(rm.findings_of(review_comment(1, findings, login="octocat")))
        self.assertIsNone(rm.findings_of({"body": ar.render(findings, "s", "u", "a" * 40)}))

    def test_a_findings_line_anywhere_but_line_three_is_ignored(self) -> None:
        line = f"<!-- agent-review-findings {encoded([finding('Failure paths')])} -->"
        self.assertIsNone(rm.findings_of(review_comment(1, [], body=f"{ar.MARKER}\n{line}\n<!-- agent-review-state sha={'a' * 40} verdict=pass -->\n## x")))
        self.assertIsNone(rm.findings_of(review_comment(1, [], body=f"{ar.MARKER}\n<!-- agent-review-state sha={'a' * 40} verdict=pass -->\ntext\n{line}")))
        self.assertIsNone(rm.findings_of(review_comment(1, [], body=f"Thanks!\n{ar.MARKER}\nx\n{line}")))
        self.assertIsNone(rm.findings_of(review_comment(1, [], body=f"{ar.MARKER}\nonly two lines")))
        self.assertIsNone(rm.findings_of(review_comment(1, [], body=f"<!-- second-review -->\nx\n{line}")))

    def test_a_malformed_or_oversized_record_is_ignored(self) -> None:
        def body(third: str) -> str:
            return f"{ar.MARKER}\n<!-- agent-review-state sha={'a' * 40} verdict=pass -->\n{third}\n## Independent review"

        self.assertIsNone(rm.findings_of(review_comment(1, [], body=body("<!-- agent-review-findings not-base64 -->"))))
        self.assertIsNone(rm.findings_of(review_comment(1, [], body=body(f"<!-- agent-review-findings {encoded([])}"))))
        self.assertIsNone(rm.findings_of(review_comment(1, [], body=body("<!-- agent-review-findings " + base64.b64encode(b'{"a": 1}').decode() + " -->"))))
        huge = encoded([finding("k", "x" * 150) for _ in range(80)])
        self.assertGreater(len(huge), ar.MAX_FINDINGS_LINE)
        self.assertIsNone(rm.findings_of(review_comment(1, [], body=body(f"<!-- agent-review-findings {huge} -->"))))
        self.assertIsNone(rm.findings_of(review_comment(1, [], body=body(f"<!-- agent-review-findings {encoded([])} -->") + "x" * ar.MAX_COMMENT)))
        parsed = rm.findings_of(review_comment(1, [], body=body(f"<!-- agent-review-findings {encoded([finding('k', 'x' * 201), finding('k', 'ok'), finding('', 'x'), {'severity': 'fatal', 'kind': 'k', 'title': 't'}, finding('k', 'bad check', check='c' * 301), {'severity': 'advisory', 'kind': 'k', 'title': 'no strings', 'file': 3}])} -->")))
        assert parsed is not None
        self.assertEqual([f["title"] for f in parsed[1]], ["ok"])
        many = rm.findings_of(review_comment(1, [], body=body(f"<!-- agent-review-findings {encoded([finding('k', str(i)) for i in range(rm.MAX_FINDINGS + 5)])} -->")))
        assert many is not None
        self.assertEqual(len(many[1]), rm.MAX_FINDINGS)

    def test_only_the_newest_record_per_review_and_pull_request_counts(self) -> None:
        older = review_comment(5, [finding("Layout")], when="2026-08-01T00:00:00Z")
        newer = review_comment(5, [finding("Failure paths")], when="2026-08-02T00:00:00Z")
        second = review_comment(5, [finding("Undo and restore")], ar.SECOND, when="2026-07-15T00:00:00Z")
        elsewhere = review_comment(9, [finding("Layout")])
        records = rm.newest_records([newer, older, second, elsewhere], {5})
        self.assertEqual(sorted(records), [(5, "agent-review"), (5, "second-review")])
        self.assertEqual(records[(5, "agent-review")][1][0]["kind"], "Failure paths")
        self.assertEqual(records[(5, "second-review")][1][0]["kind"], "Undo and restore")


class TableTest(unittest.TestCase):
    def rows(self, records: dict[tuple[int, str], tuple[str, list[dict[str, Any]]]], agents: str = AGENTS) -> list[rm.Row]:
        return rm.rows_of(rm.groups_of(records, rm.mistakes(agents)))

    def test_kinds_match_agents_items_case_insensitively_and_other_titles_are_normalized(self) -> None:
        records = {
            (1, "agent-review"): ("2026-08-01T00:00:00Z", [finding("failure paths", "a", "a test"), finding("other", "Unused import!")]),
            (2, "agent-review"): ("2026-08-03T00:00:00Z", [finding("FAILURE PATHS", "b", "a test"), finding("Other", "unused import")]),
            (3, "second-review"): ("2026-08-02T00:00:00Z", [finding("Failure paths", "c", "mypy"), finding("other", "Something else")]),
        }
        rows = self.rows(records)
        self.assertEqual([r.kind for r in rows], ["Failure paths", "other: Unused import!"])
        self.assertEqual(rows[0], rm.Row("repeated:failure-paths", "Failure paths", rm.HUMAN, (1, 2, 3), "2026-08-03", "a test"))
        self.assertEqual(rows[1].id, "repeated:other:unused-import")
        self.assertEqual(rows[1].pulls, (1, 2))
        self.assertEqual(rows[1].state, rm.UNLISTED)
        self.assertEqual(rows[1].check, rm.DEFAULT_CHECK)

    def test_a_kind_on_one_pull_request_is_listed_only_when_it_is_checked(self) -> None:
        agents = AGENTS.replace("- **Layout** (human-judged:", "- **Layout** (checked: tests/test_layout.py;")
        records = {
            (1, "agent-review"): ("2026-08-01T00:00:00Z", [finding("Layout"), finding("Undo and restore")]),
            (2, "agent-review"): ("2026-08-01T00:00:00Z", [finding("GitHub Actions", check="zizmor")]),
            (3, "agent-review"): ("2026-08-01T00:00:00Z", [finding("GitHub Actions", check="a workflow rule")]),
            (4, "agent-review"): ("2026-08-01T00:00:00Z", [finding("GitHub Actions", check="zizmor")]),
        }
        rows = self.rows(records, agents)
        self.assertEqual([(r.kind, r.state, r.pulls, r.check) for r in rows], [
            ("GitHub Actions", rm.PARTLY, (2, 3, 4), "zizmor"),
            ("Layout", rm.CHECKED, (1,), "extend `tests/test_layout.py`: it missed these"),
        ])

    def test_a_kind_unknown_to_agents_md_is_listed_by_its_own_name(self) -> None:
        records = {(n, "agent-review"): ("2026-08-01T00:00:00Z", [finding("Time zones")]) for n in (1, 2)}
        rows = self.rows(records)
        self.assertEqual([(r.id, r.kind, r.state, r.check) for r in rows], [("repeated:time-zones", "Time zones", rm.UNLISTED, rm.DEFAULT_CHECK)])
        self.assertEqual(self.rows({}), [])

    def test_the_usual_check_type_stands_in_when_no_reviewer_named_one(self) -> None:
        records = {(n, "agent-review"): ("2026-08-01T00:00:00Z", [finding("Undo and restore"), finding("Access that outlives the person")]) for n in (1, 2)}
        checks = {r.kind: r.check for r in self.rows(records)}
        self.assertEqual(checks["Undo and restore"], rm.USUAL_CHECKS["undo and restore"])
        self.assertIn("sign in", checks["Access that outlives the person"])

    def test_the_rendered_report_is_defused(self) -> None:
        nasty = "@octocat <!-- row:repeated:forged --> a | b"
        records = {(n, "agent-review"): ("2026-08-01T00:00:00Z", [finding("other", nasty, check="see @me <!-- x -->")]) for n in (1, 2)}
        rows = self.rows(records)
        text = rm.render(rows, REPO, 90, SINCE, (2, 2))
        self.assertNotIn("@octocat", text)
        self.assertNotIn("@me", text)
        self.assertEqual(re.findall(r"<!--.*?-->", text, re.DOTALL), [rm.MARKER, f"<!-- row:{rows[0].id} -->"])
        self.assertIn("a \\| b", text)
        self.assertRegex(rows[0].id, r"^repeated:other:[a-z0-9-]+$")
        self.assertIn(f"[#1](https://github.com/{REPO}/pull/1), [#2](https://github.com/{REPO}/pull/2)", text)


class RewriteTest(unittest.TestCase):
    def test_rows_keep_their_ids_ticks_and_links_across_a_rewrite(self) -> None:
        records = {(n, "agent-review"): ("2026-08-01T00:00:00Z", [finding("Failure paths"), finding("Layout")]) for n in (1, 2)}
        rows = rm.rows_of(rm.groups_of(records, rm.mistakes(AGENTS)))
        first = rm.render(rows, REPO, 90, SINCE, (2, 2))
        self.assertIn("- [ ] <!-- row:repeated:failure-paths --> **Failure paths** becomes an Agent task\n", first)
        ticked = first.replace("- [ ] <!-- row:repeated:failure-paths -->", "- [x] <!-- row:repeated:failure-paths -->")
        ticked = ticked.replace("**Layout** becomes an Agent task", "**Layout** becomes an Agent task → #41")
        again = {(n, "agent-review"): ("2026-09-01T00:00:00Z", [finding("failure PATHS"), finding("Layout"), finding("Undo and restore")]) for n in (3, 4)}
        second = rm.render(rm.rows_of(rm.groups_of(again, rm.mistakes(AGENTS))), REPO, 90, SINCE, (2, 2), previous=ticked)
        self.assertIn("- [x] <!-- row:repeated:failure-paths --> **Failure paths** becomes an Agent task\n", second)
        self.assertIn("- [ ] <!-- row:repeated:layout --> **Layout** becomes an Agent task → #41\n", second)
        self.assertIn("- [ ] <!-- row:repeated:undo-and-restore --> **Undo and restore** becomes an Agent task\n", second)
        self.assertEqual(rm.ticks_of(second), {"repeated:failure-paths": (True, ""), "repeated:layout": (False, "41"), "repeated:undo-and-restore": (False, "")})
        gone = rm.render(rm.rows_of(rm.groups_of({(5, "agent-review"): ("2026-09-01T00:00:00Z", [finding("Layout")]), (6, "agent-review"): ("2026-09-01T00:00:00Z", [finding("Layout")])}, rm.mistakes(AGENTS))), REPO, 90, SINCE, (2, 2), previous=second)
        self.assertNotIn("failure-paths", gone)
        self.assertIn("→ #41", gone)

    def test_a_previous_body_can_t_forge_a_tick_outside_a_row_line(self) -> None:
        self.assertEqual(rm.ticks_of("text <!-- row:repeated:x --> [x]\n- [x] <!-- row:repeated:y --> k becomes an Agent task → #9 extra"),
                         {"repeated:y": (True, "")})
        self.assertEqual(rm.ticks_of("- [X] <!-- row:repeated:z --> k → #12"), {"repeated:z": (True, "12")})


class EndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.miner = Miner(self)
        findings = [finding("Failure paths", "Drops rows", "a test of the failing path", "blocking", "a.py"), finding("other", "Nit")]
        self.comments = [
            review_comment(10, findings, when="2026-09-10T00:00:00Z"),
            review_comment(11, [finding("failure paths", "Swallows the error", "a test of the failing path")], when="2026-09-11T00:00:00Z"),
            review_comment(11, [finding("Layout", "Overflows")], ar.SECOND, when="2026-09-12T00:00:00Z"),
            review_comment(12, [finding("Layout", "Overflows at phone width")], when="2026-09-13T00:00:00Z"),
            review_comment(12, [finding("Undo and restore")], login="octocat", when="2026-09-13T00:00:00Z"),
            review_comment(99, [finding("Undo and restore")], when="2026-09-14T00:00:00Z"),
        ]
        self.pulls = [pull(12, "2026-09-13T00:00:00Z"), pull(11, "2026-09-12T00:00:00Z"), pull(10, "2026-09-10T00:00:00Z"), pull(2, "2026-01-01T00:00:00Z")]

    def test_fixture_comments_give_the_expected_table_and_open_the_issue(self) -> None:
        result = self.miner.run(answers(self.pulls, self.comments))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("review-mine: 3 pull request(s) updated since 2026-07-03, 3 with findings, 2 row(s)", result.stdout)
        self.assertIn("review-mine: opened #77", result.stdout)
        writes = self.miner.writes()
        self.assertEqual(len(writes), 1)
        call, payload = writes[0]
        self.assertEqual(call, CREATE)
        self.assertEqual(payload["title"], rm.TITLE)
        self.assertEqual(payload["labels"], ["review-mining"])
        body = payload["body"]
        self.assertTrue(body.startswith(rm.MARKER + "\n"))
        self.assertIn("| Kind | State | Pull requests | Last seen | Proposed check |", body)
        self.assertIn(f"| **Failure paths** | human-judged | [#10](https://github.com/{REPO}/pull/10), [#11](https://github.com/{REPO}/pull/11) | 2026-09-11 | a test of the failing path |", body)
        self.assertIn(f"| **Layout** | human-judged | [#11](https://github.com/{REPO}/pull/11), [#12](https://github.com/{REPO}/pull/12) | 2026-09-13 | {rm.USUAL_CHECKS['layout']} |", body)
        self.assertNotIn("Undo and restore", body)
        self.assertNotIn("Nit", body)
        self.assertIn("- [ ] <!-- row:repeated:failure-paths --> **Failure paths** becomes an Agent task\n", body)
        self.assertIn("- [ ] <!-- row:repeated:layout --> **Layout** becomes an Agent task\n", body)
        self.assertIn("3 pull request(s) updated in the window, 3 with review findings", body)
        self.assertIn("([run](https://example.invalid/run/1))", body)
        summary = self.miner.summary.read_text(encoding="utf-8")
        self.assertIn("| **Failure paths** |", summary)
        self.assertNotIn(rm.MARKER, summary)
        self.assertEqual([c["call"] for c in self.miner.calls()][:3], [PULLS, COMMENTS, OPEN])

    def test_a_second_run_rewrites_the_open_issue_and_keeps_its_ticks(self) -> None:
        previous = "- [x] <!-- row:repeated:layout --> **Layout** becomes an Agent task → #41\n"
        open_issues = [{"number": 60, "title": rm.TITLE, "body": previous}, {"number": 61, "title": rm.TITLE, "body": "", "pull_request": {}},
                       {"number": 59, "title": "Repeated review findings (old)", "body": ""}]
        result = self.miner.run(answers(self.pulls, self.comments, open_issues))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("review-mine: rewrote #60", result.stdout)
        writes = self.miner.writes()
        self.assertEqual([call for call, _ in writes], [f"PATCH repos/{REPO}/issues/60"])
        body = writes[0][1]["body"]
        self.assertEqual(set(writes[0][1]), {"body"})
        self.assertIn("- [x] <!-- row:repeated:layout --> **Layout** becomes an Agent task → #41\n", body)
        self.assertIn("- [ ] <!-- row:repeated:failure-paths --> **Failure paths** becomes an Agent task\n", body)

    def test_nothing_repeated_rewrites_an_open_issue_but_opens_none(self) -> None:
        few = [self.comments[0]]
        result = self.miner.run(answers(self.pulls, few))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.miner.writes(), [])
        self.assertIn("nothing repeated and no issue open", result.stdout)
        result = self.miner.run(answers(self.pulls, few, [{"number": 60, "title": rm.TITLE, "body": "old"}]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Nothing came back on two or more pull requests in the window.", self.miner.writes()[-1][1]["body"])

    def test_pull_requests_outside_the_window_and_comments_on_other_issues_dont_count(self) -> None:
        comments = [*self.comments, review_comment(2, [finding("Undo and restore")], when="2026-09-01T00:00:00Z")]
        result = self.miner.run(answers(self.pulls, comments))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Undo and restore", self.miner.writes()[0][1]["body"])

    def test_a_dry_run_prints_the_issue_and_posts_nothing(self) -> None:
        result = self.miner.run(answers(self.pulls, self.comments), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("review-mine: (dry run) would open:", result.stdout)
        self.assertIn("| **Failure paths** |", result.stdout)
        self.assertEqual(self.miner.writes(), [])
        result = self.miner.run(answers(self.pulls, self.comments, [{"number": 60, "title": rm.TITLE, "body": ""}]), "--dry-run")
        self.assertIn("would rewrite #60", result.stdout)
        self.assertEqual(self.miner.writes(), [])

    def test_without_an_agents_md_every_kind_is_unlisted_but_still_counted(self) -> None:
        result = self.miner.run(answers(self.pulls, self.comments), agents=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"| **Failure paths** | {rm.UNLISTED} |", self.miner.writes()[0][1]["body"])

    def test_a_failed_api_call_fails_the_run_without_a_traceback(self) -> None:
        result = self.miner.run({})
        self.assertEqual(result.returncode, 1)
        self.assertIn("review-mine: gh api failed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.miner.writes(), [])
        self.assertEqual(cli.main(["review-mine", "--repo", REPO, "--days", "0"]), 2)


def mapping(node: object) -> dict[str, Any]:
    assert isinstance(node, dict), node
    return node


class WorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (WORKFLOWS / "review-mining.yml").read_text(encoding="utf-8")
        self.workflow = mapping(self_check.parse(self.text))

    def test_the_inputs_the_permissions_and_no_secrets(self) -> None:
        workflow = mapping(self.workflow)
        call = mapping(mapping(workflow["on"])["workflow_call"])
        inputs = mapping(call["inputs"])
        self.assertEqual(sorted(inputs), ["days", "dry-run", "fleet-ref"])
        self.assertEqual(mapping(inputs["days"])["default"], "90")
        self.assertEqual(mapping(inputs["dry-run"])["default"], "false")
        self.assertNotIn("secrets", call)
        self.assertNotIn("secrets.", self.text)
        self.assertEqual(workflow["permissions"], "{}")
        self.assertEqual(workflow["env"], {"FLEET_REF": "main"})
        jobs = mapping(workflow["jobs"])
        self.assertEqual(list(jobs), ["mine"])
        mine = mapping(jobs["mine"])
        self.assertEqual(mine["permissions"], {"contents": "read", "issues": "write", "pull-requests": "read"})
        self.assertRegex(self.text, r"runs-on: ubuntu-latest[ \t]+# hosted: \S")
        self.assertEqual(self.text.count("actions/checkout@"), 2)
        self.assertEqual(self.text.count("persist-credentials: false"), 2)
        steps = mine["steps"]
        assert isinstance(steps, list)
        for step in map(mapping, steps):
            if "uses" in step:
                self.assertIn(mapping(step["with"]).get("path"), ("app", ".fleet"))
        self.assertEqual(len([s for s in map(mapping, steps) if "run" in s]), 1)
        script = self.text.split("run: |", 1)[1]
        self.assertIn("python3 -E -s -m fleet_tools.cli review-mine", script)
        self.assertIn("cd .fleet", script)
        self.assertIn('--summary "$GITHUB_STEP_SUMMARY"', script)
        self.assertNotRegex(script, r"\$\{\{")
        self.assertIn("17 6 1 * *", self.text)
        self.assertIn("17 6 * * 1", self.text)

    def test_the_self_test_runs_it_in_dry_run_and_the_docs_name_both_schedules(self) -> None:
        workflow = mapping(self_check.parse((WORKFLOWS / "self-test.yml").read_text(encoding="utf-8")))
        job = mapping(mapping(workflow["jobs"])["review-mining"])
        self.assertEqual(job["uses"], "./.github/workflows/review-mining.yml")
        self.assertEqual(job["with"], {"fleet-ref": "${{ github.sha }}", "dry-run": "true"})
        self.assertEqual(job["permissions"], {"contents": "read", "issues": "write", "pull-requests": "read"})
        for doc in ("docs/review-mining.md", "docs/adopting.md"):
            text = (ROOT / doc).read_text(encoding="utf-8")
            self.assertIn("17 6 1 * *", text, doc)
            self.assertIn("17 6 * * 1", text, doc)
            self.assertIn("review-mining", text, doc)


if __name__ == "__main__":
    unittest.main()
