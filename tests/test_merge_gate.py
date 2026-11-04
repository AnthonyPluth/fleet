import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fleet_tools import cli, self_check

SCRIPT = cli.ROOT / ".github" / "scripts" / "merge-gate.sh"
FAKES = cli.ROOT / "tests" / "fakes"
REPO = "example/app"
SHA = "a" * 40
NUMBER = 12
DEPENDABOT = "dependabot[bot]"
Json = dict[str, Any]


def pull(
    number: int = NUMBER, author: str = "octocat", sha: str = SHA, head_repo: str = REPO, draft: bool = False
) -> Json:
    return {"number": number, "user": {"login": author}, "head": {"sha": sha, "repo": {"full_name": head_repo}}, "draft": draft}


def run(
    name: str,
    status: str = "completed",
    conclusion: str | None = "success",
    created_at: str = "2026-01-01T10:00:00Z",
    workflow_id: int | None = None,
) -> Json:
    return {
        "name": name,
        "workflow_id": workflow_id if workflow_id is not None else abs(hash(name)) % 10000,
        "event": "pull_request",
        "path": f".github/workflows/{name.lower().replace(' ', '-')}.yml",
        "status": status,
        "conclusion": conclusion,
        "created_at": created_at,
    }


def codeql_run(status: str = "completed", conclusion: str | None = "success") -> Json:
    return {
        **run(f"PR #{NUMBER}", status, conclusion, workflow_id=99),
        "event": "dynamic",
        "path": "dynamic/github-code-scanning/codeql",
    }


def check(app: str, name: str, status: str = "completed", conclusion: str | None = "success") -> Json:
    return {"app": {"slug": app}, "name": name, "status": status, "conclusion": conclusion}


def status(context: str, state: str = "success") -> Json:
    return {"context": context, "state": state}


def thread(body: str | None, resolved: bool = False, author: str | None = "chatgpt-codex-connector") -> Json:
    node: Json = {"body": body, "author": None if author is None else {"login": author}}
    return {"isResolved": resolved, "comments": {"nodes": [] if body is None else [node]}}


def commit(author: str = DEPENDABOT, verified: bool = True) -> Json:
    return {"sha": "c" * 40, "author": {"login": author}, "commit": {"verification": {"verified": verified}}}


def changed_file(filename: str, patch: str) -> Json:
    return {"filename": filename, "patch": patch}


class Result:
    def __init__(self, completed: subprocess.CompletedProcess[str], log: Path) -> None:
        self.returncode = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr
        self.calls = [shlex.split(line) for line in log.read_text(encoding="utf-8").splitlines()]

    @property
    def posted(self) -> list[tuple[str, str]]:
        posted = []
        for call in self.calls:
            if call[1:3] != ["api", "-X"] or "/statuses/" not in call[4]:
                continue
            fields = dict(call[index + 1].split("=", 1) for index, word in enumerate(call) if word == "-f")
            posted.append((fields["state"], fields["description"]))
        return posted

    @property
    def merged(self) -> bool:
        return any(call[1:3] == ["pr", "merge"] for call in self.calls)

    def reads_of(self, path: str) -> int:
        return sum(1 for call in self.calls if call[1] == "api" and "-X" not in call and any(path in word for word in call))


class Gate:
    def __init__(self, test: unittest.TestCase) -> None:
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.answers = self.root / "answers"
        self.answers.mkdir()
        self.log = self.root / "gh.log"
        self.log.touch()
        self.pulls([pull()])
        self.runs([run("Security scans"), codeql_run()])
        self.checks([check("github-code-scanning", "CodeQL"), check("codecov", "codecov/patch", conclusion="failure")])
        self.statuses([status("Second review")])
        self.threads([])

    def record(self, name: str, answer: object) -> None:
        (self.answers / f"{name}.json").write_text(json.dumps(answer), encoding="utf-8")

    def record_series(self, name: str, answers: list[object]) -> None:
        for index, answer in enumerate(answers, start=1):
            self.record(f"{name}.{index}", answer)

    def pulls(self, pulls: list[Json]) -> None:
        self.record("pulls", pulls)

    def runs(self, runs: list[Json]) -> None:
        self.record("actions__runs", {"workflow_runs": runs})

    def checks(self, checks: list[Json]) -> None:
        self.record("commits__SHA__check-runs", {"check_runs": checks})

    def statuses(self, statuses: list[Json]) -> None:
        self.record("commits__SHA__statuses", statuses)

    def threads(self, threads: list[Json]) -> None:
        self.record("graphql", {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": threads}}}}})

    def commits(self, commits: list[Json]) -> None:
        self.record("pulls__N__commits", commits)

    def files(self, files: list[Json]) -> None:
        self.record("pulls__N__files", files)

    def reviews(self, reviews: list[Json]) -> None:
        self.record("pulls__N__reviews", reviews)

    def comments(self, comments: list[Json]) -> None:
        self.record("issues__N__comments", comments)

    def dependabot(self, files: list[Json] | None = None) -> None:
        self.pulls([pull(author=DEPENDABOT)])
        self.runs([run("Security scans")])
        self.statuses([])
        self.commits([commit()])
        self.files(files if files is not None else [changed_file("poetry.lock", '-version = "1.0"\n+version = "1.1"')])

    def run(self, **overrides: str | None) -> Result:
        env: dict[str, str | None] = {
            "PATH": f"{FAKES}{os.pathsep}{os.environ['PATH']}",
            "LC_ALL": "C",
            "FAKE_GH_DIR": str(self.answers),
            "FAKE_GH_LOG": str(self.log),
            "GH_TOKEN": "fake",
            "REPO": REPO,
            "SHA": SHA,
            "POLL_SECONDS": "0",
            "POLL_ATTEMPTS": "2",
        }
        env.update(overrides)
        self.log.write_text("", encoding="utf-8")
        for counter in self.answers.glob(".*.reads"):
            counter.unlink()
        completed = subprocess.run(
            [str(SCRIPT)],
            env={key: value for key, value in env.items() if value is not None},
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        return Result(completed, self.log)


P1 = "**<sub><sub>![P1 Badge](https://example.test/P1)</sub></sub> Add a check**"
P2 = "![P2 Badge](https://example.test/P2) Rename it"
P3 = "![P3 Badge](https://example.test/P3) A nit"


class ThreadsTest(unittest.TestCase):
    def test_an_open_p1_or_p2_thread_fails_the_gate(self) -> None:
        gate = Gate(self)
        gate.threads([thread(P1), thread(P2)])
        self.assertEqual(gate.run().posted, [("failure", "Failed: 2 open P1|P2 review comments")])

    def test_one_open_thread_is_singular(self) -> None:
        gate = Gate(self)
        gate.threads([thread(P2)])
        self.assertEqual(gate.run().posted, [("failure", "Failed: 1 open P1|P2 review comment")])

    def test_resolved_lower_priority_plain_and_empty_threads_do_not_count(self) -> None:
        gate = Gate(self)
        gate.threads([thread(P1, resolved=True), thread(P3), thread("a person mentioning P1"), thread(None)])
        self.assertEqual(gate.run().posted, [("success", "Every check passed")])

    def test_the_badges_can_be_changed_and_empty_turns_the_check_off(self) -> None:
        gate = Gate(self)
        gate.threads([thread(P3)])
        self.assertEqual(gate.run(BLOCKING_THREAD_BADGES="P1|P2|P3").posted, [("failure", "Failed: 1 open P1|P2|P3 review comment")])
        gate.threads([thread(P1)])
        result = gate.run(BLOCKING_THREAD_BADGES="")
        self.assertEqual(result.posted, [("success", "Every check passed")])
        self.assertEqual(result.reads_of("graphql"), 0)

    def test_only_the_configured_authors_count(self) -> None:
        gate = Gate(self)
        gate.threads([thread(P1, author="octocat"), thread(P1, author=None), thread(P2, author="chatgpt-codex-connector[bot]")])
        self.assertEqual(gate.run().posted, [("failure", "Failed: 1 open P1|P2 review comment")])
        self.assertEqual(gate.run(BLOCKING_THREAD_AUTHORS="octocat|hubot").posted, [("failure", "Failed: 1 open P1|P2 review comment")])
        self.assertEqual(gate.run(BLOCKING_THREAD_AUTHORS="").posted, [("failure", "Failed: 3 open P1|P2 review comments")])

    def test_an_author_setting_that_is_not_a_plain_alternation_is_refused(self) -> None:
        result = Gate(self).run(BLOCKING_THREAD_AUTHORS='x") | . | ("')
        self.assertEqual(result.returncode, 1)
        self.assertIn("BLOCKING_THREAD_AUTHORS must be", result.stdout)
        self.assertEqual(result.posted, [])

    def test_a_badge_setting_that_is_not_a_plain_alternation_is_refused(self) -> None:
        result = Gate(self).run(BLOCKING_THREAD_BADGES="P1)|.*(")
        self.assertEqual(result.returncode, 1)
        self.assertIn("BLOCKING_THREAD_BADGES must be", result.stdout)
        self.assertEqual(result.posted, [])

    def test_a_query_that_fails_leaves_the_gate_pending(self) -> None:
        gate = Gate(self)
        (gate.answers / "graphql.json").unlink()
        result = gate.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.posted, [("pending", "Waiting for: review threads (couldn't be read)")])

    def test_a_dependabot_pull_request_is_checked_too(self) -> None:
        gate = Gate(self)
        gate.dependabot()
        gate.threads([thread(P1)])
        result = gate.run()
        self.assertEqual(result.posted, [("failure", "Failed: 1 open P1|P2 review comment")])
        self.assertFalse(result.merged)


class DecisionTest(unittest.TestCase):
    def test_all_green_is_success(self) -> None:
        result = Gate(self).run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.posted, [("success", "Every check passed")])
        self.assertFalse(result.merged)

    def test_a_required_status_missing_is_pending(self) -> None:
        gate = Gate(self)
        gate.statuses([status("Agent review")])
        result = gate.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.posted, [("pending", "Waiting for: Second review (not started)")])

    def test_second_review_failing_is_failure(self) -> None:
        gate = Gate(self)
        gate.statuses([status("Second review", "failure")])
        result = gate.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.posted, [("failure", "Failed: Second review (failure)")])

    def test_a_required_workflow_that_has_not_run_is_pending(self) -> None:
        gate = Gate(self)
        gate.runs([codeql_run()])
        self.assertEqual(gate.run().posted, [("pending", "Waiting for: Security scans (not started)")])

    def test_a_failing_workflow_fails_and_a_skipped_one_passes(self) -> None:
        gate = Gate(self)
        gate.runs([run("Security scans", conclusion="skipped"), run("Tests", conclusion="failure"), codeql_run()])
        self.assertEqual(gate.run().posted, [("failure", "Failed: Tests (failure)")])

    def test_the_newest_run_of_a_workflow_wins(self) -> None:
        gate = Gate(self)
        gate.runs([
            run("Security scans", conclusion="failure", created_at="2026-01-01T09:00:00Z", workflow_id=7),
            run("Security scans", created_at="2026-01-01T11:00:00Z", workflow_id=7),
            codeql_run(),
        ])
        self.assertEqual(gate.run().posted, [("success", "Every check passed")])

    def test_another_apps_failing_check_fails_and_an_ignored_apps_does_not(self) -> None:
        gate = Gate(self)
        gate.checks([check("codecov", "codecov/project", conclusion="failure"), check("semgrep", "Semgrep", conclusion="failure")])
        self.assertEqual(gate.run().posted, [("failure", "Failed: Semgrep (failure)")])
        gate = Gate(self)
        gate.checks([check("codecov", "codecov/project", conclusion="failure"), check("trivy", "Trivy", status="in_progress", conclusion=None)])
        self.assertEqual(gate.run().posted, [("pending", "Waiting for: Trivy")])

    def test_a_pending_status_waits_and_the_gates_own_status_is_ignored(self) -> None:
        gate = Gate(self)
        gate.statuses([status("Merge gate", "failure"), status("Agent review", "pending"), status("Second review")])
        self.assertEqual(gate.run().posted, [("pending", "Waiting for: Agent review")])

    def test_the_newest_status_of_a_context_wins(self) -> None:
        gate = Gate(self)
        gate.statuses([status("Agent review"), status("Agent review", "failure"), status("Second review")])
        self.assertEqual(gate.run().posted, [("success", "Every check passed")])

    def test_two_pull_requests_on_one_head_is_failure(self) -> None:
        gate = Gate(self)
        gate.pulls([pull(), pull(number=13, author="hubot")])
        result = gate.run()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.posted, [("failure", "More than one open pull request has this commit as its head")])
        self.assertIn("::error::More than one open pull request", result.stdout)

    def test_no_open_pull_request_decides_nothing(self) -> None:
        gate = Gate(self)
        gate.pulls([pull(sha="b" * 40)])
        result = gate.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.posted, [])
        self.assertIn(f"No open pull request into main at {SHA}.", result.stdout)

    def test_a_pull_request_number_finds_its_head(self) -> None:
        gate = Gate(self)
        gate.record("pulls__N", {"head": {"sha": SHA}})
        result = gate.run(SHA=None, PR=str(NUMBER))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.posted, [("success", "Every check passed")])
        self.assertIn(f"Pull request #{NUMBER} by octocat at {SHA}", result.stdout)

    def test_no_head_commit_fails(self) -> None:
        result = Gate(self).run(SHA="not-a-commit")
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::No head commit to check", result.stdout)
        self.assertEqual(result.calls, [])

    def test_a_missing_recorded_answer_fails_loudly(self) -> None:
        gate = Gate(self)
        (gate.answers / "commits__SHA__statuses.json").unlink()
        result = gate.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no recorded answer for repos/example/app/commits/", result.stderr)


class PollingTest(unittest.TestCase):
    def test_codeql_only_pending_polls_until_it_arrives(self) -> None:
        gate = Gate(self)
        (gate.answers / "actions__runs.json").unlink()
        gate.record_series("actions__runs", [
            {"workflow_runs": [run("Security scans")]},
            {"workflow_runs": [run("Security scans"), codeql_run(status="in_progress", conclusion=None)]},
            {"workflow_runs": [run("Security scans"), codeql_run()]},
        ])
        result = gate.run(POLL_ATTEMPTS="5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.reads_of("actions/runs"), 3)
        self.assertEqual(result.posted, [
            ("pending", "Waiting for: CodeQL (not started)"),
            ("pending", "Waiting for: CodeQL"),
            ("success", "Every check passed"),
        ])

    def test_polling_stops_after_poll_attempts_and_posts_each_state_once(self) -> None:
        gate = Gate(self)
        gate.runs([run("Security scans")])
        result = gate.run(POLL_ATTEMPTS="2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.reads_of("actions/runs"), 3)
        self.assertEqual(result.posted, [("pending", "Waiting for: CodeQL (not started)")])

    def test_anything_else_pending_does_not_poll(self) -> None:
        gate = Gate(self)
        gate.runs([run("Security scans")])
        gate.statuses([status("Agent review")])
        result = gate.run()
        self.assertEqual(result.reads_of("actions/runs"), 1)
        self.assertEqual(result.posted, [("pending", "Waiting for: CodeQL (not started), Second review (not started)")])


NOT_RISKY = "Not a risky pull request"


def described(context: str, state: str, description: str) -> Json:
    return {**status(context, state), "description": description}


class DefaultPolicyTest(unittest.TestCase):
    def test_the_defaults_need_only_the_second_review_and_no_agent_review(self) -> None:
        gate = Gate(self)
        gate.statuses([described("Second review", "success", NOT_RISKY)])
        result = gate.run(REQUIRED_STATUSES=None, BLOCKING_THREAD_BADGES=None, BLOCKING_THREAD_AUTHORS=None)
        self.assertEqual(result.posted, [("success", "Every check passed")])
        self.assertIn("graphql", [call[2] for call in result.calls if call[1] == "api"])

    def test_the_defaults_fail_on_a_blocking_second_review(self) -> None:
        gate = Gate(self)
        gate.statuses([described("Second review", "failure", "1 blocking finding(s); see the comment")])
        result = gate.run(REQUIRED_STATUSES=None, BLOCKING_THREAD_BADGES=None, BLOCKING_THREAD_AUTHORS=None)
        self.assertEqual(result.posted, [("failure", "Failed: Second review (failure)")])

    def test_the_defaults_fail_on_any_open_codex_p1_or_p2_thread(self) -> None:
        for badge in (P1, P2):
            gate = Gate(self)
            gate.statuses([described("Second review", "success", NOT_RISKY)])
            gate.threads([thread(badge), thread(P1, resolved=True), thread(P3)])
            result = gate.run(REQUIRED_STATUSES=None, BLOCKING_THREAD_BADGES=None, BLOCKING_THREAD_AUTHORS=None)
            self.assertEqual(result.posted, [("failure", "Failed: 1 open P1|P2 review comment")])

    def test_the_defaults_fail_on_both_at_once(self) -> None:
        gate = Gate(self)
        gate.statuses([described("Second review", "failure", "2 blocking finding(s); see the comment")])
        gate.threads([thread(P2)])
        result = gate.run(REQUIRED_STATUSES=None, BLOCKING_THREAD_BADGES=None, BLOCKING_THREAD_AUTHORS=None)
        self.assertEqual(result.posted, [("failure", "Failed: Second review (failure), 1 open P1|P2 review comment")])

    def test_a_failing_agent_review_still_fails_where_an_app_turned_it_on(self) -> None:
        gate = Gate(self)
        gate.statuses([status("Agent review", "failure"), status("Second review")])
        self.assertEqual(gate.run(REQUIRED_STATUSES=None).posted, [("failure", "Failed: Agent review (failure)")])

    def test_the_reusable_workflow_and_the_script_share_the_default(self) -> None:
        workflow = self_check.parse((cli.ROOT / ".github" / "workflows" / "merge-gate.yml").read_text(encoding="utf-8"))
        assert isinstance(workflow, dict)
        trigger = workflow["on"]
        assert isinstance(trigger, dict)
        call = trigger["workflow_call"]
        assert isinstance(call, dict)
        inputs = call["inputs"]
        assert isinstance(inputs, dict)
        text = (cli.ROOT / ".github" / "workflows" / "merge-gate.yml").read_text(encoding="utf-8")
        self.assertIn("        default: |\n          Second review\n      ignored-apps:", text)
        codex = inputs["require-codex-review"]
        assert isinstance(codex, dict)
        self.assertEqual((codex["type"], codex["default"]), ("boolean", "false"))
        self.assertIn("required_statuses_text=${REQUIRED_STATUSES-Second review}", SCRIPT.read_text(encoding="utf-8"))


CODEX = "chatgpt-codex-connector[bot]"


def review(commit_id: str = SHA, author: str = CODEX) -> Json:
    return {"user": {"login": author}, "commit_id": commit_id, "state": "COMMENTED", "body": "### Codex Review"}


def comment(body: str, author: str = CODEX) -> Json:
    return {"user": {"login": author}, "body": body}


def summary(commit_sha: str = SHA, state: str = "completed") -> str:
    record = {"code_review": {"commit_sha": commit_sha, "status": state}, "security_review": {"commit_sha": None, "status": None}}
    return f"<!-- codex-pull-request-review-summary -->\n<!-- codex-review-state:v1 {json.dumps(record)} -->\n\n## Codex Review Summary\n"


def receipt(prefix: str = SHA[:10]) -> str:
    return f"<!-- codex-review-receipt:v1 -->\n\nCodex Review: Didn't find any major issues.\n\n**Reviewed commit:** `{prefix}`\n"


class CodexReviewTest(unittest.TestCase):
    def gate(self, reviews: list[Json], comments: list[Json]) -> Gate:
        gate = Gate(self)
        gate.reviews(reviews)
        gate.comments(comments)
        return gate

    def test_off_by_default_reads_nothing_of_codex(self) -> None:
        result = Gate(self).run()
        self.assertEqual(result.posted, [("success", "Every check passed")])
        self.assertEqual(result.reads_of("/reviews"), 0)
        self.assertEqual(result.reads_of("/comments"), 0)

    def test_a_head_without_a_codex_review_waits(self) -> None:
        gate = self.gate([], [comment("@codex review", author="octocat")])
        self.assertEqual(gate.run(REQUIRE_CODEX_REVIEW="true").posted, [("pending", "Waiting for: Codex review of this commit")])

    def test_a_review_of_an_older_commit_does_not_count(self) -> None:
        older = "b" * 40
        gate = self.gate([review(older)], [comment(summary(older)), comment(receipt(older[:10]))])
        self.assertEqual(gate.run(REQUIRE_CODEX_REVIEW="true").posted, [("pending", "Waiting for: Codex review of this commit")])

    def test_only_codexs_own_reviews_and_comments_count(self) -> None:
        gate = self.gate([review(author="octocat")], [comment(summary(), author="octocat"), comment(receipt(), author="hubot")])
        self.assertEqual(gate.run(REQUIRE_CODEX_REVIEW="true").posted, [("pending", "Waiting for: Codex review of this commit")])

    def test_a_running_review_or_a_short_prefix_does_not_count(self) -> None:
        gate = self.gate([], [comment(summary(state="in_progress")), comment(receipt(SHA[:6]))])
        self.assertEqual(gate.run(REQUIRE_CODEX_REVIEW="true").posted, [("pending", "Waiting for: Codex review of this commit")])

    def test_a_review_a_receipt_or_a_summary_of_the_head_passes(self) -> None:
        for reviews, comments in (([review()], []), ([], [comment(receipt())]), ([], [comment(summary())])):
            gate = self.gate(reviews, comments)
            self.assertEqual(gate.run(REQUIRE_CODEX_REVIEW="true").posted, [("success", "Every check passed")])

    def test_a_summary_that_is_not_json_is_ignored(self) -> None:
        gate = self.gate([], [comment("<!-- codex-review-state:v1 {not json -->")])
        self.assertEqual(gate.run(REQUIRE_CODEX_REVIEW="true").posted, [("pending", "Waiting for: Codex review of this commit")])

    def test_reviews_that_cannot_be_read_leave_it_pending(self) -> None:
        gate = Gate(self)
        gate.comments([comment(receipt())])
        self.assertEqual(gate.run(REQUIRE_CODEX_REVIEW="true").posted, [("pending", "Waiting for: Codex review (couldn't be read)")])

    def test_an_open_p1_still_fails_a_reviewed_head(self) -> None:
        gate = self.gate([review()], [])
        gate.threads([thread(P1)])
        self.assertEqual(gate.run(REQUIRE_CODEX_REVIEW="true").posted, [("failure", "Failed: 1 open P1|P2 review comment")])

    def test_dependabot_is_exempt(self) -> None:
        gate = Gate(self)
        gate.dependabot()
        result = gate.run(REQUIRE_CODEX_REVIEW="true")
        self.assertEqual(result.posted, [("success", "Every check passed")])
        self.assertTrue(result.merged)


class DependabotTest(unittest.TestCase):
    def test_a_green_dependabot_pull_request_merges(self) -> None:
        gate = Gate(self)
        gate.dependabot()
        result = gate.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.posted, [("success", "Every check passed")])
        self.assertTrue(result.merged)
        self.assertIn(["gh", "pr", "merge", str(NUMBER), "--repo", REPO, "--squash", "--match-head-commit", SHA], result.calls)
        self.assertIn(["gh", "workflow", "run", "docker.yml", "--repo", REPO, "--ref", "main"], result.calls)
        self.assertIn(["gh", "workflow", "run", "security.yml", "--repo", REPO, "--ref", "main"], result.calls)

    def test_a_dry_run_prints_the_merge_instead(self) -> None:
        gate = Gate(self)
        gate.dependabot()
        result = gate.run(DRY_RUN="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(result.merged)
        self.assertEqual(result.posted, [])
        self.assertIn(f"(dry run) gh api -X POST repos/{REPO}/statuses/{SHA} -f state=success", result.stdout)
        self.assertIn(f"(dry run) gh pr merge {NUMBER} --repo {REPO} --squash --match-head-commit {SHA}", result.stdout)
        self.assertIn(f"(dry run) gh workflow run docker.yml --repo {REPO} --ref main", result.stdout)
        self.assertIn(f"(dry run) gh workflow run security.yml --repo {REPO} --ref main", result.stdout)
        self.assertIn("(dry run) gh pr merge", gate.run(DRY_RUN="true").stdout)

    def test_a_dependabot_pull_request_bumping_a_fleet_reference_passes_but_is_not_merged(self) -> None:
        gate = Gate(self)
        gate.dependabot([
            changed_file("poetry.lock", '-version = "1.0"\n+version = "1.1"'),
            changed_file(
                ".github/workflows/merge-gate.yml",
                "     merge:\n"
                "-    uses: AnthonyPluth/fleet/.github/workflows/merge-gate.yml@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa   # v1.0.0\n"
                "+    uses: AnthonyPluth/fleet/.github/workflows/merge-gate.yml@bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb   # v1.1.0\n",
            ),
        ])
        result = gate.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.posted, [("success", "Every check passed")])
        self.assertFalse(result.merged)
        self.assertIn(
            f"::notice::#{NUMBER} changes a AnthonyPluth/fleet/ reference (.github/workflows/merge-gate.yml); leaving it for the owner.",
            result.stdout,
        )

    def test_a_dependabot_pull_request_bumping_a_fleet_reference_merges_when_fleet_bumps_are_on(self) -> None:
        gate = Gate(self)
        gate.dependabot([
            changed_file(
                ".github/workflows/merge-gate.yml",
                "-    uses: AnthonyPluth/fleet/.github/workflows/merge-gate.yml@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa   # v1.0.0\n"
                "+    uses: AnthonyPluth/fleet/.github/workflows/merge-gate.yml@bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb   # v1.1.0\n",
            ),
        ])
        result = gate.run(AUTOMERGE_FLEET_BUMPS="true", AFTER_MERGE_WORKFLOWS="docker.yml fleet-sync.yml")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.posted, [("success", "Every check passed")])
        self.assertTrue(result.merged)
        self.assertIn(["gh", "pr", "merge", str(NUMBER), "--repo", REPO, "--squash", "--match-head-commit", SHA], result.calls)
        self.assertIn(["gh", "workflow", "run", "fleet-sync.yml", "--repo", REPO, "--ref", "main"], result.calls)
        self.assertNotIn("leaving it for the owner", result.stdout)
        self.assertFalse(gate.run(AUTOMERGE_FLEET_BUMPS="false").merged)
        self.assertFalse(gate.run(AUTOMERGE_FLEET_BUMPS="true", AUTOMERGE_DEPENDABOT="false").merged)

    def test_a_fleet_reference_only_in_unchanged_context_lines_still_merges(self) -> None:
        gate = Gate(self)
        gate.dependabot([
            changed_file(
                ".github/workflows/ci.yml",
                "     uses: AnthonyPluth/fleet/.github/workflows/checks.yml@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa   # v1.0.0\n"
                "-    uses: actions/checkout@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa   # v7.0.1\n"
                "+    uses: actions/checkout@bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb   # v7.0.2\n",
            ),
        ])
        self.assertTrue(gate.run().merged)

    def test_dependabot_needs_neither_codeql_nor_the_reviews(self) -> None:
        gate = Gate(self)
        gate.dependabot()
        gate.checks([])
        self.assertEqual(gate.run().posted, [("success", "Every check passed")])

    def test_dependabot_still_needs_the_other_required_workflows(self) -> None:
        gate = Gate(self)
        gate.dependabot()
        gate.runs([])
        result = gate.run()
        self.assertEqual(result.posted, [("pending", "Waiting for: Security scans (not started)")])
        self.assertFalse(result.merged)

    def test_a_commit_from_someone_else_is_left_for_review(self) -> None:
        gate = Gate(self)
        gate.dependabot()
        gate.commits([commit(), commit(author="octocat")])
        result = gate.run()
        self.assertFalse(result.merged)
        self.assertIn(f"::notice::#{NUMBER} has commits from someone other than Dependabot", result.stdout)
        gate.commits([commit(verified=False)])
        self.assertFalse(gate.run().merged)

    def test_a_fork_or_a_draft_is_not_merged(self) -> None:
        gate = Gate(self)
        gate.dependabot()
        gate.pulls([pull(author=DEPENDABOT, head_repo="someone/app")])
        result = gate.run()
        self.assertEqual(result.posted, [("success", "Every check passed")])
        self.assertFalse(result.merged)
        gate.pulls([pull(author=DEPENDABOT, draft=True)])
        self.assertFalse(gate.run().merged)

    def test_automerge_can_be_turned_off(self) -> None:
        gate = Gate(self)
        gate.dependabot()
        result = gate.run(AUTOMERGE_DEPENDABOT="false")
        self.assertEqual(result.posted, [("success", "Every check passed")])
        self.assertFalse(result.merged)
        self.assertIn(f"Not merging #{NUMBER}: AUTOMERGE_DEPENDABOT is off.", result.stdout)

    def test_after_merge_workflows_are_configurable(self) -> None:
        gate = Gate(self)
        gate.dependabot()
        result = gate.run(AFTER_MERGE_WORKFLOWS="release.yml")
        self.assertTrue(result.merged)
        workflow_runs = [call for call in result.calls if call[1:3] == ["workflow", "run"]]
        self.assertEqual(workflow_runs, [["gh", "workflow", "run", "release.yml", "--repo", REPO, "--ref", "main"]])
        gate = Gate(self)
        gate.dependabot()
        result = gate.run(AFTER_MERGE_WORKFLOWS="")
        self.assertTrue(result.merged)
        self.assertEqual([call for call in result.calls if call[1:3] == ["workflow", "run"]], [])


FORK = "someone/app"


def deleted_fork_pull() -> Json:
    return {**pull(), "head": {"sha": SHA, "repo": None}}


class ForkTest(unittest.TestCase):
    def fork(self, head_repo: str = FORK) -> Gate:
        gate = Gate(self)
        gate.pulls([pull(head_repo=head_repo)])
        gate.runs([run("Security scans")])
        gate.checks([])
        return gate

    def test_a_fork_needs_no_codeql_and_does_not_poll_for_it(self) -> None:
        result = self.fork().run(POLL_ATTEMPTS="5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.posted, [("success", "Every check passed")])
        self.assertEqual(result.reads_of("actions/runs"), 1)
        self.assertIn(f"From a fork ({FORK}): CodeQL isn't required.", result.stdout)

    def test_every_trigger_decides_the_same(self) -> None:
        gate = self.fork()
        gate.record("pulls__N", {"head": {"sha": SHA}})
        by_sha = gate.run(REQUIRED_WORKFLOWS=None)
        by_number = gate.run(REQUIRED_WORKFLOWS=None, SHA=None, PR=str(NUMBER))
        self.assertEqual(by_sha.posted, [("success", "Every check passed")])
        self.assertEqual(by_number.posted, [("success", "Every check passed")])

    def test_only_codeql_is_dropped_and_duplicates_are_still_tolerated(self) -> None:
        gate = self.fork()
        gate.runs([])
        result = gate.run(REQUIRED_WORKFLOWS="Docker image\nSecurity scans\nCodeQL\nCodeQL\n")
        self.assertEqual(result.posted, [("pending", "Waiting for: Docker image (not started), Security scans (not started)")])
        result = gate.run(REQUIRED_WORKFLOWS="Docker image\nSecurity scans\nSecurity scans\n")
        self.assertEqual(result.posted, [(
            "pending", "Waiting for: Docker image (not started), Security scans (not started), Security scans (not started)",
        )])
        gate.runs([run("Docker image"), run("Security scans")])
        self.assertEqual(gate.run(REQUIRED_WORKFLOWS="Docker image\nSecurity scans\nSecurity scans\n").posted, [("success", "Every check passed")])

    def test_a_fork_still_needs_the_statuses_the_reviews_and_no_blocking_thread(self) -> None:
        gate = self.fork()
        gate.statuses([])
        self.assertEqual(gate.run(REQUIRED_STATUSES=None).posted, [("pending", "Waiting for: Second review (not started)")])
        gate.statuses([status("Second review", "failure")])
        self.assertEqual(gate.run().posted, [("failure", "Failed: Second review (failure)")])
        gate.statuses([status("Second review")])
        gate.threads([thread(P1)])
        self.assertEqual(gate.run().posted, [("failure", "Failed: 1 open P1|P2 review comment")])
        gate.threads([])
        gate.reviews([])
        gate.comments([])
        self.assertEqual(gate.run(REQUIRE_CODEX_REVIEW="true").posted, [("pending", "Waiting for: Codex review of this commit")])

    def test_a_codeql_run_that_does_come_still_counts(self) -> None:
        gate = self.fork()
        gate.runs([run("Security scans"), codeql_run(conclusion="failure")])
        self.assertEqual(gate.run().posted, [("failure", "Failed: CodeQL (failure)")])
        gate.runs([run("Security scans"), codeql_run(status="in_progress", conclusion=None)])
        self.assertEqual(gate.run().posted, [("pending", "Waiting for: CodeQL")])

    def test_a_deleted_fork_counts_as_a_fork(self) -> None:
        gate = self.fork()
        gate.pulls([deleted_fork_pull()])
        result = gate.run()
        self.assertEqual(result.posted, [("success", "Every check passed")])
        self.assertIn("From a fork (a deleted repository): CodeQL isn't required.", result.stdout)

    def test_a_pull_request_from_this_repository_still_needs_codeql(self) -> None:
        for head_repo in (REPO, REPO.upper()):
            result = self.fork(head_repo=head_repo).run()
            self.assertEqual(result.posted, [("pending", "Waiting for: CodeQL (not started)")])
            self.assertNotIn("From a fork", result.stdout)

    def test_a_fork_named_like_this_repository_with_a_suffix_is_a_fork(self) -> None:
        result = self.fork(head_repo=f"{REPO}-fork").run()
        self.assertEqual(result.posted, [("success", "Every check passed")])


WAYPOINT = {
    "REQUIRED_WORKFLOWS": "Docker image\nSecurity scans\nCodeQL\n",
    "IGNORED_WORKFLOWS": "",
    "REQUIRED_STATUSES": "",
    "IGNORED_APPS": "codecov|github-actions",
}
RUNWAY = {
    "REQUIRED_WORKFLOWS": "Docker image\nSecurity scans\nCodeQL\n",
    "IGNORED_WORKFLOWS": "Preview|Screenshot diff",
    "REQUIRED_STATUSES": "Agent review\n",
    "IGNORED_APPS": "codecov|github-actions",
}


class ConfigurationTest(unittest.TestCase):
    def test_waypoints_configuration(self) -> None:
        gate = Gate(self)
        gate.runs([
            run("Docker image"), run("Security scans"), run("Docs"), run("Control smoke"), codeql_run(),
            run("Cancel runs on close", conclusion="failure"), run("Merge gate", status="in_progress", conclusion=None),
        ])
        gate.statuses([])
        self.assertEqual(gate.run(**WAYPOINT).posted, [("success", "Every check passed")])
        gate.runs([run("Security scans"), codeql_run()])
        self.assertEqual(gate.run(**WAYPOINT).posted, [("pending", "Waiting for: Docker image (not started)")])
        gate.runs([run("Docker image"), run("Security scans"), codeql_run(), run("Preview", status="in_progress", conclusion=None)])
        self.assertEqual(gate.run(**WAYPOINT).posted, [("pending", "Waiting for: Preview")])

    def test_runways_configuration(self) -> None:
        gate = Gate(self)
        gate.runs([
            run("Docker image"), run("Security scans"), run("Docs"), run("Verify flows"), codeql_run(),
            run("Preview", status="in_progress", conclusion=None), run("Screenshot diff", conclusion="failure"),
            run("Cancel runs on close", conclusion="failure"), run("Merge gate", status="in_progress", conclusion=None),
        ])
        gate.statuses([status("Agent review")])
        self.assertEqual(gate.run(**RUNWAY).posted, [("success", "Every check passed")])
        gate.statuses([])
        self.assertEqual(gate.run(**RUNWAY).posted, [("pending", "Waiting for: Agent review (not started)")])
        gate.statuses([status("Agent review", "failure")])
        self.assertEqual(gate.run(**RUNWAY).posted, [("failure", "Failed: Agent review (failure)")])

    def test_unset_configuration_takes_fleets_defaults(self) -> None:
        gate = Gate(self)
        gate.runs([run("Docker image")])
        gate.statuses([])
        result = gate.run(REQUIRED_WORKFLOWS=None, IGNORED_WORKFLOWS=None, REQUIRED_STATUSES=None, IGNORED_APPS=None)
        self.assertEqual(result.posted, [("pending", "Waiting for: Security scans (not started), CodeQL (not started), Second review (not started)")])

    def test_an_app_can_still_require_the_agent_review(self) -> None:
        gate = Gate(self)
        statuses = "Agent review\nSecond review\n"
        self.assertEqual(gate.run(REQUIRED_STATUSES=statuses).posted, [("pending", "Waiting for: Agent review (not started)")])
        gate.statuses([status("Agent review"), status("Second review")])
        self.assertEqual(gate.run(REQUIRED_STATUSES=statuses).posted, [("success", "Every check passed")])

    def test_empty_configuration_requires_nothing(self) -> None:
        gate = Gate(self)
        gate.runs([])
        gate.statuses([])
        gate.checks([])
        self.assertEqual(gate.run(REQUIRED_WORKFLOWS="", REQUIRED_STATUSES="").posted, [("success", "Every check passed")])

    def test_ignored_apps_is_configurable(self) -> None:
        gate = Gate(self)
        gate.checks([check("codecov", "codecov/patch", conclusion="failure"), check("semgrep", "Semgrep", conclusion="failure")])
        self.assertEqual(gate.run(IGNORED_APPS="codecov|semgrep").posted, [("success", "Every check passed")])
        self.assertEqual(gate.run(IGNORED_APPS="").posted, [("failure", "Failed: codecov/patch (failure), Semgrep (failure)")])

    def test_lists_tolerate_blank_lines_and_spaces(self) -> None:
        gate = Gate(self)
        gate.statuses([status("Agent review"), status("Second review")])
        result = gate.run(REQUIRED_WORKFLOWS="\n  Security scans  \n\nCodeQL\n", REQUIRED_STATUSES=" Agent review \n Second review \n")
        self.assertEqual(result.posted, [("success", "Every check passed")])


class WorkflowTest(unittest.TestCase):
    def test_the_self_test_calls_the_gate_in_dry_run(self) -> None:
        workflow = self_check.parse((cli.ROOT / ".github" / "workflows" / "self-test.yml").read_text(encoding="utf-8"))
        assert isinstance(workflow, dict)
        jobs = workflow["jobs"]
        assert isinstance(jobs, dict)
        calls = [job for job in jobs.values() if isinstance(job, dict) and job.get("uses") == "./.github/workflows/merge-gate.yml"]
        self.assertEqual(len(calls), 1)
        inputs = calls[0]["with"]
        assert isinstance(inputs, dict)
        self.assertEqual(inputs["dry-run"], "true")
        self.assertEqual(inputs["fleet-ref"], "${{ github.sha }}")

    def test_the_reusable_workflow_declares_every_input_the_script_reads(self) -> None:
        workflow = self_check.parse((cli.ROOT / ".github" / "workflows" / "merge-gate.yml").read_text(encoding="utf-8"))
        assert isinstance(workflow, dict)
        trigger = workflow["on"]
        assert isinstance(trigger, dict)
        call = trigger["workflow_call"]
        assert isinstance(call, dict)
        inputs = call["inputs"]
        assert isinstance(inputs, dict)
        self.assertEqual(sorted(inputs), sorted([
            "sha", "pr", "required-workflows", "ignored-workflows", "required-statuses", "ignored-apps", "poll-seconds",
            "poll-attempts", "blocking-thread-badges", "blocking-thread-authors", "require-codex-review", "automerge-dependabot",
            "automerge-fleet-bumps", "after-merge-workflows", "dry-run", "fleet-ref",
        ]))
        jobs = workflow["jobs"]
        assert isinstance(jobs, dict)
        gate = jobs["gate"]
        assert isinstance(gate, dict)
        self.assertEqual(gate["permissions"], {
            "contents": "write", "pull-requests": "write", "statuses": "write", "actions": "write", "checks": "read",
        })
        steps = gate["steps"]
        assert isinstance(steps, list)
        decide = steps[-1]
        assert isinstance(decide, dict)
        self.assertEqual(decide["run"], ".fleet/.github/scripts/merge-gate.sh")
        env = decide["env"]
        assert isinstance(env, dict)
        for variable in [
            "REQUIRED_WORKFLOWS", "IGNORED_WORKFLOWS", "REQUIRED_STATUSES", "IGNORED_APPS", "POLL_SECONDS", "POLL_ATTEMPTS",
            "BLOCKING_THREAD_BADGES", "BLOCKING_THREAD_AUTHORS", "REQUIRE_CODEX_REVIEW", "AUTOMERGE_DEPENDABOT", "AUTOMERGE_FLEET_BUMPS", "AFTER_MERGE_WORKFLOWS", "DRY_RUN", "SHA", "PR", "REPO", "GH_TOKEN", "RUN_URL",
        ]:
            self.assertIn(variable, env)

    def test_the_threads_workflow_only_starts_the_callers_gate(self) -> None:
        workflow = self_check.parse((cli.ROOT / ".github" / "workflows" / "merge-gate-threads.yml").read_text(encoding="utf-8"))
        assert isinstance(workflow, dict)
        trigger = workflow["on"]
        assert isinstance(trigger, dict)
        call = trigger["workflow_call"]
        assert isinstance(call, dict)
        inputs = call["inputs"]
        assert isinstance(inputs, dict)
        self.assertEqual(sorted(inputs), ["dry-run", "fleet-ref", "merge-gate-workflow", "pr"])
        self.assertEqual(workflow["permissions"], "{}")
        jobs = workflow["jobs"]
        assert isinstance(jobs, dict)
        decide = jobs["decide"]
        assert isinstance(decide, dict)
        self.assertEqual(decide["permissions"], {"actions": "write"})
        steps = decide["steps"]
        assert isinstance(steps, list)
        self.assertEqual(len(steps), 1)
        step = steps[0]
        assert isinstance(step, dict)
        self.assertNotIn("uses", step)
        text = (cli.ROOT / ".github" / "workflows" / "merge-gate-threads.yml").read_text(encoding="utf-8")
        self.assertIn('gh workflow run "$WORKFLOW" --repo "$REPO" --ref main -f pr="$PR"', text)
        self.assertNotIn("actions/checkout", text)
        self.assertRegex(text, r"runs-on: ubuntu-latest[ \t]+# hosted: \S")


if __name__ == "__main__":
    unittest.main()
