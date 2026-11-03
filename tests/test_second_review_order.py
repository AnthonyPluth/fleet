import base64
import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from fleet_tools import agent_review as ar
from fleet_tools import cli

WORKFLOW = cli.ROOT / ".github" / "workflows" / "agent-review.yml"
FAKE = cli.ROOT / "tests" / "fakes" / "github"
REPO = "example/app"
PR = "7"
HEAD = "a" * 40
NEWER = "b" * 40
EARLIER = 100
LATER = 200
Json = dict[str, Any]


def url(run: int) -> str:
    return f"https://github.com/{REPO}/actions/runs/{run}"


def status(state: str, run: int, description: str = "d", creator: str = ar.BOT, copy: bool = False) -> list[str]:
    return [ar.SECOND.context, state, creator, description, url(run) + (ar.RESTORED if copy else "")]


def step_script(job: str, prefix: str) -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    source = re.search(rf"^  {re.escape(job)}:\n(.*?)(?=^  \S|\Z)", text, re.MULTILINE | re.DOTALL)
    assert source, job
    step = source.group(1).split(f"      - name: {prefix}", 1)[1]
    body = step.split("        run: |\n", 1)[1]
    lines = []
    for line in body.splitlines():
        if line.strip() and not line.startswith(" " * 10):
            break
        lines.append(line[10:])
    return "\n".join(lines) + "\n"


PENDING = step_script("second-pending", "Mark the second review pending")
REPORT = step_script("second-report", "Post the second review")
BLOCKING = [{"severity": "blocking", "kind": "Failure paths", "title": "Drops the last warning", "detail": "d", "file": "app.py"}]


class Decisions(unittest.TestCase):
    def test_a_later_run_is_one_whose_id_is_higher(self) -> None:
        statuses = [status("failure", LATER), status("success", EARLIER)]
        self.assertEqual(ar.later_run(statuses, EARLIER), LATER)
        self.assertEqual(ar.later_run(statuses, LATER), 0)
        self.assertEqual(ar.later_run(statuses, LATER + 1), 0)
        self.assertEqual(ar.later_run([], EARLIER), 0)

    def test_only_this_workflow_s_second_review_statuses_count(self) -> None:
        self.assertEqual(ar.later_run([status("failure", LATER, creator="someone")], EARLIER), 0)
        self.assertEqual(ar.later_run([["Agent review", "failure", ar.BOT, "d", url(LATER)]], EARLIER), 0)
        self.assertEqual(ar.later_run([[ar.SECOND.context, "failure", ar.BOT, "d", "https://example.com/elsewhere"]], EARLIER), 0)
        self.assertEqual(ar.later_run([[ar.SECOND.context, "failure", ar.BOT, "d"]], EARLIER), 0)
        self.assertEqual(ar.later_run([[ar.SECOND.context, "failure", ar.BOT, "d", url(LATER) + "/x"]], EARLIER), 0)

    def test_the_newest_status_of_the_latest_run_needs_no_restoring(self) -> None:
        self.assertIsNone(ar.to_restore([]))
        self.assertIsNone(ar.to_restore([status("failure", LATER), status("success", EARLIER)]))
        self.assertIsNone(ar.to_restore([status("failure", LATER, copy=True), status("success", EARLIER), status("failure", LATER)]))

    def test_an_earlier_run_s_status_over_the_latest_run_s_is_restored(self) -> None:
        restore = ar.to_restore([status("success", EARLIER), status("failure", LATER, "1 blocking finding(s)")])
        assert restore is not None
        self.assertEqual((restore.state, restore.description, restore.url, restore.copy), ("failure", "1 blocking finding(s)", url(LATER), False))
        restore = ar.to_restore([status("success", EARLIER), status("pending", LATER)])
        assert restore is not None
        self.assertEqual(restore.state, "pending")

    def test_a_restored_copy_never_stands_for_the_run_s_own_newer_status(self) -> None:
        statuses = [status("pending", LATER, copy=True), status("failure", LATER), status("success", EARLIER), status("pending", LATER)]
        restore = ar.to_restore(statuses)
        assert restore is not None
        self.assertEqual((restore.state, restore.copy), ("failure", False))

    def test_the_comment_of_a_later_run_is_kept(self) -> None:
        later = ar.render(BLOCKING, "", url(LATER), HEAD, ar.SECOND, run=str(LATER))
        earlier = ar.render([], "", url(EARLIER), HEAD, ar.SECOND, run=str(EARLIER))
        old = ar.render([], "", url(EARLIER), HEAD, ar.SECOND)
        self.assertEqual(ar.comment_action([[5, later]], EARLIER), f"keep 5 {LATER}")
        self.assertEqual(ar.comment_action([[5, later]], LATER), "patch 5")
        self.assertEqual(ar.comment_action([[5, earlier]], LATER), "patch 5")
        self.assertEqual(ar.comment_action([[5, old]], EARLIER), "patch 5")
        self.assertEqual(ar.comment_action([[4, "<!-- agent-review -->\nx"], [5, earlier]], LATER), "patch 5")
        self.assertEqual(ar.comment_action([], LATER), "post")

    def test_the_state_line_carries_the_run_and_still_parses_without_it(self) -> None:
        comment = ar.render(BLOCKING, "", url(LATER), HEAD, ar.SECOND, record="c" * 16, run=str(LATER))
        self.assertEqual(comment.splitlines()[1], f"<!-- second-review-state sha={HEAD} verdict=blocking record={'c' * 16} run={LATER} -->")
        self.assertEqual(ar.previous([comment], ar.SECOND)[:2], (HEAD, "blocking"))
        self.assertEqual(ar.previous_record([comment], ar.SECOND), "c" * 16)
        self.assertEqual(ar.comment_run(comment), LATER)
        self.assertNotIn("run=", ar.render([], "", url(LATER), HEAD, ar.SECOND, run="12a"))
        self.assertNotIn("run=", ar.render([], "", url(LATER), HEAD, ar.SECOND, run="١٢"))
        old = ar.render([], "", url(LATER), HEAD, ar.SECOND)
        self.assertEqual(ar.comment_run(old), 0)
        self.assertEqual(ar.previous([old], ar.SECOND)[:2], (HEAD, "pass"))

    def test_the_report_puts_the_run_from_the_environment_in_the_second_review_s_comment_only(self) -> None:
        answer = {"type": "result", "is_error": False, "structured_output": {"summary": "s", "findings": []}}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp, "output.json")
            out.write_text(json.dumps(answer), encoding="utf-8")
            env = {"RUN_ID": str(LATER), "REVIEWED_SHA": HEAD, "RUN_URL": url(LATER)}
            with mock.patch.dict(os.environ, env):
                second = ar.report(str(out), ar.SECOND)["comment"]
                first = ar.report(str(out), ar.FIRST)["comment"]
        self.assertIn(f" run={LATER} -->", second.splitlines()[1])
        self.assertNotIn("run=", first)

    def test_the_commands(self) -> None:
        lines = "".join(json.dumps(s) + "\n" for s in [status("success", EARLIER), status("failure", LATER, "two\twords\nhere")])
        done = subprocess.run(["python3", "-I", str(cli.ROOT / "fleet_tools" / "agent_review.py"), "second", "later", str(EARLIER)],
                              input=lines, capture_output=True, text=True, check=True)
        self.assertEqual(done.stdout, f"{LATER}\n")
        done = subprocess.run(["python3", "-I", str(cli.ROOT / "fleet_tools" / "agent_review.py"), "second", "settle"],
                              input=lines, capture_output=True, text=True, check=True)
        self.assertEqual(done.stdout.splitlines(), ["failure", url(LATER) + ar.RESTORED, "two words here"])
        done = subprocess.run(["python3", "-I", str(cli.ROOT / "fleet_tools" / "agent_review.py"), "second", "settle"],
                              input=lines.splitlines()[1] + "\n", capture_output=True, text=True, check=True)
        self.assertEqual(done.stdout, "")


class Runs(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state = self.dir / "state"
        self.log = self.dir / "gh.log"
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").symlink_to(FAKE)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        self.runs = 0

    def env(self, run: int, sha: str, **more: str) -> dict[str, str]:
        return {"PATH": self.path, "HOME": os.environ.get("HOME", "/tmp"), "GH_TOKEN": "fake", "FAKE_GH_STATE": str(self.state),
                "FAKE_GH_LOG": str(self.log), "REPO": REPO, "PR": PR, "RUN_ID": str(run), "RUN_URL": url(run),
                "MERGE_GATE": "merge-gate.yml", "DRY_RUN": "false", **more}

    def pending_env(self, run: int, sha: str = HEAD) -> dict[str, str]:
        return self.env(run, sha, HEAD=sha, ACTION="labeled", SENDER="owner", OWNER="owner", LABEL="security",
                        TRIGGER_LABELS="safety-critical,security", FAKE_EVENT="")

    def report_env(self, run: int, verdict: str, sha: str = HEAD) -> dict[str, str]:
        findings = BLOCKING if verdict == "blocking" else []
        comment = ar.render(findings, "s", url(run), sha, ar.SECOND, "labelled `security`", "", str(run)) if verdict in ("pass", "blocking") else ""
        description = {"pass": "No blocking findings (0 advisory)", "blocking": "1 blocking finding(s); see the comment",
                       "skip": "Already reviewed a second time at this commit", "error": ""}[verdict]
        return self.env(run, sha, SHA=sha, RESULT="failure" if verdict == "error" else "success", VERDICT="" if verdict == "error" else verdict,
                        DESCRIPTION=description, COMMENT=base64.b64encode(comment.encode()).decode(),
                        FINDINGS=base64.b64encode(json.dumps(ar.findings_rows(findings)).encode()).decode(), INCREMENTAL="false",
                        TOUCHED="")

    def step(self, script: str, env: dict[str, str]) -> str:
        self.runs += 1
        work = self.dir / f"work{self.runs}"
        (work / "temp").mkdir(parents=True)
        (work / ".fleet").symlink_to(cli.ROOT)
        done = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script], cwd=work,
                              env={**env, "RUNNER_TEMP": str(work / "temp")}, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout

    def hook(self, script: str, env: dict[str, str]) -> str:
        self.runs += 1
        work = self.dir / f"work{self.runs}"
        (work / "temp").mkdir(parents=True)
        (work / ".fleet").symlink_to(cli.ROOT)
        file = self.dir / f"hook{self.runs}.sh"
        file.write_text(script, encoding="utf-8")
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in {**env, "RUNNER_TEMP": str(work / "temp")}.items())
        return f"cd {shlex.quote(str(work))} && env {exports} bash --noprofile --norc -eo pipefail {shlex.quote(str(file))} > /dev/null"

    def pending(self, run: int, sha: str = HEAD) -> str:
        return self.step(PENDING, self.pending_env(run, sha))

    def report(self, run: int, verdict: str, sha: str = HEAD, hook: str = "") -> str:
        env = self.report_env(run, verdict, sha)
        if hook:
            env = {**env, "FAKE_GH_HOOK": hook, "FAKE_GH_HOOK_ON": "/statuses/"}
        return self.step(REPORT, env)

    def statuses(self, sha: str = HEAD) -> list[Json]:
        file = self.state / "statuses" / f"{sha}.json"
        value = json.loads(file.read_text(encoding="utf-8")) if file.exists() else []
        assert isinstance(value, list)
        return value

    def newest(self, sha: str = HEAD) -> tuple[str, int]:
        top = self.statuses(sha)[0]
        link = ar.RUN_LINK.search(top["target_url"])
        assert link
        return str(top["state"]), int(link.group(1))

    def comment(self) -> str:
        comments = json.loads((self.state / "comments.json").read_text(encoding="utf-8"))
        bodies = [c["body"] for c in comments if c["body"].startswith(ar.SECOND.marker)]
        self.assertEqual(len(bodies), 1)
        return str(bodies[0])

    def assert_decided_by(self, run: int, state: str, verdict: str, sha: str = HEAD) -> None:
        self.assertEqual(self.newest(sha), (state, run))
        self.assertEqual(ar.comment_run(self.comment()), run)
        self.assertEqual(ar.previous([self.comment()], ar.SECOND)[:2], (sha, verdict))

    def test_push_then_label_the_label_run_blocks_first_and_the_push_run_s_pass_doesn_t_replace_it(self) -> None:
        self.pending(LATER)
        self.report(LATER, "blocking")
        out = self.report(EARLIER, "pass")
        self.assertIn(f"Run {LATER}, which started after this one", out)
        self.assert_decided_by(LATER, "failure", "blocking")

    def test_push_then_label_the_push_run_passes_first_and_the_status_stays_pending_until_the_label_run_blocks(self) -> None:
        self.pending(LATER)
        self.report(EARLIER, "pass")
        self.assertEqual(self.newest(), ("pending", LATER))
        self.assertEqual([s["state"] for s in self.statuses()], ["pending"])
        self.report(LATER, "blocking")
        self.assert_decided_by(LATER, "failure", "blocking")

    def test_label_then_push_the_later_push_run_s_block_stands_in_either_order(self) -> None:
        self.pending(EARLIER)
        self.report(LATER, "blocking")
        self.report(EARLIER, "pass")
        self.assert_decided_by(LATER, "failure", "blocking")

    def test_label_then_push_the_label_run_passes_first_then_the_push_run_blocks(self) -> None:
        self.pending(EARLIER)
        self.report(EARLIER, "pass")
        self.assertEqual(self.newest(), ("success", EARLIER))
        self.report(LATER, "blocking")
        self.assert_decided_by(LATER, "failure", "blocking")

    def test_the_later_run_decides_also_when_it_passes_whichever_finishes_first(self) -> None:
        self.report(EARLIER, "blocking")
        self.report(LATER, "pass")
        self.assert_decided_by(LATER, "success", "pass")
        self.setUp()
        self.report(LATER, "pass")
        self.report(EARLIER, "blocking")
        self.assert_decided_by(LATER, "success", "pass")

    def test_an_earlier_run_that_posts_between_the_later_run_s_check_and_post_is_put_back_under_it(self) -> None:
        later = self.hook(REPORT, self.report_env(LATER, "blocking"))
        out = self.report(EARLIER, "pass", hook=later)
        self.assertIn("posting its failure again", out)
        self.assertEqual(self.newest(), ("failure", LATER))
        self.assertTrue(self.statuses()[0]["target_url"].endswith(ar.RESTORED))
        self.assertEqual(ar.comment_run(self.comment()), LATER)
        self.assertEqual([s["state"] for s in self.statuses()], ["failure", "success", "failure"])

    def test_a_pending_put_back_by_an_earlier_run_gives_way_to_the_later_run_s_verdict(self) -> None:
        later = self.hook(PENDING, self.pending_env(LATER))
        self.report(EARLIER, "pass", hook=later)
        self.assertEqual(self.newest(), ("pending", LATER))
        self.report(LATER, "blocking")
        self.assertEqual(self.newest(), ("failure", LATER))
        self.assertIsNone(ar.to_restore([[ar.SECOND.context, s["state"], s["creator"]["login"], s["description"], s["target_url"]]
                                         for s in self.statuses()]))

    def test_a_re_run_of_the_earlier_run_posts_nothing_and_a_re_run_of_the_later_one_replaces_its_own_verdict(self) -> None:
        self.pending(LATER)
        self.report(LATER, "error")
        self.assertEqual(self.newest(), ("error", LATER))
        self.report(EARLIER, "pass")
        self.assertEqual(self.newest(), ("error", LATER))
        self.pending(LATER)
        self.report(LATER, "blocking")
        self.assert_decided_by(LATER, "failure", "blocking")
        self.report(LATER, "pass")
        self.assert_decided_by(LATER, "success", "pass")

    def test_a_push_to_a_new_head_keeps_its_comment_and_status_when_the_old_head_s_label_run_ends_later(self) -> None:
        self.report(LATER, "pass", sha=NEWER)
        self.report(EARLIER, "blocking", sha=HEAD)
        self.assertEqual(self.newest(NEWER), ("success", LATER))
        self.assertEqual(self.newest(HEAD), ("failure", EARLIER))
        self.assertEqual(ar.comment_run(self.comment()), LATER)
        self.assertEqual(ar.previous([self.comment()], ar.SECOND)[:2], (NEWER, "pass"))

    def test_a_single_run_posts_as_before(self) -> None:
        out = self.report(EARLIER, "pass")
        self.assert_decided_by(EARLIER, "success", "pass")
        self.assertIn("gh workflow run merge-gate.yml", self.log.read_text(encoding="utf-8"))
        self.assertIn("success: No blocking findings", out)
        self.report(LATER, "blocking", sha=NEWER)
        self.assert_decided_by(LATER, "failure", "blocking", sha=NEWER)
        self.assertEqual(self.log.read_text(encoding="utf-8").count("issues/7/comments -F"), 1)
        self.assertEqual(self.log.read_text(encoding="utf-8").count("PATCH"), 1)

    def test_a_skip_posts_nothing_and_an_error_fails_the_status(self) -> None:
        self.report(EARLIER, "skip")
        self.assertEqual(self.statuses(), [])
        self.assertFalse(self.log.exists())
        self.report(EARLIER, "error")
        self.assertEqual(self.newest(), ("error", EARLIER))

    def test_a_label_run_marks_the_head_pending_unless_a_later_run_has_set_it(self) -> None:
        self.pending(EARLIER)
        self.assertEqual(self.newest(), ("pending", EARLIER))
        self.setUp()
        self.report(LATER, "error")
        out = self.pending(EARLIER)
        self.assertIn(f"Run {LATER}, which started after this one", out)
        self.assertEqual(self.newest(), ("error", LATER))

    def test_a_dry_run_writes_nothing(self) -> None:
        self.step(REPORT, {**self.report_env(EARLIER, "blocking"), "DRY_RUN": "true"})
        self.step(PENDING, {**self.pending_env(EARLIER), "DRY_RUN": "true"})
        self.assertEqual(self.statuses(), [])
        self.assertNotIn("-X", self.log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
