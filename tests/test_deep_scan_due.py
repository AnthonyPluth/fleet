import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fleet_tools.self_check import Node, parse

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "deep_scan_due.py"
FAKES = ROOT / "tests" / "fakes" / "security"
REPO = "example/app"
TITLE = "Deep security scan due"
OPEN = f"GET repos/{REPO}/issues?state=open&per_page=100"
CLOSED = f"GET repos/{REPO}/issues?state=closed&per_page=100"
CREATE = f"POST repos/{REPO}/issues"
REPOSITORY = f"GET repos/{REPO}"
SHA12 = "0123456789ab"
FULL = "f" * 40


def issue(number: int, title: str = TITLE, body: str = "", closed_at: str = "", pull_request: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {"number": number, "title": title, "body": body, "closed_at": closed_at or None}
    if pull_request:
        data["pull_request"] = {"url": f"https://example.invalid/pulls/{number}"}
    return data


def comments(number: int, *bodies: str) -> tuple[str, list[dict[str, str]]]:
    return f"GET repos/{REPO}/issues/{number}/comments?per_page=100", [{"body": body} for body in bodies]


class Reminder:
    def __init__(self, test: unittest.TestCase) -> None:
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.answers = self.directory / "answers.json"
        self.log = self.directory / "log.jsonl"

    def run(self, answers: dict[str, Any], *flags: str, fleet_ref: str = "main") -> subprocess.CompletedProcess[str]:
        self.answers.write_text(json.dumps(answers), encoding="utf-8")
        env = {
            **os.environ,
            "PATH": f"{FAKES}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_ANSWERS": str(self.answers),
            "FAKE_GH_LOG": str(self.log),
            "GH_TOKEN": "fake",
        }
        command = [sys.executable, "-I", str(SCRIPT), "--repo", REPO, "--fleet-ref", fleet_ref, *flags]
        return subprocess.run(command, env=env, capture_output=True, text=True, check=False)

    def calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def created(self) -> list[dict[str, Any]]:
        return [json.loads(call["input"]) for call in self.calls() if call["call"] == CREATE]


def base(open_issues: list[dict[str, Any]], closed_issues: list[dict[str, Any]]) -> dict[str, Any]:
    return {OPEN: open_issues, CLOSED: closed_issues, REPOSITORY: {"default_branch": "main"}, CREATE: {"number": 42}}


class OpensOneIssueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reminder = Reminder(self)

    def test_opens_the_issue_when_none_exists(self) -> None:
        result = self.reminder.run(base([], []))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "deep-scan-due: opened #42")
        created = self.reminder.created()
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["title"], TITLE)
        self.assertEqual(created[0]["labels"], ["manual", "security"])
        self.assertIn("https://github.com/AnthonyPluth/fleet/blob/main/docs/deep-scan.md", created[0]["body"])
        self.assertIn("No earlier scan is recorded", created[0]["body"])
        self.assertIn("CLAUDE-SECURITY-REVISION-<sha12>.json", created[0]["body"])

    def test_a_second_run_while_one_is_open_adds_nothing(self) -> None:
        result = self.reminder.run(base([issue(42)], []))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "deep-scan-due: #42 is open; nothing to do")
        self.assertEqual(self.reminder.created(), [])
        self.assertEqual([call["call"] for call in self.reminder.calls()], [OPEN])

    def test_other_open_issues_and_pull_requests_dont_count(self) -> None:
        others = [issue(1, title="Something else"), issue(2, pull_request=True), issue(3, title="deep security scan due")]
        result = self.reminder.run(base(others, []))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.reminder.created()), 1)

    def test_the_runbook_link_uses_the_fleet_ref(self) -> None:
        self.reminder.run(base([], []), fleet_ref="v1.2.3")
        self.assertIn("/blob/v1.2.3/docs/deep-scan.md", self.reminder.created()[0]["body"])


class LastScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reminder = Reminder(self)

    def test_reads_the_stamp_from_the_last_closed_issue_and_counts_the_commits_since(self) -> None:
        older = issue(10, body="Stamp: CLAUDE-SECURITY-REVISION-aaaaaaaaaaaa.json", closed_at="2026-01-01T00:00:00Z")
        newer = issue(20, closed_at="2026-04-01T00:00:00Z")
        key, bodies = comments(20, "Scan done.", f"Stamp `CLAUDE-SECURITY-REVISION-{SHA12}-dirty.json`, effort high, 0 critical, 1 high.")
        answers = {**base([], [older, newer]), key: bodies, f"GET repos/{REPO}/compare/{SHA12}...main": {"ahead_by": 17}}
        result = self.reminder.run(answers)
        self.assertEqual(result.returncode, 0, result.stderr)
        body = self.reminder.created()[0]["body"]
        self.assertIn(f"The last scan (#20) covered revision `{SHA12}`; `main` has 17 commits since.", body)

    def test_a_full_sha_counts_when_no_stamp_is_named(self) -> None:
        last = issue(20, body=f"Scanned {FULL} at medium effort.", closed_at="2026-04-01T00:00:00Z")
        key, bodies = comments(20)
        answers = {**base([], [last]), key: bodies, f"GET repos/{REPO}/compare/{FULL}...main": {"ahead_by": 3}}
        result = self.reminder.run(answers)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"revision `{FULL}`; `main` has 3 commits since", self.reminder.created()[0]["body"])

    def test_a_closed_issue_without_a_revision_says_so(self) -> None:
        last = issue(20, body="Done, see the report.", closed_at="2026-04-01T00:00:00Z")
        key, bodies = comments(20, "Closing.")
        result = self.reminder.run({**base([], [last]), key: bodies})
        self.assertEqual(result.returncode, 0, result.stderr)
        body = self.reminder.created()[0]["body"]
        self.assertIn("#20, names no revision stamp", body)
        self.assertFalse(any("compare" in call["call"] for call in self.reminder.calls()))

    def test_a_revision_gone_from_history_gives_an_unknown_count(self) -> None:
        last = issue(20, body=f"CLAUDE-SECURITY-REVISION-{SHA12}.json", closed_at="2026-04-01T00:00:00Z")
        key, bodies = comments(20)
        answers = {**base([], [last]), key: bodies, f"GET repos/{REPO}/compare/{SHA12}...main": {"__status__": 404}}
        result = self.reminder.run(answers)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"revision `{SHA12}`; `main` has an unknown number of commits since", self.reminder.created()[0]["body"])
        self.assertIn(f"::warning::deep-scan-due: the comparison of {SHA12} with main was refused", result.stderr)

    def test_a_counted_comparison_warns_of_nothing(self) -> None:
        last = issue(20, body=f"CLAUDE-SECURITY-REVISION-{SHA12}.json", closed_at="2026-04-01T00:00:00Z")
        key, bodies = comments(20)
        answers = {**base([], [last]), key: bodies, f"GET repos/{REPO}/compare/{SHA12}...main": {"ahead_by": 5}}
        result = self.reminder.run(answers)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("`main` has 5 commits since", self.reminder.created()[0]["body"])
        self.assertNotIn("::warning::", result.stderr)

    def test_the_default_branch_is_the_repositorys(self) -> None:
        last = issue(20, body=f"CLAUDE-SECURITY-REVISION-{SHA12}.json", closed_at="2026-04-01T00:00:00Z")
        key, bodies = comments(20)
        answers = {**base([], [last]), key: bodies, REPOSITORY: {"default_branch": "trunk"}, f"GET repos/{REPO}/compare/{SHA12}...trunk": {"ahead_by": 1}}
        result = self.reminder.run(answers)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("`trunk` has 1 commits since", self.reminder.created()[0]["body"])


class DryRunTest(unittest.TestCase):
    def test_prints_the_issue_and_opens_nothing(self) -> None:
        reminder = Reminder(self)
        result = reminder.run(base([], []), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"deep-scan-due: would open {TITLE!r} with labels manual, security:", result.stdout)
        self.assertIn("No earlier scan is recorded", result.stdout)
        self.assertEqual(reminder.created(), [])

    def test_a_dry_run_while_one_is_open_says_so(self) -> None:
        reminder = Reminder(self)
        result = reminder.run(base([issue(5)], []), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "deep-scan-due: #5 is open; nothing to do")
        self.assertEqual(reminder.created(), [])


def granted(source: str, job: str) -> dict[str, Node]:
    workflow = parse(source)
    assert isinstance(workflow, dict)
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    found = jobs[job]
    assert isinstance(found, dict)
    permissions = found["permissions"]
    assert isinstance(permissions, dict)
    return permissions


class PermissionsTest(unittest.TestCase):
    def test_the_job_may_read_the_comparison_the_count_comes_from(self) -> None:
        workflow = (ROOT / ".github/workflows/deep-scan-due.yml").read_text(encoding="utf-8")
        self.assertEqual(granted(workflow, "remind"), {"contents": "read", "issues": "write"})

    def test_the_callers_grant_what_the_job_asks_for(self) -> None:
        self_test = (ROOT / ".github/workflows/self-test.yml").read_text(encoding="utf-8")
        self.assertEqual(granted(self_test, "deep-scan-due"), {"contents": "read", "issues": "write"})
        adopting = (ROOT / "docs/adopting.md").read_text(encoding="utf-8")
        snippet = re.search(r"^```yaml\n(name: Deep scan due\n.*?)^```", adopting, re.MULTILINE | re.DOTALL)
        assert snippet is not None
        self.assertEqual(granted(snippet.group(1), "remind"), {"contents": "read", "issues": "write"})


class FailuresTest(unittest.TestCase):
    def test_a_failed_api_call_fails_the_run(self) -> None:
        reminder = Reminder(self)
        result = reminder.run({})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(reminder.created(), [])


class DocsTest(unittest.TestCase):
    def script_title(self) -> str:
        spec = importlib.util.spec_from_file_location("deep_scan_due", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        title = module.TITLE
        assert isinstance(title, str)
        return title

    def section(self, doc: str, heading: str) -> str:
        text = (ROOT / "docs" / doc).read_text()
        start = text.index(f"\n{heading}\n")
        end = text.find("\n## ", start + len(heading) + 2)
        return text[start:] if end == -1 else text[start:end]

    def assert_retitle(self, text: str, where: str) -> None:
        title = self.script_title()
        self.assertRegex(text, r"[Rr]etitle", where)
        self.assertIn(f'exactly "{title}"', text, where)

    def test_the_scan_record_says_to_retitle_to_the_title_the_reminder_reads(self) -> None:
        self.assert_retitle(self.section("deep-scan.md", "## The scan record"), "docs/deep-scan.md: The scan record")

    def test_per_app_settings_says_the_first_run_issue_takes_that_title(self) -> None:
        settings = self.section("deep-scan.md", "## Per-app settings")
        item = next(line for line in settings.splitlines() if line.startswith("4. "))
        self.assert_retitle(item, "docs/deep-scan.md: Per-app settings, item 4")

    def test_the_adoption_step_says_to_retitle_the_first_run_issue(self) -> None:
        reminder = self.section("adopting.md", "## Deep-scan reminder")
        step = next(line for line in reminder.splitlines() if "first-run issue" in line)
        self.assert_retitle(step, "docs/adopting.md: Deep-scan reminder")


if __name__ == "__main__":
    unittest.main()
