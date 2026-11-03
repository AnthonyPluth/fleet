import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_checks_workflows import ROOT, snippets

from fleet_tools import self_check
from fleet_tools.self_check import Node

CODEX = "chatgpt-codex-connector[bot]"


def events(workflow: Node) -> list[str]:
    assert isinstance(workflow, dict)
    trigger = workflow.get("on")
    if isinstance(trigger, dict):
        return list(trigger)
    if isinstance(trigger, str):
        return [name.strip() for name in trigger.strip("[]").split(",")]
    return []


def comment_workflows() -> list[tuple[str, str]]:
    found = [(doc, text) for doc, text in snippets() if "issue_comment" in events(self_check.parse(text))]
    for path in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        text = path.read_text(encoding="utf-8")
        if "issue_comment" in events(self_check.parse(text)):
            found.append((path.name, text))
    return found


def threads_callers() -> list[str]:
    return [text for _, text in snippets() if "/.github/workflows/merge-gate-threads.yml@" in text]


def zizmor() -> str:
    beside = Path(sys.executable).with_name("zizmor")
    found = str(beside) if beside.is_file() else shutil.which("zizmor")
    if found is None:
        raise AssertionError("zizmor isn't installed (`make setup` installs the pinned one)")
    return found


def run_zizmor(texts: list[str]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        workflows = Path(directory) / ".github" / "workflows"
        workflows.mkdir(parents=True)
        for index, text in enumerate(texts):
            (workflows / f"snippet-{index}.yml").write_text(text, encoding="utf-8")
        environment = {key: value for key, value in os.environ.items() if key != "GH_TOKEN"}
        return subprocess.run([zizmor(), "--offline", "--no-progress", str(workflows)], capture_output=True, text=True,
                              env=environment, timeout=300, check=False)


class Concurrency(unittest.TestCase):
    def test_the_comment_caller_is_documented(self) -> None:
        self.assertTrue(any(doc == "adopting.md" for doc, _ in comment_workflows()))

    def test_no_issue_comment_workflow_has_a_workflow_wide_group(self) -> None:
        for name, text in comment_workflows():
            with self.subTest(workflow=name):
                workflow = self_check.parse(text)
                assert isinstance(workflow, dict)
                self.assertNotIn("concurrency", workflow, "a workflow-wide group is joined before any job's `if:`, so "
                                 "every comment on the pull request would cancel (or replace, while queued) a gate "
                                 "restart in flight; give each job its own group")

    def test_each_threads_job_has_its_own_group_and_only_codexs_comment_shares_one(self) -> None:
        callers = threads_callers()
        self.assertEqual(len(callers), 2)
        for text in callers:
            workflow = self_check.parse(text)
            assert isinstance(workflow, dict)
            jobs = workflow["jobs"]
            assert isinstance(jobs, dict)
            for name, job in jobs.items():
                with self.subTest(job=name):
                    assert isinstance(job, dict)
                    concurrency = job["concurrency"]
                    assert isinstance(concurrency, dict)
                    group = concurrency["group"]
                    assert isinstance(group, str)
                    if "issue_comment" in events(workflow):
                        self.assertIn("github.event_name", job["if"])
                    if name == "codex-comment":
                        self.assertIn(f"github.event.comment.user.login == '{CODEX}'", job["if"])
                        self.assertIn("${{ github.event.issue.number }}", group)
                        self.assertIn("${{ github.event.comment.user.login }}", group)
                    else:
                        self.assertEqual(group, "merge-gate-threads-${{ github.event.pull_request.number }}")


class Zizmor(unittest.TestCase):
    def test_the_documented_callers_pass_zizmor(self) -> None:
        result = run_zizmor([text for _, text in snippets()])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_issue_comment_trigger_carries_its_justified_ignore(self) -> None:
        for text in threads_callers():
            if "issue_comment" not in text:
                continue
            line = re.search(r"^  issue_comment:(.*)$", text, re.MULTILINE)
            assert line is not None
            self.assertRegex(line.group(1), r"^\s+# zizmor: ignore\[dangerous-triggers\] .*Codex.*number")
            bare = text.replace(line.group(0), "  issue_comment:")
            result = run_zizmor([bare])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("dangerous-triggers", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
