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
SCRIPT = cli.ROOT / "fleet_tools" / "agent_review.py"
REPO = "example/app"
PR = "7"
HEAD = "a" * 40
NEWER = "b" * 40
EARLIER = 100
LATER = 200
LATEST = 300
PREFIX = f"https://github.com/{REPO}/actions/runs/"
Json = dict[str, Any]


def url(run: int) -> str:
    return f"{PREFIX}{run}"


def status(state: str, run: int, description: str = "d", creator: str = ar.BOT, copy: bool = False) -> list[str]:
    return [ar.SECOND.context, state, creator, description, url(run) + (ar.RESTORED if copy else "")]


def run_record(run: int, sha: str = HEAD, workflow: int = 1, event: str = "pull_request_target", pulls: tuple[int, ...] = (7,),
               completed: bool = False) -> Json:
    return {"id": run, "workflow_id": workflow, "event": event, "head_sha": sha, "pull_requests": [{"number": n} for n in pulls],
            "status": "completed" if completed else "in_progress"}


FOREIGN = (run_record(500, workflow=2), run_record(501, pulls=(8,)), run_record(502, sha=NEWER), run_record(503, event="pull_request"))


def run_line(record: Json) -> list[Any]:
    return [record["id"], record["workflow_id"], record["event"], record["head_sha"], [p["number"] for p in record["pull_requests"]],
            record["status"]]


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


def entries(*statuses: list[str]) -> list[ar.Entry]:
    return ar.entries(list(statuses), PREFIX)


class Decisions(unittest.TestCase):
    def test_only_a_run_of_this_workflow_event_pull_request_and_head_is_trusted(self) -> None:
        records = (run_record(EARLIER), run_record(LATER), *FOREIGN, run_record(505, pulls=()), run_record(506, completed=True))
        runs = ar.read_runs([run_line(r) for r in records])
        self.assertEqual(ar.trusted(runs, EARLIER, 7, HEAD), {EARLIER: False, LATER: False, 506: True})
        self.assertEqual(ar.trusted({}, EARLIER, 7, HEAD), {EARLIER: False})
        self.assertEqual(ar.read_runs([[1, 1, "e", HEAD, "not a list", "completed"], ["1", 1, "e", HEAD, [], "x"], [1]]), {})

    def test_a_status_counts_for_its_run_only_with_this_repository_s_exact_run_link(self) -> None:
        found = entries(status("success", LATER), [ar.SECOND.context, "success", ar.BOT, "d", f"https://evil.example/{REPO}/actions/runs/{LATER}"],
                        [ar.SECOND.context, "success", ar.BOT, "d", f"https://github.com/other/app/actions/runs/{LATER}"],
                        [ar.SECOND.context, "success", ar.BOT, "d", url(LATER) + "/x"], status("success", LATER, creator="someone"),
                        ["Agent review", "success", ar.BOT, "d", url(LATER)])
        self.assertEqual([e.run for e in found], [LATER, 0, 0, 0, 0])
        self.assertEqual(ar.run_of(url(LATER) + ar.RESTORED, PREFIX), (LATER, True))
        self.assertIsNone(ar.run_of(url(LATER), ""))
        self.assertEqual(ar.run_prefix(url(LATER)), PREFIX)
        self.assertEqual(ar.run_prefix("https://example.com/run"), "")

    def test_the_latest_trusted_run_decides(self) -> None:
        runs = {EARLIER: False, LATER: False}
        decided = ar.decide(ar.effective(entries(status("failure", LATER), status("success", EARLIER)), runs))
        self.assertEqual(decided, ar.Entry("failure", "d", LATER, False))
        decided = ar.decide(ar.effective(entries(status("pending", LATER), status("success", EARLIER)), runs))
        assert decided is not None
        self.assertEqual(decided.state, "pending")
        self.assertIsNone(ar.decide({}))

    def test_a_complete_blocking_review_stands_over_a_later_pass_but_not_over_the_owner_s_not_risky(self) -> None:
        runs = {EARLIER: False, LATER: False}
        decided = ar.decide(ar.effective(entries(status("success", LATER, "No blocking findings"), status("failure", EARLIER)), runs))
        assert decided is not None
        self.assertEqual((decided.state, decided.run), ("failure", EARLIER))
        decided = ar.decide(ar.effective(entries(status("success", LATER, ar.NOT_RISKY), status("failure", EARLIER)), runs))
        assert decided is not None
        self.assertEqual((decided.state, decided.run), ("success", LATER))
        decided = ar.decide(ar.effective(entries(status("error", LATER), status("failure", EARLIER)), runs))
        assert decided is not None
        self.assertEqual((decided.state, decided.run), ("error", LATER))

    def test_an_untrusted_or_finished_pending_status_counts_for_nothing(self) -> None:
        found = entries(status("success", 999), status("pending", LATER), status("success", EARLIER))
        self.assertEqual(ar.effective(found, {EARLIER: False, LATER: True}), {EARLIER: ar.Entry("success", "d", EARLIER, False)})
        self.assertEqual(set(ar.effective(found, {EARLIER: False, LATER: False})), {EARLIER, LATER})

    def test_what_a_run_posts(self) -> None:
        runs = {EARLIER: False, LATER: False}
        self.assertEqual(ar.publish(entries(status("failure", LATER)), runs, EARLIER, "success", "p"), None)
        self.assertEqual(ar.publish(entries(status("pending", LATER)), runs, EARLIER, "failure", "b"), ("failure", "b"))
        self.assertEqual(ar.publish(entries(status("success", LATER, "p")), runs, EARLIER, "failure", "b"), ("failure", "b"))
        self.assertEqual(ar.publish(entries(status("failure", EARLIER, "b")), runs, LATER, "success", "p"), ("failure", ar.ON_BEHALF + "b"))
        self.assertEqual(ar.publish(entries(status("failure", EARLIER, ar.ON_BEHALF + "b")), runs, LATER, "success", "p"),
                         ("failure", ar.ON_BEHALF + "b"))
        self.assertEqual(ar.publish(entries(status("failure", EARLIER)), runs, LATER, "success", ar.NOT_RISKY), ("success", ar.NOT_RISKY))
        self.assertEqual(ar.publish(entries(status("success", 999)), runs, EARLIER, "success", "p"), ("success", "p"))
        self.assertEqual(ar.publish([], {EARLIER: False}, EARLIER, "error", "e"), ("error", "e"))

    def test_the_decided_status_goes_back_on_top(self) -> None:
        runs = {EARLIER: False, LATER: False}
        self.assertIsNone(ar.to_restore([], runs))
        self.assertIsNone(ar.to_restore(entries(status("failure", LATER), status("success", EARLIER)), runs))
        self.assertIsNone(ar.to_restore(entries(status("failure", LATER, copy=True), status("success", EARLIER), status("failure", LATER)), runs))
        restore = ar.to_restore(entries(status("success", EARLIER), status("failure", LATER, "b")), runs)
        self.assertEqual(restore, ar.Entry("failure", "b", LATER, False))
        restore = ar.to_restore(entries(status("pending", LATER, copy=True), status("failure", LATER), status("success", EARLIER),
                                        status("pending", LATER)), runs)
        assert restore is not None
        self.assertEqual(restore.state, "failure")
        self.assertEqual(ar.to_restore(entries(status("success", 999), status("failure", LATER)), runs), ar.Entry("failure", "d", LATER, False))
        self.assertEqual(ar.to_restore(entries([ar.SECOND.context, "success", "someone", "", ""], status("failure", LATER)), runs),
                         ar.Entry("failure", "d", LATER, False))

    def test_the_comment(self) -> None:
        runs = ar.read_runs([run_line(run_record(EARLIER)), run_line(run_record(LATER)), run_line(run_record(LATEST, sha=NEWER))])
        later = ar.render(BLOCKING, "", url(LATER), HEAD, ar.SECOND, run=str(LATER))
        newer = ar.render([], "", url(LATEST), NEWER, ar.SECOND, run=str(LATEST))
        forged = ar.render([], "", url(999), NEWER, ar.SECOND, run="999")
        self.assertEqual(ar.comment_action([[5, newer]], runs, EARLIER, 7, HEAD, True), f"keep 5 {LATEST}")
        self.assertEqual(ar.comment_action([[5, later]], runs, EARLIER, 7, HEAD, False), f"keep 5 {LATER}")
        self.assertEqual(ar.comment_action([[5, later]], runs, EARLIER, 7, HEAD, True), "patch 5")
        self.assertEqual(ar.comment_action([[5, forged]], runs, EARLIER, 7, HEAD, True), "patch 5")
        self.assertEqual(ar.comment_action([[4, "<!-- agent-review -->\nx"], [5, later]], runs, LATER, 7, HEAD, True), "patch 5")
        self.assertEqual(ar.comment_action([], runs, LATER, 7, HEAD, True), "post")

    def test_the_state_line_carries_the_run_and_still_parses_without_it(self) -> None:
        comment = ar.render(BLOCKING, "", url(LATER), HEAD, ar.SECOND, record="c" * 16, run=str(LATER))
        self.assertEqual(comment.splitlines()[1], f"<!-- second-review-state sha={HEAD} verdict=blocking record={'c' * 16} run={LATER} -->")
        self.assertEqual(ar.previous([comment], ar.SECOND)[:2], (HEAD, "blocking"))
        self.assertEqual(ar.previous_record([comment], ar.SECOND), "c" * 16)
        self.assertEqual(ar.comment_state(comment), (HEAD, LATER))
        self.assertNotIn("run=", ar.render([], "", url(LATER), HEAD, ar.SECOND, run="12a"))
        self.assertNotIn("run=", ar.render([], "", url(LATER), HEAD, ar.SECOND, run="١٢"))
        old = ar.render([], "", url(LATER), HEAD, ar.SECOND)
        self.assertEqual(ar.comment_run(old), 0)
        self.assertEqual(ar.previous([old], ar.SECOND)[:2], (HEAD, "pass"))

    def test_the_report_puts_the_run_in_the_second_review_s_comment_only(self) -> None:
        answer = {"type": "result", "is_error": False, "structured_output": {"summary": "s", "findings": []}}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp, "output.json")
            out.write_text(json.dumps(answer), encoding="utf-8")
            with mock.patch.dict(os.environ, {"RUN_ID": str(LATER), "REVIEWED_SHA": HEAD, "RUN_URL": url(LATER)}):
                second = ar.report(str(out), ar.SECOND)["comment"]
                first = ar.report(str(out), ar.FIRST)["comment"]
        self.assertIn(f" run={LATER} -->", second.splitlines()[1])
        self.assertNotIn("run=", first)

    def test_a_re_run_s_own_earlier_verdict_isn_t_the_head_already_reviewed(self) -> None:
        def wanted(statuses: list[list[str]], run: int) -> list[str]:
            env = {**os.environ, "RUN_URL": url(run), "ACTION": "labeled", "LABEL": "security", "TRIGGER_LABELS": "security", "LABELS": "[]"}
            done = subprocess.run(["python3", "-I", str(SCRIPT), "second", "wanted", "labeled"],
                                  input="".join(json.dumps(s) + "\n" for s in statuses), env=env, capture_output=True, text=True, check=True)
            return done.stdout.splitlines()
        self.assertIn("reviewed=false", wanted([status("failure", LATER), status("success", EARLIER)], LATER))
        self.assertIn("pending=true", wanted([status("failure", LATER, copy=True)], LATER))
        self.assertIn("reviewed=true", wanted([status("failure", LATER)], LATEST))
        self.assertIn("reviewed=true", wanted([status("failure", LATER), status("failure", EARLIER)], EARLIER))

    def test_the_commands(self) -> None:
        statuses = "".join(json.dumps(s) + "\n" for s in [status("success", EARLIER), status("failure", LATER, "two\twords\nhere"), status("x", 999)])
        with tempfile.TemporaryDirectory() as tmp:
            comments, runs = Path(tmp, "comments.jsonl"), Path(tmp, "runs.jsonl")
            comments.write_text(json.dumps([5, ar.render([], "", url(LATEST), HEAD, ar.SECOND, run=str(LATEST))]) + "\n", encoding="utf-8")
            runs.write_text("".join(json.dumps(run_line(run_record(r))) + "\n" for r in (EARLIER, LATER)), encoding="utf-8")
            env = {**os.environ, "RUN_ID": str(EARLIER), "RUN_URL": url(EARLIER), "PR": PR, "DESCRIPTION": "p"}

            def command(*args: str, stdin: str = statuses) -> str:
                return subprocess.run(["python3", "-I", str(SCRIPT), "second", *args], input=stdin, env=env, capture_output=True, text=True,
                                      check=True).stdout
            self.assertEqual(command("runs", str(comments)).split(), [str(EARLIER), str(LATER), "999", str(LATEST)])
            self.assertEqual(command("settle", HEAD, str(runs)).splitlines(), ["failure", url(LATER) + ar.RESTORED, "two words here"])
            self.assertEqual(command("settle", HEAD, str(runs), stdin=json.dumps(status("failure", LATER)) + "\n"), "")
            self.assertEqual(command("publish", HEAD, str(runs), str(comments), "success").splitlines(),
                             ["status=", "description=", f"decided_by={LATER}", "comment=patch 5"])
            self.assertNotEqual(subprocess.run(["python3", "-I", str(SCRIPT), "second", "settle"], env=env, input="",
                                               capture_output=True, text=True).returncode, 0)


class Runs(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state = self.dir / "state"
        self.state.mkdir()
        self.log = self.dir / "gh.log"
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").symlink_to(FAKE)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        self.steps = 0
        self.records: dict[str, Json] = {}
        for record in (run_record(EARLIER), run_record(LATER), run_record(LATEST), *FOREIGN):
            self.register(record)

    def register(self, record: Json) -> None:
        self.records[str(record["id"])] = record
        (self.state / "runs.json").write_text(json.dumps(self.records), encoding="utf-8")

    def env(self, run: int, **more: str) -> dict[str, str]:
        return {"PATH": self.path, "HOME": os.environ.get("HOME", "/tmp"), "GH_TOKEN": "fake", "FAKE_GH_STATE": str(self.state),
                "FAKE_GH_LOG": str(self.log), "REPO": REPO, "PR": PR, "RUN_ID": str(run), "RUN_URL": url(run),
                "MERGE_GATE": "merge-gate.yml", "DRY_RUN": "false", **more}

    def pending_env(self, run: int, sha: str = HEAD) -> dict[str, str]:
        return self.env(run, HEAD=sha, ACTION="labeled", SENDER="owner", OWNER="owner", LABEL="security",
                        TRIGGER_LABELS="safety-critical,security", FAKE_EVENT="")

    def report_env(self, run: int, verdict: str, sha: str = HEAD, description: str = "") -> dict[str, str]:
        findings = BLOCKING if verdict == "blocking" else []
        comment = ar.render(findings, "s", url(run), sha, ar.SECOND, "labelled `security`", "", str(run)) if verdict in ("pass", "blocking") else ""
        description = description or {"pass": "No blocking findings (0 advisory)", "blocking": "1 blocking finding(s); see the comment",
                                       "skip": "Already reviewed a second time at this commit", "error": ""}[verdict]
        if description == ar.NOT_RISKY:
            comment = ""
        return self.env(run, SHA=sha, RESULT="failure" if verdict == "error" else "success", VERDICT="" if verdict == "error" else verdict,
                        DESCRIPTION=description, COMMENT=base64.b64encode(comment.encode()).decode(),
                        FINDINGS=base64.b64encode(json.dumps(ar.findings_rows(findings)).encode()).decode(), INCREMENTAL="false",
                        TOUCHED="")

    def workdir(self) -> Path:
        self.steps += 1
        work = self.dir / f"work{self.steps}"
        (work / "temp").mkdir(parents=True)
        (work / ".fleet").symlink_to(cli.ROOT)
        return work

    def step(self, script: str, env: dict[str, str]) -> str:
        work = self.workdir()
        done = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script], cwd=work,
                              env={**env, "RUNNER_TEMP": str(work / "temp")}, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout

    def hook(self, script: str, env: dict[str, str]) -> str:
        work = self.workdir()
        file = self.dir / f"hook{self.steps}.sh"
        file.write_text(script, encoding="utf-8")
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in {**env, "RUNNER_TEMP": str(work / "temp")}.items())
        return f"cd {shlex.quote(str(work))} && env {exports} bash --noprofile --norc -eo pipefail {shlex.quote(str(file))} > /dev/null"

    def forge(self, state: str, target_url: str, sha: str = HEAD) -> str:
        return (f"FAKE_GH_STATE={shlex.quote(str(self.state))} FAKE_GH_LOG={shlex.quote(str(self.log))} PATH={shlex.quote(self.path)} "
                f"gh api -X POST repos/{REPO}/statuses/{sha} -f state={state} -f context='Second review' -f description=forged "
                f"-f target_url={shlex.quote(target_url)} > /dev/null")

    def pending(self, run: int, sha: str = HEAD) -> str:
        return self.step(PENDING, self.pending_env(run, sha))

    def report(self, run: int, verdict: str, sha: str = HEAD, hook: str = "", description: str = "") -> str:
        env = self.report_env(run, verdict, sha, description)
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
        found = ar.run_of(top["target_url"], PREFIX)
        return str(top["state"]), found[0] if found else 0

    def comment(self) -> str:
        comments = json.loads((self.state / "comments.json").read_text(encoding="utf-8"))
        bodies = [c["body"] for c in comments if c["body"].startswith(ar.SECOND.marker)]
        self.assertEqual(len(bodies), 1)
        return str(bodies[0])

    def assert_decided_by(self, run: int, state: str, verdict: str, sha: str = HEAD, comment_run: int = 0) -> None:
        self.assertEqual(self.newest(sha), (state, run))
        self.assertEqual(ar.comment_run(self.comment()), comment_run or run)
        self.assertEqual(ar.previous([self.comment()], ar.SECOND)[:2], (sha, verdict))

    def test_push_then_label_the_label_run_blocks_first_and_the_push_run_s_pass_doesn_t_replace_it(self) -> None:
        self.pending(LATER)
        self.report(LATER, "blocking")
        out = self.report(EARLIER, "pass")
        self.assertIn(f"Run {LATER} decides", out)
        self.assert_decided_by(LATER, "failure", "blocking")

    def test_push_then_label_the_push_run_passes_first_and_the_status_stays_pending_until_the_label_run_blocks(self) -> None:
        self.pending(LATER)
        self.report(EARLIER, "pass")
        self.assertEqual([s["state"] for s in self.statuses()], ["pending"])
        self.report(LATER, "blocking")
        self.assert_decided_by(LATER, "failure", "blocking")

    def test_label_then_push_the_later_push_run_s_block_stands_in_either_order(self) -> None:
        self.pending(EARLIER)
        self.report(LATER, "blocking")
        self.report(EARLIER, "pass")
        self.assert_decided_by(LATER, "failure", "blocking")
        self.setUp()
        self.pending(EARLIER)
        self.report(EARLIER, "pass")
        self.assertEqual(self.newest(), ("success", EARLIER))
        self.report(LATER, "blocking")
        self.assert_decided_by(LATER, "failure", "blocking")

    def test_an_earlier_complete_blocking_review_stands_over_a_later_pass_whichever_finishes_first(self) -> None:
        self.report(EARLIER, "blocking")
        out = self.report(LATER, "pass")
        self.assertIn("An earlier run's blocking verdict stands", out)
        self.assertEqual(self.newest(), ("failure", LATER))
        self.assertTrue(self.statuses()[0]["description"].startswith(ar.ON_BEHALF))
        self.assert_decided_by(LATER, "failure", "blocking", comment_run=EARLIER)
        self.setUp()
        self.report(LATER, "pass")
        self.report(EARLIER, "blocking")
        self.assert_decided_by(EARLIER, "failure", "blocking")
        self.report(LATEST, "pass")
        self.assertEqual(self.newest()[0], "failure")

    def test_the_owner_s_not_risky_after_removing_the_label_still_clears_a_blocking_verdict(self) -> None:
        self.report(EARLIER, "blocking")
        self.report(LATER, "pass", description=ar.NOT_RISKY)
        self.assertEqual(self.newest(), ("success", LATER))
        self.setUp()
        self.report(LATER, "pass", description=ar.NOT_RISKY)
        self.report(EARLIER, "blocking")
        self.assertEqual(self.newest(), ("success", LATER))

    def test_an_earlier_run_that_posts_between_the_later_run_s_check_and_post_is_put_back_under_it(self) -> None:
        out = self.report(EARLIER, "pass", hook=self.hook(REPORT, self.report_env(LATER, "blocking")))
        self.assertIn("posting the decided failure again", out)
        self.assertEqual(self.newest(), ("failure", LATER))
        self.assertTrue(self.statuses()[0]["target_url"].endswith(ar.RESTORED))
        self.assertEqual(ar.comment_run(self.comment()), LATER)

    def test_a_pending_put_back_by_an_earlier_run_gives_way_to_the_later_run_s_verdict(self) -> None:
        self.report(EARLIER, "pass", hook=self.hook(PENDING, self.pending_env(LATER)))
        self.assertEqual(self.newest(), ("pending", LATER))
        self.report(LATER, "blocking")
        self.assertEqual(self.newest(), ("failure", LATER))

    def test_a_push_run_s_verdict_clears_the_pending_of_a_label_run_that_was_cancelled(self) -> None:
        self.pending(LATER)
        self.register(run_record(LATER, completed=True))
        self.report(EARLIER, "pass")
        self.assert_decided_by(EARLIER, "success", "pass")

    def test_a_re_run_of_the_earlier_run_posts_nothing_and_a_re_run_of_the_later_one_replaces_its_own_verdict(self) -> None:
        self.pending(LATER)
        self.report(LATER, "error")
        self.assertEqual(self.newest(), ("error", LATER))
        self.report(EARLIER, "pass")
        self.assertEqual(self.newest(), ("error", LATER))
        self.assertIn("pending=true", self.pending(LATER))
        self.assertEqual(self.newest(), ("pending", LATER))
        self.report(LATER, "blocking")
        self.assert_decided_by(LATER, "failure", "blocking")
        self.assertIn("pending=true", self.pending(LATER))
        self.report(LATER, "pass")
        self.assert_decided_by(LATER, "success", "pass")

    def test_a_push_to_a_new_head_keeps_its_comment_and_status_when_the_old_head_s_label_run_ends_later(self) -> None:
        self.register(run_record(LATER, sha=NEWER))
        self.report(LATER, "pass", sha=NEWER)
        self.report(EARLIER, "blocking", sha=HEAD)
        self.assertEqual(self.newest(NEWER), ("success", LATER))
        self.assertEqual(self.newest(HEAD), ("failure", EARLIER))
        self.assertEqual(ar.comment_run(self.comment()), LATER)
        self.assertEqual(ar.previous([self.comment()], ar.SECOND)[:2], (NEWER, "pass"))

    def test_a_forged_status_or_comment_never_stops_the_real_verdict_from_being_posted(self) -> None:
        for forged in (url(99999999999), f"https://evil.example/{REPO}/actions/runs/{LATER}", f"https://github.com/other/app/actions/runs/{LATER}",
                       url(500), url(501), url(502), url(503)):
            with self.subTest(forged=forged):
                self.setUp()
                for state in ("success", "pending"):
                    subprocess.run(["bash", "-c", self.forge(state, forged)], check=True)
                body = ar.render([], "", forged, HEAD, ar.SECOND, run=forged.rsplit("/", 1)[1])
                comments = [{"id": 1, "user": {"login": ar.BOT}, "author_association": "NONE", "html_url": "", "body": body}]
                (self.state / "comments.json").write_text(json.dumps(comments), encoding="utf-8")
                out = self.report(EARLIER, "blocking")
                self.assertNotIn("decides this head", out)
                self.assert_decided_by(EARLIER, "failure", "blocking")

    def test_a_forged_status_posted_just_before_the_real_one_stays_under_it(self) -> None:
        self.report(EARLIER, "blocking", hook=self.forge("success", url(99999999999)))
        self.assertEqual(self.newest(), ("failure", EARLIER))
        self.assertEqual([s["state"] for s in self.statuses()], ["failure", "success"])

    def test_a_run_that_can_t_be_looked_up_counts_for_nothing(self) -> None:
        self.pending(LATER)
        self.records.pop(str(LATER))
        (self.state / "runs.json").write_text(json.dumps(self.records), encoding="utf-8")
        out = self.report(EARLIER, "blocking")
        self.assertIn(f"Run {LATER} couldn't be looked up", out)
        self.assert_decided_by(EARLIER, "failure", "blocking")

    def test_a_single_run_posts_as_before(self) -> None:
        out = self.report(EARLIER, "pass")
        self.assert_decided_by(EARLIER, "success", "pass")
        self.assertIn("gh workflow run merge-gate.yml", self.log.read_text(encoding="utf-8"))
        self.assertIn("success: No blocking findings", out)
        self.register(run_record(LATER, sha=NEWER))
        self.report(LATER, "blocking", sha=NEWER)
        self.assert_decided_by(LATER, "failure", "blocking", sha=NEWER)
        log = self.log.read_text(encoding="utf-8")
        self.assertEqual(log.count("issues/7/comments -F"), 1)
        self.assertEqual(log.count("PATCH"), 1)

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
        self.assertIn("A run that started after this one", out)
        self.assertEqual(self.newest(), ("error", LATER))

    def test_a_dry_run_writes_nothing(self) -> None:
        self.step(REPORT, {**self.report_env(EARLIER, "blocking"), "DRY_RUN": "true"})
        self.step(PENDING, {**self.pending_env(EARLIER), "DRY_RUN": "true"})
        self.assertEqual(self.statuses(), [])
        self.assertNotIn("-X", self.log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
