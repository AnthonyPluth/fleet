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
from fleet_tools import (
    cli,
    loop_health,
    mining_tasks,
    release,
    review_outcomes,
    self_check,
)
from fleet_tools import review_mine as rm

ROOT = Path(__file__).resolve().parent.parent
FAKES = ROOT / "tests" / "fakes" / "security"
RUNWAY = ROOT / "tests" / "fixtures" / "consumer-runway"
WORKFLOWS = ROOT / ".github" / "workflows"
REPO = "example/app"
NOW = "2026-10-01T00:00:00Z"
SINCE = "2026-07-03T00:00:00Z"
PULLS = f"GET repos/{REPO}/pulls?state=all&sort=updated&direction=desc&per_page=100&page=1"
COMMENTS = f"GET repos/{REPO}/issues/comments?since={SINCE}&per_page=100"
OPEN = f"GET repos/{REPO}/issues?state=open&labels=review-mining&per_page=100"
MINED = f"GET repos/{REPO}/issues?state=all&labels=review-mining&per_page=100"
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


def pull(number: int, updated: str = "2026-09-01T00:00:00Z", merged: str | None = None, body: str | None = None) -> dict[str, Any]:
    return {"number": number, "updated_at": updated, "merged_at": merged, "body": body}


def outcome(kind: str, title: str, status: str = "open", file: str = "") -> dict[str, Any]:
    entry = {"id": ar.finding_id(kind, file, title), "status": "resolved" if status == "resolved" else "open", "kind": kind,
             "file": file, "title": title, "first": "a" * 40}
    if status == "resolved":
        entry["resolved"] = "b" * 40
    return entry


def outcomes_comment(number: int, entries: list[dict[str, Any]], review: ar.Review = ar.FIRST,
                     when: str = "2026-09-01T00:00:00Z") -> dict[str, Any]:
    findings = [finding(e["kind"], e["title"], file=e["file"]) for e in entries]
    lines = ar.render(findings, "summary", "run", "a" * 40, review).splitlines()
    body = "\n".join([*lines[:3], review_outcomes.encode(entries, review), *lines[3:]])
    return review_comment(number, findings, review, when, body=body)


def dismissal(number: int, kind: str, title: str, reason: str, file: str = "", association: str = "OWNER",
              url: str | None = None) -> dict[str, Any]:
    return {"user": {"login": "owner"}, "author_association": association, "body": f"/fleet dismiss f:{ar.finding_id(kind, file, title)} {reason}",
            "html_url": url if url is not None else f"https://github.com/{REPO}/pull/{number}#issuecomment-{number}00",
            "updated_at": "2026-09-02T00:00:00Z", "created_at": "2026-09-02T00:00:00Z",
            "issue_url": f"https://api.github.com/repos/{REPO}/issues/{number}"}


def record(*rows: tuple[str, str, str, str]) -> str:
    table = "\n".join(f"| {d} | {s} | {h} | {e} |" for d, s, h, e in rows)
    return f"## Verification\n\n<!-- fleet-verification -->\n| Dimension | Status | How | Evidence |\n| --- | --- | --- | --- |\n{table}\n<!-- /fleet-verification -->\n"


def files(*names: str) -> list[dict[str, Any]]:
    return [{"filename": name} for name in names]


def history(number: int, page: int = 1) -> str:
    return f"GET repos/{REPO}/issues/{number}/comments?per_page=100&page={page}"


def answers(pulls: list[dict[str, Any]], comments: list[dict[str, Any]], open_issues: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {PULLS: pulls, COMMENTS: comments, OPEN: open_issues or [], MINED: [], CREATE: {"number": 77},
                            f"PATCH repos/{REPO}/issues/60": {"number": 60}}
    for comment in comments:
        number = rm.issue_number(comment)
        if number is not None:
            data.setdefault(history(number), []).append(comment)
    for each in pulls:
        data.setdefault(history(each["number"]), [])
    return data


class Miner:
    def __init__(self, test: unittest.TestCase) -> None:
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.answers = self.directory / "answers.json"
        self.log = self.directory / "log.jsonl"
        self.summary = self.directory / "summary.md"

    def body(self) -> str:
        writes = self.writes()
        assert writes, "nothing was posted"
        return str(writes[-1][1]["body"])

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
        self.assertEqual(sorted(items), ["access that outlives the person", "failure paths", "github actions", "layout", "undo and restore",
                                         "verification that doesn t cover the change"])
        self.assertEqual(items["verification that doesn t cover the change"].state, rm.PARTLY)
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
        self.assertEqual(parsed[1], [{"severity": "advisory", "kind": "Failure paths", "title": "Drops rows", "file": "", "check": "a test", "id": ar.finding_id("Failure paths", "", "Drops rows")}])
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

    def test_the_links_mining_tasks_writes_survive_a_rewrite_and_go_with_a_row_that_disappears(self) -> None:
        records = {(n, "agent-review"): ("2026-08-01T00:00:00Z", [finding("Failure paths"), finding("Layout")]) for n in (1, 2)}
        first = rm.render(rm.rows_of(rm.groups_of(records, rm.mistakes(AGENTS))), REPO, 90, SINCE, (2, 2)).replace("\n", "\r\n")
        ticked = first.replace("- [ ] <!-- row:repeated:failure-paths -->", "- [x] <!-- row:repeated:failure-paths -->")
        ticked = ticked.replace("- [ ] <!-- row:repeated:layout -->", "- [x] <!-- row:repeated:layout -->")
        previous = mining_tasks.linked(ticked, {"repeated:failure-paths": 81, "repeated:layout": 82})
        self.assertEqual(rm.ticks_of(previous), {"repeated:failure-paths": (True, "81"), "repeated:layout": (True, "82")})
        again = {(n, "agent-review"): ("2026-09-01T00:00:00Z", [finding("Layout"), finding("Undo and restore")]) for n in (3, 4)}
        second = rm.render(rm.rows_of(rm.groups_of(again, rm.mistakes(AGENTS))), REPO, 90, SINCE, (2, 2), previous=previous)
        self.assertIn("- [x] <!-- row:repeated:layout --> **Layout** becomes an Agent task → #82\n", second)
        self.assertEqual(rm.ticks_of(second), {"repeated:layout": (True, "82"), "repeated:undo-and-restore": (False, "")})
        self.assertNotIn("#81", second)
        back = rm.render(rm.rows_of(rm.groups_of(records, rm.mistakes(AGENTS))), REPO, 90, SINCE, (2, 2), previous=second)
        self.assertIn("- [ ] <!-- row:repeated:failure-paths --> **Failure paths** becomes an Agent task\n", back)
        self.assertEqual(mining_tasks.pending(back.replace("- [ ] <!-- row:repeated:failure-paths -->", "- [x] <!-- row:repeated:failure-paths -->")),
                         ["repeated:failure-paths"])
        restored = rm.render(rm.rows_of(rm.groups_of(records, rm.mistakes(AGENTS))), REPO, 90, SINCE, (2, 2), previous=second,
                             markers={"repeated:failure-paths": 81, "repeated:layout": 99})
        self.assertEqual(rm.ticks_of(restored), {"repeated:failure-paths": (True, "81"), "repeated:layout": (True, "82")})

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
        self.assertIn("review-mine: 3 pull request(s) updated since 2026-07-03, 3 with findings, 0 merged, 2 row(s)", result.stdout)
        self.assertIn("review-mine: opened #77", result.stdout)
        writes = self.miner.writes()
        self.assertEqual(len(writes), 1)
        call, payload = writes[0]
        self.assertEqual(call, CREATE)
        self.assertEqual(payload["title"], rm.TITLE)
        self.assertEqual(payload["labels"], ["review-mining"])
        body = payload["body"]
        self.assertTrue(body.startswith(rm.MARKER + "\n"))
        self.assertIn("| Kind | State | Pull requests | Fixed | Last seen | Proposed check |", body)
        self.assertIn(f"| **Failure paths** | human-judged | [#10](https://github.com/{REPO}/pull/10), [#11](https://github.com/{REPO}/pull/11) | 0 | 2026-09-11 | a test of the failing path |", body)
        self.assertIn(f"| **Layout** | human-judged | [#11](https://github.com/{REPO}/pull/11), [#12](https://github.com/{REPO}/pull/12) | 0 | 2026-09-13 | {rm.USUAL_CHECKS['layout']} |", body)
        self.assertIn("No kind was mostly dismissed in the window.", body)
        self.assertIn("No finding was open at merge in the window.", body)
        self.assertNotIn("### Verification gaps", body)
        self.assertNotIn("Undo and restore", body)
        self.assertNotIn("Nit", body)
        self.assertIn("- [ ] <!-- row:repeated:failure-paths --> **Failure paths** becomes an Agent task\n", body)
        self.assertIn("- [ ] <!-- row:repeated:layout --> **Layout** becomes an Agent task\n", body)
        self.assertIn("3 pull request(s) updated in the window, 3 with review findings", body)
        self.assertIn("([run](https://example.invalid/run/1))", body)
        summary = self.miner.summary.read_text(encoding="utf-8")
        self.assertIn("| **Failure paths** |", summary)
        self.assertNotIn(rm.MARKER, summary)
        calls = [c["call"] for c in self.miner.calls()]
        self.assertEqual([c for c in calls if not re.search(r"/issues/\d+/comments", c)][:4], [PULLS, COMMENTS, MINED, OPEN])
        self.assertEqual(sorted(c for c in calls if re.search(r"/issues/\d+/comments", c)), [history(10), history(11), history(12)])

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

    def test_a_link_lost_from_the_body_comes_back_from_the_bot_s_mined_from_marker(self) -> None:
        open_issues = [{"number": 60, "title": rm.TITLE, "body": "- [ ] <!-- row:repeated:failure-paths --> **Failure paths** becomes an Agent task\n"}]
        data = answers(self.pulls, self.comments, open_issues)
        data[MINED] = [{"number": 81, "user": {"login": rm.BOT}, "body": "x\n<!-- mined-from row:repeated:failure-paths -->\n"},
                       {"number": 82, "user": {"login": "someone"}, "body": "<!-- mined-from row:repeated:layout -->"}]
        result = self.miner.run(data)
        self.assertEqual(result.returncode, 0, result.stderr)
        body = self.miner.body()
        self.assertIn("- [x] <!-- row:repeated:failure-paths --> **Failure paths** becomes an Agent task → #81\n", body)
        self.assertIn("- [ ] <!-- row:repeated:layout --> **Layout** becomes an Agent task\n", body)

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


class SignalsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.miner = Miner(self)

    def test_dismissed_findings_don_t_count_towards_a_repeat_and_fixed_ones_are_counted(self) -> None:
        comments = [
            outcomes_comment(1, [outcome("Layout", "Overflows"), outcome("Failure paths", "Drops rows", "resolved")]),
            outcomes_comment(2, [outcome("Layout", "Overflows again"), outcome("Failure paths", "Swallows it")]),
            dismissal(2, "Layout", "Overflows again", "the page is desktop-only by design"),
        ]
        result = self.miner.run(answers([pull(1), pull(2)], comments))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = self.miner.body()
        self.assertIn(f"| **Failure paths** | human-judged | [#1](https://github.com/{REPO}/pull/1), [#2](https://github.com/{REPO}/pull/2) | 1 |", body)
        self.assertNotIn("**Layout**", body)

    def test_groups_come_from_the_outcomes_history_written_by_finish(self) -> None:
        def review(number: int, sha: str, findings: list[dict[str, Any]], earlier: list[dict[str, Any]]) -> dict[str, Any]:
            history = [{"user": ar.BOT, "body": c["body"]} for c in earlier]
            body = review_outcomes.finish(ar.render(findings, "summary", "run", sha), history, sha, True)
            return review_comment(number, findings, when=f"2026-09-0{number}T00:00:00Z", body=body)

        layout = finding("Layout", "Overflows", "screenshots", file="a.py")
        failure = finding("Failure paths", "Swallows the error", file="b.py")
        comments: list[dict[str, Any]] = []
        for number in (1, 2):
            first = review(number, "a" * 40, [layout, failure], [])
            fixed = review(number, "b" * 40, [failure], [first])
            comments.append(fixed)
        third = review(3, "a" * 40, [layout], [])
        comments += [third, dismissal(3, "Layout", "Overflows", "desktop-only page by design", file="a.py")]
        records = rm.newest_records(comments, {1, 2, 3})
        self.assertEqual([f["kind"] for f in records[(1, "agent-review")][1]], ["Failure paths"])
        result = self.miner.run(answers([pull(1, merged="2026-09-05T00:00:00Z"), pull(2), pull(3)], comments))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = self.miner.body()
        self.assertIn(f"| **Layout** | human-judged | [#1](https://github.com/{REPO}/pull/1), [#2](https://github.com/{REPO}/pull/2) | 2 |", body)
        self.assertNotIn("pull/3", body.split("### Often dismissed")[0])
        self.assertIn(f"| **Failure paths** | human-judged | [#1](https://github.com/{REPO}/pull/1), [#2](https://github.com/{REPO}/pull/2) | 0 |", body)
        self.assertIn(f"| **Failure paths** | 1 | [#1](https://github.com/{REPO}/pull/1) |", body)
        self.assertNotIn("open-at-merge:layout", body)

    def test_a_finding_dismissed_after_the_merge_was_still_open_at_merge(self) -> None:
        comments = [outcomes_comment(n, [outcome("Layout", f"t{n}")]) for n in (1, 2, 3)]
        comments += [dismissal(n, "Layout", f"t{n}", "dismissed once it had merged") for n in (1, 2, 3)]
        pulls = [pull(n, merged="2026-09-01T12:00:00Z") for n in (1, 2, 3)]
        result = self.miner.run(answers(pulls, comments))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = self.miner.body()
        self.assertIn(f"| **Layout** | 3 | [#1](https://github.com/{REPO}/pull/1), [#2](https://github.com/{REPO}/pull/2), [#3](https://github.com/{REPO}/pull/3) |", body)
        self.assertIn("| **Layout** | 3 | 0 | 3 |", body)
        self.assertNotIn("row:repeated:layout", body)
        late = [{**c, "created_at": "2026-09-03T00:00:00Z"} if c["user"]["login"] == rm.BOT else c for c in comments]
        self.miner.run(answers(pulls, late))
        self.assertNotIn("row:open-at-merge:layout", self.miner.body())

    def test_a_review_comment_edited_after_the_merge_leaves_the_pull_request_out_of_open_at_merge(self) -> None:
        merged = "2026-09-01T12:00:00Z"
        edited = {**outcomes_comment(1, [outcome("Layout", "t1")]), "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-02T00:00:00Z"}
        unedited = {**outcomes_comment(2, [outcome("Layout", "t2")]), "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-01T06:00:00Z"}
        late = dismissal(2, "Layout", "t2", "dismissed once it had merged")
        pulls = [pull(1, merged=merged), pull(2, merged=merged)]
        result = self.miner.run(answers(pulls, [edited, unedited, late]))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = self.miner.body()
        self.assertIn(f"| **Layout** | 1 | [#2](https://github.com/{REPO}/pull/2) |", body)
        self.assertIn("1 merged pull request(s) whose review or dismissal comments were posted or changed at or after the merge instant were skipped for Open at merge.", body)
        self.assertNotIn("#1](", body.split("### Open at merge", 1)[1])

    def test_a_pull_request_reviewed_before_the_window_and_merged_in_it_is_open_at_merge(self) -> None:
        def old(number: int, entries: list[dict[str, Any]]) -> dict[str, Any]:
            return {**outcomes_comment(number, entries), "created_at": "2026-05-01T00:00:00Z", "updated_at": "2026-05-01T00:00:00Z"}

        def old_dismissal(number: int, title: str) -> dict[str, Any]:
            return {**dismissal(number, "Layout", title, "not a finding on this page"), "created_at": "2026-05-02T00:00:00Z",
                    "updated_at": "2026-05-02T00:00:00Z"}

        data = answers([pull(5, merged="2026-09-30T00:00:00Z"), pull(6, merged="2026-09-30T00:00:00Z")], [])
        for number in (5, 6):
            entries = [outcome("Failure paths", f"Swallows the error {number}")] + [outcome("Layout", f"d{number}{i}") for i in range(2)]
            data[history(number)] = [old(number, entries), *(old_dismissal(number, f"d{number}{i}") for i in range(2))]
        result = self.miner.run(data)
        self.assertEqual(result.returncode, 0, result.stderr)
        body = self.miner.body()
        self.assertIn(f"| **Failure paths** | 2 | [#5](https://github.com/{REPO}/pull/5), [#6](https://github.com/{REPO}/pull/6) |", body)
        self.assertIn("Nothing came back on two or more pull requests in the window.", body)
        self.assertIn("No kind was mostly dismissed in the window.", body)
        self.assertNotIn("row:repeated:", body)
        self.assertNotIn("row:dismissed:", body)

    def test_an_owner_comment_edited_after_the_merge_leaves_the_pull_request_out_of_open_at_merge(self) -> None:
        merged = "2026-09-01T12:00:00Z"
        comments = [outcomes_comment(n, [outcome("Layout", f"t{n}"), outcome("Failure paths", f"f{n}")]) for n in (1, 2, 3)]
        edited = {**dismissal(1, "Layout", "t1", "not a finding on this page"), "created_at": "2026-09-01T06:00:00Z", "updated_at": "2026-09-02T00:00:00Z"}
        unedited = {**dismissal(2, "Layout", "t2", "not a finding on this page"), "created_at": "2026-09-01T06:00:00Z", "updated_at": "2026-09-01T06:00:00Z"}
        edited_out = {"user": {"login": "owner"}, "author_association": "OWNER", "body": "never mind", "html_url": "",
                      "created_at": "2026-09-01T06:00:00Z", "updated_at": "2026-09-03T00:00:00Z",
                      "issue_url": f"https://api.github.com/repos/{REPO}/issues/3"}
        pulls = [pull(n, merged=merged) for n in (1, 2, 3)]
        result = self.miner.run(answers(pulls, [*comments, edited, unedited, edited_out]))
        self.assertEqual(result.returncode, 0, result.stderr)
        section = self.miner.body().split("### Open at merge", 1)[1]
        self.assertIn(f"| **Failure paths** | 1 | [#2](https://github.com/{REPO}/pull/2) |", section)
        self.assertNotIn("**Layout**", section)
        self.assertIn("2 merged pull request(s) whose review or dismissal comments were posted or changed at or after the merge instant were skipped for Open at merge.", section)

    def test_a_dismissal_in_the_same_second_as_the_merge_is_indeterminate(self) -> None:
        merged = "2026-09-01T12:00:00Z"
        comments = [outcomes_comment(n, [outcome("Layout", f"t{n}"), outcome("Failure paths", f"f{n}")]) for n in (1, 2, 3)]
        same = {**dismissal(1, "Layout", "t1", "not a finding on this page"), "created_at": merged, "updated_at": merged}
        earlier = {**dismissal(2, "Layout", "t2", "not a finding on this page"), "created_at": "2026-09-01T11:59:59Z", "updated_at": "2026-09-01T11:59:59Z"}
        touched = {**dismissal(3, "Layout", "t3", "not a finding on this page"), "created_at": "2026-09-01T11:59:59Z", "updated_at": merged}
        pulls = [pull(n, merged=merged) for n in (1, 2, 3)]
        result = self.miner.run(answers(pulls, [*comments, same, earlier, touched]))
        self.assertEqual(result.returncode, 0, result.stderr)
        section = self.miner.body().split("### Open at merge", 1)[1]
        self.assertIn(f"| **Failure paths** | 1 | [#2](https://github.com/{REPO}/pull/2) |", section)
        self.assertNotIn("**Layout**", section)
        self.assertIn("2 merged pull request(s) whose review or dismissal comments were posted or changed at or after the merge instant were skipped", section)

    def test_kinds_that_differ_only_after_sixty_characters_stay_apart(self) -> None:
        stem = "Unused helper " + "x" * 50
        first, second = f"{stem} in the parser", f"{stem} in the writer"
        self.assertEqual(rm.normalize(first)[:60], rm.normalize(second)[:60])
        comments = [outcomes_comment(n, [outcome("other", first), outcome("other", second)]) for n in (1, 2)]
        pulls = [pull(1, merged="2026-09-01T12:00:00Z"), pull(2, merged="2026-09-01T12:00:00Z")]

        def ids(data_pulls: list[dict[str, Any]], data_comments: list[dict[str, Any]]) -> list[str]:
            self.miner.run(answers(data_pulls, data_comments))
            return sorted(rm.ticks_of(self.miner.body()))

        found = ids(pulls, comments)
        self.assertEqual(len([i for i in found if i.startswith("open-at-merge:other:")]), 2, found)
        self.assertEqual(len([i for i in found if i.startswith("repeated:other:")]), 2, found)
        self.assertTrue(all(re.fullmatch(r"[a-z0-9:-]+", i) and len(i.split(":", 2)[2]) <= 60 for i in found), found)
        self.assertEqual(ids(list(reversed(pulls)), list(reversed(comments))), found)
        dismissed = [outcomes_comment(3, [outcome("other", first, file="a.py"), outcome("other", first, file="b.py"), outcome("other", second)]),
                     dismissal(3, "other", first, "not a finding on this page", "a.py"),
                     dismissal(3, "other", first, "not a finding on this page", "b.py"),
                     dismissal(3, "other", second, "not a finding on this page")]
        result = self.miner.run(answers([pull(3)], dismissed), "--dry-run")
        self.assertIn("No kind was mostly dismissed in the window.", result.stdout)
        self.assertEqual(rm.slug("Failure paths"), "failure-paths")
        self.assertEqual(rm.slug("a" * 60), "a" * 60)
        self.assertNotEqual(rm.slug("a" * 61), rm.slug("a" * 60 + "b"))

    def test_a_dismissal_older_than_the_window_still_counts(self) -> None:
        old = {**dismissal(2, "Layout", "Overflows again", "the page is desktop-only by design"),
               "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}
        comments = [outcomes_comment(1, [outcome("Layout", "Overflows")]), outcomes_comment(2, [outcome("Layout", "Overflows again")])]
        data = answers([pull(1), pull(2, merged="2026-09-01T12:00:00Z")], comments)
        data[history(2)].insert(0, old)
        result = self.miner.run(data, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("**Layout**", result.stdout)
        self.assertNotIn("row:open-at-merge:layout", result.stdout)
        self.assertIn("would open nothing", result.stdout)

    def long_history(self, extra: int) -> dict[str, Any]:
        comments = [outcomes_comment(n, [outcome("Layout", f"t{n}")]) for n in (1, 2)]
        data = answers([pull(1, merged="2026-09-01T12:00:00Z"), pull(2, merged="2026-09-01T12:00:00Z")], comments)
        filler = {"user": {"login": "someone"}, "author_association": "NONE", "body": "thanks", "created_at": "2026-08-01T00:00:00Z"}
        data[history(2, 1)] = [comments[1]] + [filler] * 99
        for page in range(2, rm.MAX_COMMENT_PAGES + 1):
            data[history(2, page)] = [filler] * 100
        data[history(2, rm.MAX_COMMENT_PAGES + 1)] = [filler] * extra
        return data

    def test_a_history_of_exactly_the_cap_is_read(self) -> None:
        self.assertEqual(rm.MAX_COMMENT_PAGES * rm.PER_PAGE, 1000)
        result = self.miner.run(self.long_history(0))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = self.miner.body()
        self.assertNotIn("couldn't be read in full", body)
        self.assertIn(f"| **Layout** | 2 | [#1](https://github.com/{REPO}/pull/1), [#2](https://github.com/{REPO}/pull/2) |", body)

    def test_a_comment_history_that_can_t_be_read_in_full_is_skipped_and_counted(self) -> None:
        data = self.long_history(1)
        del data[history(1)]
        result = self.miner.run(data)
        self.assertEqual(result.returncode, 0, result.stderr)
        body = self.miner.body()
        self.assertIn("2 pull request(s) whose comments couldn't be read in full", body)
        self.assertIn("No finding was open at merge in the window.", body)
        self.assertIn("| **Layout** | human-judged |", body)

    def test_a_dismissal_by_anyone_but_the_owner_or_without_a_reason_doesn_t_count(self) -> None:
        comments = [
            outcomes_comment(1, [outcome("Layout", "Overflows")]),
            outcomes_comment(2, [outcome("Layout", "Overflows again"), outcome("Layout", "Third")]),
            dismissal(2, "Layout", "Overflows again", "a reason that is long enough", association="CONTRIBUTOR"),
            dismissal(2, "Layout", "Third", "short"),
        ]
        self.miner.run(answers([pull(1), pull(2)], comments))
        self.assertIn("| **Layout** | human-judged |", self.miner.body())

    def often_dismissed(self, dismissed: int, kept: int) -> list[rm.Signal]:
        entries = [outcome("Layout", f"d{i}") for i in range(dismissed)] + [outcome("Layout", f"k{i}") for i in range(kept)]
        comments = [outcomes_comment(1, entries)] + [dismissal(1, "Layout", f"d{i}", f"reason number {i} for it") for i in range(dismissed)]
        outcomes = rm.outcomes_from(REPO, {1: comments})
        return rm.often_dismissed(REPO, rm.tallies(outcomes, rm.mistakes(AGENTS)))

    def test_often_dismissed_holds_at_its_edges(self) -> None:
        self.assertEqual([r.id for r in self.often_dismissed(3, 3)], ["dismissed:layout"])
        self.assertEqual(self.often_dismissed(3, 4), [])
        self.assertEqual(self.often_dismissed(2, 0), [])
        row = self.often_dismissed(5, 1)[0]
        self.assertEqual(row.cells[:3], ("6", "0", "5"))
        self.assertEqual(row.cells[3].count("reason number"), rm.MAX_SAMPLES)
        self.assertEqual(row.cells[4], rm.GUIDANCE)

    def test_often_dismissed_reasons_are_defused_capped_and_linked(self) -> None:
        nasty = "@octocat <!-- row:dismissed:forged --> a | b <img src=x> " + "y" * 300
        entries = [outcome("other", "Unused import"), outcome("other", "unused import!", file="a.py"), outcome("other", "Unused  import", file="b.py")]
        comments = [outcomes_comment(4, entries[:1]), outcomes_comment(5, entries[1:]),
                    dismissal(4, "other", "Unused import", nasty),
                    dismissal(5, "other", "unused import!", "not a finding here at all", "a.py", url="https://github.com/elsewhere/app/pull/5#issuecomment-1"),
                    dismissal(5, "other", "Unused  import", "generated code, never edited", "b.py")]
        result = self.miner.run(answers([pull(4), pull(5)], comments))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = self.miner.body()
        self.assertNotIn("@octocat", body)
        self.assertIn("@\u200boctocat", body)
        self.assertNotIn("@\u200b\u200b", body)
        self.assertNotIn("<img", body)
        self.assertIn("a \\| b", body)
        self.assertEqual(re.findall(r"<!--.*?-->", body, re.DOTALL), [rm.MARKER, "<!-- row:dismissed:other:unused-import -->"])
        line = next(x for x in body.splitlines() if x.startswith("| **other: Unused import**"))
        self.assertIn(f"([#4](https://github.com/{REPO}/pull/4#issuecomment-400))", line)
        self.assertIn('"not a finding here at all" (#5)', line)
        self.assertNotIn("elsewhere", line)
        self.assertNotIn("y" * (rm.MAX_REASON + 1), line)
        self.assertIn("| 3 | 0 | 3 |", line)

    def test_open_at_merge_lists_only_merged_pull_requests(self) -> None:
        comments = [outcomes_comment(1, [outcome("Layout", "Overflows"), outcome("Failure paths", "Drops rows", "resolved")]),
                    outcomes_comment(2, [outcome("Layout", "Still open")]),
                    outcomes_comment(3, [outcome("Layout", "Open but merged before the window")])]
        pulls = [pull(1, merged="2026-09-01T12:00:00Z"), pull(2), pull(3, merged="2026-01-01T00:00:00Z")]
        self.miner.run(answers(pulls, comments))
        body = self.miner.body()
        self.assertIn(f"| **Layout** | 1 | [#1](https://github.com/{REPO}/pull/1) |", body)
        self.assertIn("- [ ] <!-- row:open-at-merge:layout --> **Layout** becomes an Agent task", body)
        self.assertNotIn("open-at-merge:failure-paths", body)

    def gap_answers(self) -> dict[str, Any]:
        gap = ("Bank sync", "not verified", "no fake provider @octocat <!-- x -->", "-")
        done = ("Totals", "verified", "unit test", "tests/test_budget.py")
        pulls = [
            pull(20, merged="2026-09-01T12:00:00Z", body=record(gap, done)),
            pull(21, merged="2026-09-02T00:00:00Z", body=record(("Chart colours", "not verified", "needs a screenshot flow", "-"))),
            pull(22, merged="2026-09-03T00:00:00Z", body="No record here."),
            pull(23, merged="2026-09-04T00:00:00Z", body="<!-- fleet-verification -->\n| broken | secret-text |\n<!-- /fleet-verification -->"),
            pull(24, body=record(("Open one", "not verified", "OPEN-PR-TEXT", "-"))),
            pull(25, merged="2026-09-05T00:00:00Z", body=record(("Lonely", "not verified", "one pull request only", "-"))),
            pull(26, merged="2026-09-06T00:00:00Z", body=record(done)),
        ]
        data = answers(pulls, [])
        data[f"GET repos/{REPO}/pulls/20/files?per_page=100"] = files("runway/static/app.js", "README.md")
        data[f"GET repos/{REPO}/pulls/21/files?per_page=100"] = [{"filename": "runway/static/new.css", "previous_filename": "old.css"}]
        data[f"GET repos/{REPO}/pulls/24/files?per_page=100"] = files("elsewhere.txt")
        data[f"GET repos/{REPO}/pulls/25/files?per_page=100"] = files("elsewhere.txt")
        return data

    def test_verification_gaps_group_merged_pull_requests_by_area(self) -> None:
        result = self.miner.run(self.gap_answers(), "--app-root", str(RUNWAY))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = self.miner.body()
        self.assertIn("### Verification gaps", body)
        line = next(x for x in body.splitlines() if x.startswith("| **Screens and flows**"))
        self.assertIn(f"[#20](https://github.com/{REPO}/pull/20), [#21](https://github.com/{REPO}/pull/21)", line)
        self.assertIn("Bank sync: no fake provider", line)
        self.assertIn("Chart colours: needs a screenshot flow (#21)", line)
        self.assertIn(rm.GAP_PROPOSAL, line)
        self.assertNotIn("Totals", body)
        self.assertIn("- [ ] <!-- row:gaps:screens --> **Screens and flows** becomes an Agent task", body)
        self.assertNotIn("row:gaps:ungrouped", body)
        self.assertNotIn("| **ungrouped** |", body)
        self.assertIn("1 merged pull request(s) without a verification record (it isn't required).", body)
        self.assertIn("1 verification record(s) that don't parse were skipped.", body)
        self.assertNotIn("secret-text", body)
        self.assertNotIn("OPEN-PR-TEXT", body)
        self.assertNotIn("Lonely", body)
        self.assertNotIn("@octocat", body)
        self.assertEqual(re.findall(r"<!--.*?-->", body, re.DOTALL), [rm.MARKER, "<!-- row:gaps:screens -->"])
        called = [c["call"] for c in self.miner.calls()]
        self.assertNotIn(f"GET repos/{REPO}/pulls/24/files?per_page=100", called)
        self.assertNotIn(f"GET repos/{REPO}/pulls/22/files?per_page=100", called)

    def test_a_file_list_at_github_s_cap_skips_that_pull_request_and_is_counted(self) -> None:
        data = self.gap_answers()
        data[f"GET repos/{REPO}/pulls/21/files?per_page=100"] = [{"filename": f"lib/{i:04}.py"} for i in range(rm.MAX_FILES)]
        result = self.miner.run(data, "--app-root", str(RUNWAY), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        body = result.stdout
        self.assertIn("1 merged pull request(s) whose changed files couldn't be read in full were skipped.", body)
        self.assertNotIn("row:gaps:screens", body)

    def test_a_failing_files_call_skips_that_pull_request_and_is_counted(self) -> None:
        data = self.gap_answers()
        data[f"GET repos/{REPO}/pulls/21/files?per_page=100"] = {"__status__": 502}
        result = self.miner.run(data, "--app-root", str(RUNWAY), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        body = result.stdout
        self.assertIn("1 merged pull request(s) whose changed files couldn't be read in full were skipped.", body)
        self.assertNotIn("row:gaps:screens", body)
        self.assertNotIn("Chart colours", body)
        self.assertNotIn("502", body)

    def test_a_marker_only_inside_a_fenced_example_is_no_record(self) -> None:
        data = self.gap_answers()
        fenced = "Example:\n\n```md\n<!-- fleet-verification -->\n| Dimension | Status | How | Evidence |\n<!-- /fleet-verification -->\n```\n"
        data[PULLS][2]["body"] = fenced
        result = self.miner.run(data, "--app-root", str(RUNWAY), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 merged pull request(s) without a verification record (it isn't required).", result.stdout)
        self.assertIn("1 verification record(s) that don't parse were skipped.", result.stdout)

    def test_guides_whose_names_share_a_long_prefix_are_separate_areas(self) -> None:
        app = self.miner.directory / "long"
        guides = app / "docs" / "verification"
        guides.mkdir(parents=True)
        (app / "pyproject.toml").write_text('[tool.fleet.verification]\nguides = "docs/verification"\n', encoding="utf-8")
        stem = "a" * 60
        names = {f"{stem}-first-area": "one/**", f"{stem}-other-area": "two/**"}
        for name, glob in names.items():
            (guides / f"{name}.md").write_text(f"# {name[-10:]}\n\n## Paths\n\n- `{glob}`\n\n## How to verify\n\n- `make test`\n\n"
                                              "## Not verifiable yet\n\n- Nothing\n", encoding="utf-8")
        gap = ("Thing", "not verified", "no fake for it", "-")
        pulls = [pull(n, merged="2026-09-01T12:00:00Z", body=record(gap)) for n in (30, 31, 32)]
        data = answers(pulls, [])
        data[f"GET repos/{REPO}/pulls/30/files?per_page=100"] = files("one/a.py")
        data[f"GET repos/{REPO}/pulls/31/files?per_page=100"] = files("two/b.py")
        data[f"GET repos/{REPO}/pulls/32/files?per_page=100"] = files("elsewhere.txt")
        result = self.miner.run(data, "--app-root", str(app), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No area had gaps on 2 or more pull requests.", result.stdout)
        self.assertNotIn("row:gaps:", result.stdout)
        data[f"GET repos/{REPO}/pulls/32/files?per_page=100"] = files("one/c.py")
        ids = []
        for _ in range(2):
            result = self.miner.run(data, "--app-root", str(app), "--dry-run")
            ids.append(sorted(i for i in rm.ticks_of(result.stdout.split("\n\n", 1)[1]) if i.startswith("gaps:")))
        self.assertEqual(ids, [[f"gaps:{stem}-first-area"]] * 2)

    def test_ungrouped_gaps_need_two_merged_pull_requests_too(self) -> None:
        data = self.gap_answers()
        data[f"GET repos/{REPO}/pulls/20/files?per_page=100"] = files("elsewhere.txt")
        self.miner.run(data, "--app-root", str(RUNWAY))
        body = self.miner.body()
        self.assertIn(f"| **ungrouped** | [#20](https://github.com/{REPO}/pull/20), [#25](https://github.com/{REPO}/pull/25) |", body)
        self.assertNotIn("**Screens and flows**", body)
        self.assertIn("<!-- row:gaps:ungrouped -->", body)

    def test_no_verification_table_without_the_setting_and_no_count_when_required(self) -> None:
        result = self.miner.run(self.gap_answers(), "--app-root", str(ROOT / "tests" / "fixtures" / "consumer-waypoint"), "--dry-run")
        self.assertIn("would open nothing", result.stdout)
        self.assertNotIn("Verification gaps", result.stdout)
        self.assertEqual([c["call"] for c in self.miner.calls() if "/files" in c["call"]], [])
        app = self.miner.directory / "app"
        (app / "docs" / "verification").mkdir(parents=True)
        for guide in (RUNWAY / "docs" / "verification").glob("*.md"):
            (app / "docs" / "verification" / guide.name).write_text(guide.read_text(encoding="utf-8"), encoding="utf-8")
        (app / "pyproject.toml").write_text('[tool.fleet.verification]\nrequired = true\nguides = "docs/verification"\n', encoding="utf-8")
        self.miner.run(self.gap_answers(), "--app-root", str(app))
        body = self.miner.body()
        self.assertIn("<!-- row:gaps:screens -->", body)
        self.assertNotIn("without a verification record", body)
        (app / "pyproject.toml").write_text("[tool.fleet.verification]\nrequired = false\n", encoding="utf-8")
        self.miner.run(self.gap_answers(), "--app-root", str(app))
        self.assertIn("<!-- row:gaps:ungrouped -->", self.miner.body())

    def test_row_ids_are_stable_across_runs(self) -> None:
        def ids(text: str) -> list[str]:
            return sorted(rm.ticks_of(text))

        comments = [outcomes_comment(n, [outcome("layout", f"t{n}"), outcome("Other", f"Unused import {'!' * n}")]) for n in (1, 2, 3)]
        comments += [dismissal(n, "layout", f"t{n}", "desktop-only by design") for n in (1, 2, 3)]
        pulls = [pull(n, merged="2026-09-01T12:00:00Z") for n in (1, 2, 3)]
        self.miner.run(answers(pulls, comments))
        first = self.miner.body()
        self.miner.run(answers(list(reversed(pulls)), list(reversed(comments))))
        second = self.miner.body()
        self.assertEqual(ids(first), ids(second))
        self.assertEqual(ids(first), ["dismissed:layout", "open-at-merge:layout", "open-at-merge:other:unused-import",
                                      "repeated:other:unused-import"])
        ticked = first.replace("- [ ] <!-- row:dismissed:layout -->", "- [x] <!-- row:dismissed:layout -->")
        rows = rm.rows_of(rm.groups_of({}, {}))
        again = rm.render(rows, REPO, 90, SINCE, (3, 3), ticked, signals=rm.Signals([rm.Signal("dismissed:layout", "Layout", ("3", "0", "3", "r", rm.GUIDANCE))]))
        self.assertIn("- [x] <!-- row:dismissed:layout --> **Layout** becomes an Agent task", again)

    def test_signals_alone_open_the_issue(self) -> None:
        comments = [outcomes_comment(1, [outcome("Layout", "Overflows")])]
        result = self.miner.run(answers([pull(1, merged="2026-09-01T12:00:00Z")], comments), "--dry-run")
        self.assertIn("would open:", result.stdout)
        self.assertIn("<!-- row:open-at-merge:layout -->", result.stdout)


GAP = "[tool.fleet.verification.gap-issues]\n"
REASON = "no fake provider; a fake under `tests/fakes/` would make it verifiable"
TITLE = "Verification gap: no fake provider; a fake under tests/fakes/ would make it verifiable"


def gap_app(directory: Path, table: str = GAP, guides: bool = True, name: str = "gap-app") -> Path:
    app = directory / name
    (app / "docs" / "verification").mkdir(parents=True, exist_ok=True)
    for guide in (RUNWAY / "docs" / "verification").glob("*.md"):
        (app / "docs" / "verification" / guide.name).write_text(guide.read_text(encoding="utf-8"), encoding="utf-8")
    settings = '[tool.fleet.verification]\nrequired = false\n' + ('guides = "docs/verification"\n' if guides else "")
    (app / "pyproject.toml").write_text(f"{settings}\n{table}", encoding="utf-8")
    return app


def merged_pull(number: int, body: str, merged: str = "2026-09-01T12:00:00Z", author: str = "someone") -> dict[str, Any]:
    return {**pull(number, merged=merged, body=body), "user": {"login": author}}


def changed(data: dict[str, Any], number: int, *names: str) -> None:
    data[f"GET repos/{REPO}/pulls/{number}/files?per_page=100"] = files(*names)


def gap_issues(miner: Miner) -> list[dict[str, Any]]:
    return [payload for call, payload in miner.writes() if call == CREATE and str(payload["title"]).startswith("Verification gap: ")]


class GapIssuesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.miner = Miner(self)

    def reasons(self, *numbers: int, reason: str = REASON, merged: str = "2026-09-01T12:00:00Z") -> dict[str, Any]:
        pulls = [merged_pull(n, record(("Bank sync", "not verified", reason, "-")), merged) for n in numbers]
        data = answers(pulls, [])
        for n in numbers:
            changed(data, n, "runway/static/app.js")
        return data

    def test_the_same_reason_on_three_merged_pull_requests_opens_one_issue_in_the_fixed_shape(self) -> None:
        data = self.reasons(20, 21)
        extra = [merged_pull(22, record(("Bank sync (retry)", "not verified", REASON.upper().replace(";", " -"), "-"),
                                        ("Totals", "verified", "unit test", "tests/test_budget.py")))]
        data[PULLS] += extra
        changed(data, 22, "runway/storage/migrations/versions/0040_x.py")
        result = self.miner.run(data, "--app-root", str(gap_app(self.miner.directory)))
        self.assertEqual(result.returncode, 0, result.stderr)
        issues = gap_issues(self.miner)
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue["title"], TITLE)
        self.assertEqual(issue["labels"], ["review-mining"])
        self.assertNotIn("assignees", issue)
        lines = issue["body"].splitlines()
        self.assertEqual(lines[:5], [
            "Area: `screens`, `migrations`",
            "Gap: `no fake provider; a fake under 'tests/fakes/' would make it verifiable`",
            "Evidence: #20, #21, #22",
            f"Would make it verifiable: {rm.GAP_VERIFIABLE}",
            "",
        ])
        body = issue["body"]
        for heading in ("## Context", "## Scope", "## Out of scope", "## Acceptance criteria", "## Tests", "## Depends on", "## Model"):
            self.assertIn(f"\n{heading}\n", body)
        self.assertIn(f"- [ ] {rm.GAP_CRITERION}\n", body)
        self.assertIn("The dimension can be verified: a guide, skill or fixture exists for it, and its steps were run once in the session.", body)
        self.assertIn(f"- [#22](https://github.com/{REPO}/pull/22): `Bank sync (retry)`", body)
        self.assertNotIn("Totals", body)
        self.assertIn("an agent starts on it only once the owner labels it", body)
        self.assertTrue(body.endswith(f"<!-- mined-from row:{rm.reason_id(rm.normalize(REASON))} -->\n"))
        self.assertIn("### Verification gap issues", self.miner.summary.read_text(encoding="utf-8"))

    def test_nothing_opens_without_the_gap_issues_table(self) -> None:
        result = self.miner.run(self.reasons(20, 21, 22), "--app-root", str(gap_app(self.miner.directory, table="")))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(gap_issues(self.miner), [])
        self.assertNotIn("verification gap", result.stdout)
        self.miner.run(self.reasons(20, 21, 22), "--app-root", str(RUNWAY))
        self.assertEqual(gap_issues(self.miner), [])
        self.assertEqual(rm.gap_settings(RUNWAY), None)
        self.assertEqual(rm.gap_settings(ROOT / "tests" / "fixtures" / "consumer-waypoint"), None)
        self.assertEqual(rm.gap_settings(ROOT), None)

    def test_an_unreadable_pyproject_or_none_at_all_still_ends_the_run_as_before(self) -> None:
        for text in ("[tool.fleet.verification\n", None):
            app = self.miner.directory / ("broken" if text else "empty")
            app.mkdir()
            if text:
                (app / "pyproject.toml").write_text(text, encoding="utf-8")
            result = self.miner.run(self.reasons(20, 21, 22), "--app-root", str(app))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("review-mine: no verification gaps:", result.stderr)
            self.assertNotIn("gap issues", result.stderr)
            self.assertEqual(gap_issues(self.miner), [])

    def test_the_threshold_and_the_window_hold_at_their_edges(self) -> None:
        app = gap_app(self.miner.directory)
        self.miner.run(self.reasons(20, 21), "--app-root", str(app))
        self.assertEqual(gap_issues(self.miner), [])
        two = gap_app(self.miner.directory, f"{GAP}pulls = 2\n", name="two")
        self.miner.run(self.reasons(20, 21), "--app-root", str(two))
        self.assertEqual(len(gap_issues(self.miner)), 1)
        self.miner.log.unlink()
        data = self.reasons(20, 21)
        data[PULLS].append(merged_pull(22, record(("Bank sync", "not verified", REASON, "-")), merged="2026-08-01T23:59:59Z"))
        self.miner.run(data, "--app-root", str(app))
        self.assertEqual(gap_issues(self.miner), [])
        data[PULLS][-1]["merged_at"] = "2026-08-02T00:00:00Z"
        self.miner.run(data, "--app-root", str(app))
        self.assertEqual(len(gap_issues(self.miner)), 1)
        self.miner.log.unlink()
        short = gap_app(self.miner.directory, f"{GAP}days = 20\n", name="short")
        self.miner.run(self.reasons(20, 21, 22), "--app-root", str(short))
        self.assertEqual(gap_issues(self.miner), [])
        self.miner.run(self.reasons(20, 21, 22, merged="2026-09-12T00:00:00Z"), "--app-root", str(short))
        self.assertEqual(len(gap_issues(self.miner)), 1)

    def test_a_window_longer_than_the_report_s_is_cut_to_it(self) -> None:
        app = gap_app(self.miner.directory, f"{GAP}days = 365\n")
        data = self.reasons(20, 21, 22)
        data[f"GET repos/{REPO}/issues/comments?since=2026-09-01T00:00:00Z&per_page=100"] = []
        result = self.miner.run(data, "--app-root", str(app), "--days", "30", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("look back 30 days, the report's window, not 365", result.stdout)
        self.assertIn("merged in the last 30 days (since 2026-09-01)", result.stdout)

    def test_two_dimensions_with_the_reason_on_one_pull_request_count_once(self) -> None:
        pulls = [merged_pull(20, record(("A", "not verified", REASON, "-"), ("B", "not verified", REASON, "-"))),
                 merged_pull(21, record(("A", "not verified", REASON, "-"))), merged_pull(22, "No record.")]
        data = answers(pulls, [])
        for n in (20, 21, 22):
            changed(data, n, "runway/static/app.js")
        self.miner.run(data, "--app-root", str(gap_app(self.miner.directory)))
        self.assertEqual(gap_issues(self.miner), [])
        self.assertEqual([c["call"] for c in self.miner.calls() if c["call"].endswith("/20/files?per_page=100")],
                         [f"GET repos/{REPO}/pulls/20/files?per_page=100"])
        self.miner.run(data, "--app-root", str(gap_app(self.miner.directory, f"{GAP}pulls = 2\n", name="two")))
        self.assertIn(f"- [#20](https://github.com/{REPO}/pull/20): `A`, `B`\n", gap_issues(self.miner)[0]["body"])

    def test_without_guides_the_area_is_ungrouped_and_no_file_list_is_read_for_it(self) -> None:
        data = self.reasons(20, 21, 22)
        data[PULLS].append(merged_pull(23, "No record."))
        result = self.miner.run(data, "--app-root", str(gap_app(self.miner.directory, guides=False)), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("\nArea: `ungrouped`\n", result.stdout)
        self.assertNotIn("GET repos/example/app/pulls/23/files?per_page=100", [c["call"] for c in self.miner.calls()])

    def test_an_issue_with_the_gap_s_marker_open_or_closed_opens_no_second(self) -> None:
        app = gap_app(self.miner.directory)
        marker = f"<!-- mined-from row:{rm.reason_id(rm.normalize(REASON))} -->"
        for state in ("open", "closed"):
            data = self.reasons(20, 21, 22)
            data[MINED] = [{"number": 90, "state": state, "user": {"login": rm.BOT}, "body": f"x\n{marker}\n"}]
            result = self.miner.run(data, "--app-root", str(app))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(gap_issues(self.miner), [], state)
            self.assertIn("already has #90; opening nothing", result.stdout)
        data = self.reasons(20, 21, 22)
        data[MINED] = [{"number": 91, "user": {"login": "someone"}, "body": f"x\n{marker}\n"}]
        self.miner.run(data, "--app-root", str(app))
        self.assertEqual(len(gap_issues(self.miner)), 1)

    def test_pull_requests_by_exempt_authors_don_t_count(self) -> None:
        data = self.reasons(20, 21, 22)
        data[PULLS][0]["user"] = {"login": "dependabot[bot]"}
        app = gap_app(self.miner.directory)
        self.miner.run(data, "--app-root", str(app))
        self.assertEqual(gap_issues(self.miner), [])
        custom = gap_app(self.miner.directory, name="custom")
        text = (custom / "pyproject.toml").read_text(encoding="utf-8")
        (custom / "pyproject.toml").write_text(text.replace("required = false\n", 'required = false\nexempt-authors = []\n'), encoding="utf-8")
        self.miner.run(data, "--app-root", str(custom))
        self.assertEqual(len(gap_issues(self.miner)), 1)

    def uncovered(self, *paths: str, numbers: tuple[int, ...] = (30, 31, 32)) -> dict[str, Any]:
        data = answers([merged_pull(n, "No record.") for n in numbers], [])
        for n in numbers:
            changed(data, n, *paths, "runway/static/app.js")
        return data

    def test_an_uncovered_folder_on_three_merged_pull_requests_opens_one_issue(self) -> None:
        data = self.uncovered("lib/providers/bank.py", "lib/providers/deep/x.py", "CHANGELOG.md", "docs/verification/new.md")
        result = self.miner.run(data, "--app-root", str(gap_app(self.miner.directory)))
        self.assertEqual(result.returncode, 0, result.stderr)
        issues = gap_issues(self.miner)
        self.assertEqual([i["title"] for i in issues], ["Verification gap: no verification guide covers lib/providers/"])
        lines = issues[0]["body"].splitlines()
        self.assertEqual(lines[:4], [
            "Area: `lib/providers/`",
            "Gap: no verification guide covers `lib/providers/`",
            "Evidence: #30, #31, #32",
            "Would make it verifiable: a verification guide whose `## Paths` covers `lib/providers/**`, with the steps that verify a "
            "change there (the fleet-verification-guide skill writes it)",
        ])
        self.assertIn(f"- [#30](https://github.com/{REPO}/pull/30): `lib/providers/bank.py`, `lib/providers/deep/x.py`", issues[0]["body"])
        self.assertIn(f"- [ ] {rm.GAP_CRITERION}", issues[0]["body"])
        self.assertTrue(issues[0]["body"].endswith(f"<!-- mined-from row:{rm.paths_id('lib/providers/')} -->\n"))
        for absent in ("CHANGELOG", "docs/verification", "runway/static"):
            self.assertNotIn(absent, issues[0]["body"])

    def test_no_uncovered_folder_without_guides_or_with_a_broken_guide(self) -> None:
        data = self.uncovered("lib/providers/bank.py")
        self.miner.run(data, "--app-root", str(gap_app(self.miner.directory, guides=False)))
        self.assertEqual(gap_issues(self.miner), [])
        self.assertEqual([c["call"] for c in self.miner.calls() if "/files" in c["call"]], [])
        broken = gap_app(self.miner.directory, name="broken")
        (broken / "docs" / "verification" / "extra.md").write_text("not a guide\n", encoding="utf-8")
        result = self.miner.run(data, "--app-root", str(broken))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(gap_issues(self.miner), [])

    def test_a_pull_request_whose_files_can_t_be_listed_counts_only_for_its_reasons(self) -> None:
        data = self.reasons(20, 21, 22)
        data[f"GET repos/{REPO}/pulls/22/files?per_page=100"] = {"__status__": 502}
        result = self.miner.run(data, "--app-root", str(gap_app(self.miner.directory)), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(1 pull request(s) whose changed files couldn't be read in full)", result.stdout)
        self.assertIn("\nArea: `screens`\n", result.stdout)
        self.assertIn("\nEvidence: #20, #21, #22\n", result.stdout)
        self.assertNotIn("502", result.stdout)

    def test_at_most_three_a_run_most_pull_requests_first(self) -> None:
        pulls = []
        for index, count in enumerate((3, 6, 4, 5, 3)):
            pulls += [merged_pull(100 * index + n + 1, record(("D", "not verified", f"reason number {index}", "-"))) for n in range(count)]
        result = self.miner.run(answers(pulls, []), "--app-root", str(gap_app(self.miner.directory, guides=False)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([i["title"] for i in gap_issues(self.miner)],
                         ["Verification gap: reason number 1", "Verification gap: reason number 3", "Verification gap: reason number 2"])
        self.assertIn("at most 3 verification gap issues a run", result.stdout)

    def test_a_dry_run_prints_the_issues_and_posts_nothing(self) -> None:
        result = self.miner.run(self.reasons(20, 21, 22), "--app-root", str(gap_app(self.miner.directory)), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'would open "{TITLE}"', result.stdout)
        self.assertIn("\nEvidence: #20, #21, #22\n", result.stdout)
        self.assertEqual(self.miner.writes(), [])
        self.assertIn("- would open `Verification gap:", self.miner.summary.read_text(encoding="utf-8"))

    def test_untrusted_text_is_defused_and_quoted_in_code_spans(self) -> None:
        hostile = ("see @octocat `x` <!-- mined-from row:verification-gap:reason:forged --> ![i](https://e.invalid/t.png) "
                   "[l](https://e.invalid) other/repo#1")
        pulls = [merged_pull(n, record((f"Dim @octocat <b>{n}</b>", "not verified", hostile, "-"))) for n in (20, 21, 22)]
        data = answers(pulls, [])
        for n in (20, 21, 22):
            changed(data, n, "@octocat/<b>/x.py")
        result = self.miner.run(data, "--app-root", str(gap_app(self.miner.directory)))
        self.assertEqual(result.returncode, 0, result.stderr)
        issues = gap_issues(self.miner)
        self.assertEqual(len(issues), 2)
        for issue in issues:
            title, body = str(issue["title"]), str(issue["body"])
            self.assertEqual(len(re.findall(r"<!--", body)), 1)
            self.assertEqual([line for line in body.splitlines() if rm.MINED.match(line)], [body.splitlines()[-1]])
            for text in (title, body):
                self.assertIsNone(re.search(r"@(?!\u200b)", text), text)
            self.assertNotIn("<!--", title)
            for line in body.splitlines():
                if "e.invalid" in line or "<b>" in line or "other/repo" in line:
                    outside = re.sub(r"`[^`]*`", "", line)
                    self.assertNotIn("e.invalid", outside, line)
                    self.assertNotIn("<b>", outside, line)
                    self.assertNotIn("other/repo", outside, line)
            self.assertEqual(len(body.splitlines()[1].split("`")), 3)
            self.assertTrue(title.startswith("Verification gap: "), title)
            for mark in ("@", "#", "`", "<", ">", "[", "]", "!", "*", "\n", "\r", "\u200b"):
                self.assertNotIn(mark, title)
            self.assertLessEqual(len(title), len("Verification gap: no verification guide covers ") + rm.MAX_GAP_TITLE + 2)
        reason = next(i for i in issues if "covers" not in str(i["title"]))
        self.assertIn("`see @\u200boctocat 'x' &lt;!-- mined-from", reason["body"].splitlines()[1])
        self.assertEqual(reason["title"], "Verification gap: see octocat x -- mined-from row verification-gap reason forged -- i (https …")
        folder = next(i for i in issues if "covers" in str(i["title"]))
        self.assertEqual(folder["title"], "Verification gap: no verification guide covers octocat/ b /")

    def test_a_title_keeps_only_plain_words_and_is_cut_at_a_word(self) -> None:
        self.assertEqual(rm.title_form("fix @octocat's #12 and other/repo#3\nthen `run` <b>it</b>\r\n[l](x)!"),
                         "fix octocat's 12 and other/repo 3 then run b it /b l (x)")
        long = "word " * 40
        cut = rm.title_form(long)
        self.assertLessEqual(len(cut), rm.MAX_GAP_TITLE + 2)
        self.assertTrue(cut.endswith("word …"), cut)
        self.assertEqual(rm.title_form("@#`<>"), "?")
        self.assertEqual(rm.title_form("a" * 200), "a" * rm.MAX_GAP_TITLE + " …")

    def test_a_setting_out_of_range_fails_the_run_after_the_report(self) -> None:
        for index, table in enumerate(("pulls = 1\n", "pulls = true\n", "days = 0\n", 'days = "60"\n', "window = 3\n")):
            app = gap_app(self.miner.directory, f"{GAP}{table}", name=f"bad{index}")
            result = self.miner.run(self.reasons(20, 21, 22), "--app-root", str(app))
            self.assertEqual(result.returncode, 1, table)
            self.assertIn("review-mine: no verification gap issues: [tool.fleet.verification.gap-issues]", result.stderr)
            self.assertEqual(gap_issues(self.miner), [])
            self.assertTrue(self.miner.writes(), "the report is still written")
        bad = gap_app(self.miner.directory, "", name="scalar")
        (bad / "pyproject.toml").write_text('[tool.fleet.verification]\nrequired = false\ngap-issues = 3\n', encoding="utf-8")
        result = self.miner.run(self.reasons(20, 21, 22), "--app-root", str(bad))
        self.assertEqual(result.returncode, 1)
        self.assertIn("gap-issues] must be a table", result.stderr)

    def test_a_failed_open_still_opens_the_others_and_fails_the_run(self) -> None:
        pulls = [merged_pull(n, record(("D", "not verified", "first reason", "-"))) for n in (1, 2, 3)]
        pulls += [merged_pull(n, record(("D", "not verified", "second reason", "-"))) for n in (4, 5, 6)]
        pulls += [merged_pull(n, record(("D", "not verified", "third reason", "-"))) for n in (7, 8, 9)]
        data = answers(pulls, [])
        data[CREATE] = {"__series__": [{"number": 70}, {"__status__": 502}, {"number": 72}]}
        result = self.miner.run(data, "--app-root", str(gap_app(self.miner.directory, guides=False)))
        self.assertEqual(result.returncode, 1)
        self.assertIn("failed (1)", result.stderr)
        self.assertIn("the next run tries again", result.stderr)
        self.assertEqual(len([c for c in self.miner.calls() if c["call"] == CREATE]), 3)
        self.assertIn("opened #70 for verification-gap:reason:first-reason", result.stdout)
        self.assertIn("opening the issue for verification-gap:reason:second-reason failed", result.stderr)
        self.assertIn("opened #72 for verification-gap:reason:third-reason", result.stdout)

    def test_ids_are_stable_and_folders_whose_slugs_meet_stay_apart(self) -> None:
        self.assertEqual(rm.reason_id(rm.normalize("No fake, provider!")), rm.reason_id(rm.normalize("no FAKE provider")))
        self.assertEqual(rm.reason_id(rm.normalize("no fake provider")), "verification-gap:reason:no-fake-provider")
        self.assertNotEqual(rm.paths_id("a-b/"), rm.paths_id("a/b/"))
        self.assertRegex(rm.paths_id("a/b/"), r"^verification-gap:paths:a-b-[0-9a-f]{12}$")
        for text in ("x" * 300, "y" * 300):
            self.assertTrue(rm.MINED.match(f"<!-- mined-from row:{rm.reason_id(text)} -->"))
        self.assertNotEqual(rm.reason_id("x" * 300), rm.reason_id("x" * 299 + "y"))

    def test_a_folder_is_at_most_two_levels_and_a_root_file_has_none(self) -> None:
        self.assertEqual(rm.area_of("CHANGELOG.md"), None)
        self.assertEqual(rm.area_of("/abs.py"), None)
        self.assertEqual(rm.area_of("lib/x.py"), "lib/")
        self.assertEqual(rm.area_of("lib/providers/x.py"), "lib/providers/")
        self.assertEqual(rm.area_of("lib/providers/deep/x.py"), "lib/providers/")
        self.assertEqual(rm.area_of(".github/workflows/ci.yml"), ".github/workflows/")


def loop_stats(merged: int = 3, resolved: float | None = 0.4, when: str = NOW, days: int = 30) -> dict[str, Any]:
    findings = {"raised": 5, "by_severity": {"blocking": 1, "advisory": 3, "unknown": 1}, "resolved": 2, "dismissed": 1, "open": 2,
                "resolved_share": resolved, "dismissed_share": 0.2, "at_merge": 0, "open_at_merge": 0, "open_at_merge_share": None,
                "indeterminate_at_merge": 0}
    return {"schema": 1, "repo": REPO, "generated": when, "since": SINCE, "days": days, "default_branch": "main", "missing": [],
            "reviews": {"merged": merged, "unread": 0, "with_agent_review": 2, "with_second_review": 1, "findings": findings},
            "verification": {"configured": False},
            "report": {"issue": 60, "rows": 2, "ticked": 1, "tasks_opened": 1, "tasks_opened_in_window": 1, "tasks_closed_in_window": 0,
                       "gap_issues_opened": 0},
            "harness": {"commits": {"context": 1, "invariants": 0, "semgrep": 0, "skills": 2, "verification_guides": None, "total": 4},
                        "checked_in_window": 1, "checked_items": ["Layout"], "days_since_change": {"AGENTS.md": 3}},
            "fleet": {"version": "v1.0.0", "sync_open": False}}


class LoopHealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.miner = Miner(self)
        self.stats = self.miner.directory / "loop-stats.json"
        self.comments = [review_comment(10, [finding("Layout", "Overflows")]), review_comment(11, [finding("Layout", "Cramped")])]
        self.pulls = [pull(10), pull(11)]

    def run_with(self, stats: dict[str, Any] | str | None, previous: str | None = None, *flags: str) -> str:
        if isinstance(stats, dict):
            self.stats.write_text(json.dumps(stats), encoding="utf-8")
        elif isinstance(stats, str):
            self.stats.write_text(stats, encoding="utf-8")
        open_issues = [{"number": 60, "title": rm.TITLE, "body": previous}] if previous is not None else []
        result = self.miner.run(answers(self.pulls, self.comments, open_issues), *(["--stats", str(self.stats)] if stats is not None else []), *flags)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.result = result
        return self.miner.body()

    def test_stats_for_another_repository_aren_t_used(self) -> None:
        body = self.run_with({**loop_stats(), "repo": "owner/other"})
        self.assertIn("owner/other", self.result.stderr)
        self.assertNotIn(loop_health.PREFIX, body)

    def test_now_with_an_offset_is_converted_to_utc(self) -> None:
        now = rm.parse_now("2026-10-01T00:00:00-04:00")
        self.assertEqual(rm.window(now, 0), "2026-10-01T04:00:00Z")

    def test_the_section_and_the_hidden_line_lead_the_report(self) -> None:
        body = self.run_with(loop_stats())
        lines = body.splitlines()
        self.assertEqual(lines[0], rm.MARKER)
        summary = loop_health.decode(lines[1])
        assert summary is not None
        self.assertEqual(summary["values"]["merged"], 3)
        self.assertEqual(summary["date"], "2026-10-01")
        self.assertEqual(lines[2], "### Loop health")
        self.assertLess(body.index("### Loop health"), body.index("Review findings that came back"))
        self.assertIn("| Measure | This run (2026-10-01) |", body)
        self.assertIn("| Merged pull requests | 3 |", body)
        self.assertIn("| … resolved | 40% |", body)
        self.assertIn("| … open at merge | n/a |", body)
        self.assertIn("| Merged pull requests with a verification record | n/a |", body)
        self.assertIn("No earlier numbers in this issue", body)
        self.assertEqual(body.count("<!--"), 1 + 1 + body.count("<!-- row:"))
        summary_text = self.miner.summary.read_text(encoding="utf-8")
        self.assertNotIn("Loop health", summary_text)
        self.assertNotIn("| Merged pull requests |", summary_text)
        self.assertNotIn("<!-- loop-stats", summary_text)
        self.assertNotIn(rm.MARKER, summary_text)
        self.assertTrue(summary_text.startswith("Review findings that came back"))
        self.assertEqual(set(mining_tasks.tasks_in(body)), {"repeated:layout"})

    def test_last_run_s_numbers_round_trip_through_the_hidden_line(self) -> None:
        first = self.run_with(loop_stats(merged=2, resolved=0.5, when="2026-09-01T00:00:00Z"))
        second = self.run_with(loop_stats(), previous=first)
        self.assertIn("| Measure | Last run (2026-09-01) | This run (2026-10-01) |", second)
        self.assertIn("| Merged pull requests | 2 | 3 |", second)
        self.assertIn("| … resolved | 50% | 40% |", second)
        self.assertNotIn("No earlier numbers", second)
        summary = loop_health.decode(second.splitlines()[1])
        assert summary is not None
        self.assertEqual(summary["values"]["merged"], 3)

    def test_a_last_run_over_another_window_isn_t_compared(self) -> None:
        first = self.run_with(loop_stats(merged=2, when="2026-09-01T00:00:00Z", days=90))
        second = self.run_with(loop_stats(), previous=first)
        self.assertIn("| Measure | This run (2026-10-01) |", second)
        self.assertNotIn("Last run (", second)
        self.assertIn("| Merged pull requests | 3 |", second)
        self.assertIn("over the last 30 days", second)
        self.assertIn("The last run (2026-09-01) covered 90 days, not 30, so it isn't compared", second)
        summary = loop_health.decode(second.splitlines()[1])
        assert summary is not None
        self.assertEqual(summary["days"], 30)
        third = self.run_with(loop_stats(merged=4), previous=second)
        self.assertIn("| Merged pull requests | 3 | 4 |", third)

    def test_a_malformed_or_oversized_previous_line_is_ignored(self) -> None:
        good = loop_health.encode({"schema": 1, "date": "2026-09-01", "days": 30, "values": {"merged": 2}})
        forged = good.replace("loop-stats ", "loop-stats x")
        oversized = f"<!-- loop-stats {'A' * 4000} -->"
        wrong = loop_health.encode({"schema": 1, "date": "2026-09-01", "days": 30, "values": {"merged": -2}})
        for line in (forged, oversized, wrong, "<!-- loop-stats e30= -->"):
            body = self.run_with(loop_stats(), previous=f"{rm.MARKER}\n{line}\nold")
            self.assertNotIn("Last run", body, line[:40])
            self.assertIn("No earlier numbers in this issue", body)
        moved = self.run_with(loop_stats(), previous=f"{rm.MARKER}\nintro\n{good}")
        self.assertNotIn("Last run", moved)

    def test_this_run_s_numbers_unreadable_keeps_the_last_run_s(self) -> None:
        first = self.run_with(loop_stats(merged=2, when="2026-09-01T00:00:00Z"))
        whole = loop_stats()
        reviews = {k: v for k, v in whole["reviews"].items() if k != "findings"}
        findings = {k: v for k, v in whole["reviews"]["findings"].items() if k != "resolved_share"}
        truncated = [{k: v for k, v in whole.items() if k != "reviews"}, {**whole, "reviews": reviews},
                     {**whole, "reviews": {**whole["reviews"], "findings": findings}}, {**whole, "extra": 1},
                     {**whole, "verification": {"configured": 0}}, {**whole, "missing": None}]
        for broken in ("{not json", json.dumps({**loop_stats(), "schema": 2}), json.dumps(loop_stats(merged=-1)),
                       *(json.dumps(each) for each in truncated)):
            body = self.run_with(broken, previous=first)
            self.assertIn("review-mine: the loop stats in", self.result.stderr)
            self.assertIn("| Measure | Last run (2026-09-01) |", body)
            self.assertNotIn("This run (", body)
            self.assertIn("This run's numbers couldn't be read", body)
            self.assertEqual(body.splitlines()[1], first.splitlines()[1])
        self.miner.summary.unlink()
        nothing = self.run_with("{", previous="old body")
        self.assertIn("this issue holds no earlier ones", nothing)
        self.assertFalse(nothing.splitlines()[1].startswith(loop_health.PREFIX))

    def test_explicit_nulls_are_this_run_s_numbers(self) -> None:
        first = self.run_with(loop_stats(merged=2, when="2026-09-01T00:00:00Z"))
        unread = {**loop_stats(), "reviews": None, "harness": None, "missing": ["reviews: gh api failed (1)"]}
        body = self.run_with(unread, previous=first)
        self.assertIn("| Merged pull requests | 2 | n/a |", body)
        self.assertNotIn("couldn't be read", body)
        summary = loop_health.decode(body.splitlines()[1])
        assert summary is not None
        self.assertIsNone(summary["values"]["merged"])
        self.assertEqual(summary["values"]["rows"], 2)

    def test_without_stats_the_report_is_unchanged(self) -> None:
        body = self.run_with(None, previous=f"{rm.MARKER}\n{loop_health.encode({'schema': 1, 'date': '2026-09-01', 'days': 30, 'values': {}})}\n")
        self.assertNotIn("Loop health", body)
        self.assertNotIn("loop-stats", body)
        self.assertTrue(body.splitlines()[1].startswith("Review findings that came back"))

    def test_a_dry_run_prints_the_section_and_writes_nothing(self) -> None:
        self.stats.write_text(json.dumps(loop_stats()), encoding="utf-8")
        result = self.miner.run(answers(self.pulls, self.comments), "--stats", str(self.stats), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("### Loop health", result.stdout)
        self.assertIn("<!-- loop-stats ", result.stdout)
        self.assertEqual(self.miner.writes(), [])
        self.assertNotIn("Loop health", self.miner.summary.read_text(encoding="utf-8"))


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
        self.assertEqual(sorted(inputs), ["days", "dry-run", "fleet-ref", "loop-stats-days"])
        self.assertEqual(mapping(inputs["days"])["default"], "90")
        self.assertEqual(mapping(inputs["loop-stats-days"])["default"], "30")
        self.assertEqual(mapping(mapping(call["outputs"])["loop-stats-url"])["value"], "${{ jobs.mine.outputs.loop-stats-url }}")
        self.assertEqual(mapping(inputs["dry-run"])["default"], "false")
        self.assertNotIn("secrets", call)
        self.assertNotIn("secrets.", self.text)
        self.assertEqual(workflow["permissions"], "{}")
        self.assertEqual(workflow["env"], {"FLEET_REF": self_check.expected_ref(release.read_version(cli.ROOT))})
        jobs = mapping(workflow["jobs"])
        self.assertEqual(list(jobs), ["mine"])
        mine = mapping(jobs["mine"])
        self.assertEqual(mine["permissions"], {"contents": "read", "issues": "write", "pull-requests": "read"})
        self.assertRegex(self.text, r"runs-on: ubuntu-latest[ \t]+# hosted: \S")
        self.assertEqual(self.text.count("actions/checkout@"), 2)
        self.assertEqual(self.text.count("persist-credentials: false"), 2)
        self.assertEqual(mine["outputs"], {"loop-stats-url": "${{ steps.upload.outputs.artifact-url }}"})
        steps = mine["steps"]
        assert isinstance(steps, list)
        for step in map(mapping, steps):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                self.assertIn(mapping(step["with"]).get("path"), ("app", ".fleet"))
        self.assertEqual([s.get("name") for s in map(mapping, steps) if "run" in s],
                         ["Check fleet's commit", "Measure the loop", "Rewrite the report"])
        upload = [mapping(s) for s in steps if str(mapping(s).get("uses", "")).startswith("actions/upload-artifact@")]
        self.assertEqual(len(upload), 1)
        self.assertRegex(str(upload[0]["uses"]), r"^actions/upload-artifact@[0-9a-f]{40}$")
        self.assertIn("actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9   # v7.0.2", self.text)
        self.assertEqual(upload[0]["id"], "upload")
        self.assertEqual(mapping(upload[0]["with"]), {"path": "${{ runner.temp }}/loop-stats/loop-stats.json", "archive": "false",
                                                      "retention-days": "90", "if-no-files-found": "error"})
        names = [mapping(s).get("name") or mapping(s).get("uses") for s in steps]
        self.assertLess(names.index("Measure the loop"), names.index(upload[0]["uses"]))
        self.assertLess(names.index(upload[0]["uses"]), names.index("Rewrite the report"))
        rewrite = mapping(next(s for s in steps if mapping(s).get("name") == "Rewrite the report"))
        self.assertEqual(rewrite["if"], "${{ !cancelled() && steps.app.outcome == 'success' && steps.fleet.outcome == 'success' }}")
        self.assertEqual([mapping(s).get("id") for s in steps], ["app", None, "fleet", None, "upload", None])
        self.assertNotIn("success()", str(rewrite["if"]))
        self.assertNotIn("steps.upload", str(rewrite["if"]))
        self.assertNotIn("Measure", str(rewrite["if"]))
        for step in map(mapping, steps):
            self.assertNotIn("continue-on-error", step)
            if step is not rewrite:
                self.assertNotIn("if", step)
        measure, script = self.text.split("run: |")[1:3]
        self.assertIn("python3 -E -s -m fleet_tools.cli loop-stats", measure)
        self.assertIn('--json "$OUT/loop-stats.json" --markdown "$OUT/loop-stats.md"', measure)
        self.assertIn('cat "$OUT/loop-stats.md" >> "$GITHUB_STEP_SUMMARY"', measure)
        self.assertNotRegex(measure.split("- uses:", 1)[0], r"\$\{\{")
        self.assertIn("python3 -E -s -m fleet_tools.cli review-mine", script)
        self.assertIn("cd .fleet", script)
        self.assertIn('--summary "$GITHUB_STEP_SUMMARY" --stats "$STATS"', script)
        self.assertIn('--app-root "$GITHUB_WORKSPACE/app"', script)
        app = mapping(mapping(steps[0])["with"])
        self.assertEqual(app["path"], "app")
        self.assertEqual(app["ref"], "${{ github.event.repository.default_branch }}")
        self.assertIn("sparse-checkout: |\n            AGENTS.md\n            pyproject.toml\n            *.md\n", self.text)
        self.assertNotRegex(script, r"\$\{\{")
        self.assertIn("17 6 1 * *", self.text)
        self.assertIn("17 6 * * 1", self.text)

    def test_gap_issues_take_no_new_input_permission_or_secret_and_follow_dry_run(self) -> None:
        call = mapping(mapping(self.workflow["on"])["workflow_call"])
        self.assertEqual(sorted(mapping(call["inputs"])), ["days", "dry-run", "fleet-ref", "loop-stats-days"])
        self.assertIn("verification gap issues", str(mapping(mapping(call["inputs"])["dry-run"])["description"]))
        mine = mapping(mapping(self.workflow["jobs"])["mine"])
        self.assertEqual(mine["permissions"], {"contents": "read", "issues": "write", "pull-requests": "read"})
        self.assertNotIn("secrets", self.text.split("jobs:", 1)[1])
        script = self.text.split("run: |")[2]
        self.assertIn('if [ "$DRY_RUN" = true ]; then flags+=(--dry-run); fi', script)
        self.assertIn('--app-root "$GITHUB_WORKSPACE/app"', script)
        self.assertIn('"${flags[@]}"', script)
        self.assertNotRegex(script, r"\$\{\{")
        self.assertIn("[tool.fleet.verification.gap-issues]", self.text)
        self.assertIn("(opt-in; without it nothing changes)", self.text)
        self.assertNotIn("concurrency", self.workflow)
        self.assertEqual(mine["concurrency"], {"group": "review-mining-${{ inputs.dry-run && format('dry-run-{0}', github.run_id) || 'live' }}",
                                               "cancel-in-progress": "false"})
        self.assertEqual(self.text.count("concurrency:"), 1)
        self.assertEqual(self.text.count("cancel-in-progress"), 1)
        test = mapping(self_check.parse((WORKFLOWS / "self-test.yml").read_text(encoding="utf-8")))
        callers = [job for job in mapping(test["jobs"]).values() if mapping(job).get("uses") == "./.github/workflows/review-mining.yml"]
        self.assertEqual([mapping(job)["with"]["dry-run"] for job in callers], ["true"])
        mining = mapping(mapping(mapping(self_check.parse((WORKFLOWS / "mining-tasks.yml").read_text(encoding="utf-8")))["jobs"])["tasks"])
        self.assertFalse(str(mapping(mining["concurrency"])["group"]).startswith("review-mining-"))
        for doc in ("docs/review-mining.md", "docs/adopting.md"):
            text = (ROOT / doc).read_text(encoding="utf-8")
            self.assertIn("[tool.fleet.verification.gap-issues]", text, doc)
        guide = (ROOT / "docs" / "review-mining.md").read_text(encoding="utf-8")
        for line in ("Area: ", "Gap: ", "Evidence: ", "Would make it verifiable: ", rm.GAP_CRITERION):
            self.assertIn(line, guide)

    def test_the_self_test_runs_it_in_dry_run_and_the_docs_name_both_schedules(self) -> None:
        workflow = mapping(self_check.parse((WORKFLOWS / "self-test.yml").read_text(encoding="utf-8")))
        job = mapping(mapping(workflow["jobs"])["review-mining"])
        self.assertEqual(job["uses"], "./.github/workflows/review-mining.yml")
        self.assertEqual(job["with"], {"fleet-ref": "${{ github.sha }}", "dry-run": "true"})
        self.assertEqual(job["permissions"], {"contents": "read", "issues": "write", "pull-requests": "read"})
        check = mapping(mapping(workflow["jobs"])["review-mining-artifact"])
        self.assertEqual(check["needs"], "[review-mining]")
        self.assertEqual(check["permissions"], {"actions": "read"})
        steps = check["steps"]
        assert isinstance(steps, list)
        step = mapping(steps[0])
        self.assertEqual(mapping(step["env"])["URL"], "${{ needs.review-mining.outputs.loop-stats-url }}")
        self.assertIn('select(.name == "loop-stats.json")', (WORKFLOWS / "self-test.yml").read_text(encoding="utf-8"))
        for doc in ("docs/review-mining.md", "docs/adopting.md"):
            text = (ROOT / doc).read_text(encoding="utf-8")
            self.assertIn("17 6 1 * *", text, doc)
            self.assertIn("17 6 * * 1", text, doc)
            self.assertIn("review-mining", text, doc)


if __name__ == "__main__":
    unittest.main()
