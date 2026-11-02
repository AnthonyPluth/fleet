import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from test_review_mine import dismissal, finding, outcome, record, review_comment

from fleet_tools import agent_review as ar
from fleet_tools import cli, loop_health, review_outcomes
from fleet_tools import loop_stats as ls
from fleet_tools import review_mine as rm

ROOT = Path(__file__).resolve().parent.parent
FAKES = ROOT / "tests" / "fakes" / "security"
RUNWAY = ROOT / "tests" / "fixtures" / "consumer-runway"
REPO = "example/app"
NOW = "2026-10-01T00:00:00Z"
SINCE = "2026-09-01T00:00:00Z"
PULLS = f"GET repos/{REPO}/pulls?state=all&sort=updated&direction=desc&per_page=100&page=1"
OPEN = f"GET repos/{REPO}/issues?state=open&labels=review-mining&per_page=100"
MINED = f"GET repos/{REPO}/issues?state=all&labels=review-mining&per_page=100"
SYNC = f"GET repos/{REPO}/pulls?state=open&head=example:fleet/sync&per_page=100"
A, B, C, D, E, F = (letter * 40 for letter in "abcdef")
START_AGENTS = ("# Agent guide\n\n## Mistakes that keep coming back\n\n- **Layout** (human-judged: a judgement). Text.\n"
                "- **Failure paths** (checked: tests/test_failing.py). Text.\n- **Undo and restore** (partly checked: x). Text.\n")
END_AGENTS = ("# Agent guide\n\n## Mistakes that keep coming back\n\n- **Layout** (checked: layout-widths). Text.\n"
              "- **Failure paths** (checked: tests/test_failing.py). Text.\n- **Undo and restore** (partly checked: x). Text.\n"
              "- **Brand new** (checked: new-rule). Text.\n")


def history(number: int) -> str:
    return f"GET repos/{REPO}/issues/{number}/comments?per_page=100&page=1"


def files_of(number: int) -> str:
    return f"GET repos/{REPO}/pulls/{number}/files?per_page=100"


def commits(path: str) -> str:
    return f"GET repos/{REPO}/commits?sha=main&path={path}&since={SINCE}&until={NOW}&per_page=100"


def newest(until: str, path: str = "") -> str:
    where = f"&path={path}" if path else ""
    return f"GET repos/{REPO}/commits?sha=main{where}&until={until}&per_page=1"


def contents(path: str, ref: str) -> str:
    return f"GET repos/{REPO}/contents/{path}?ref={ref}"


def detail(sha: str, page: int = 1) -> str:
    return f"GET repos/{REPO}/commits/{sha}?per_page=100&page={page}"


def blob(text: str | bytes) -> dict[str, Any]:
    data = text.encode() if isinstance(text, str) else text
    return {"type": "file", "encoding": "base64", "size": len(data), "content": base64.encodebytes(data).decode()}


def dated(sha: str, when: str) -> dict[str, Any]:
    return {"sha": sha, "commit": {"committer": {"date": when}}}


def merged(number: int, when: str, body: str = "", updated: str | None = None) -> dict[str, Any]:
    return {"number": number, "updated_at": updated or when, "merged_at": when, "body": body}


def reviewed(number: int, findings: list[dict[str, Any]], entries: list[dict[str, Any]] | None = None,
             review: ar.Review = ar.FIRST, when: str = "2026-09-05T00:00:00Z") -> dict[str, Any]:
    lines = ar.render(findings, "summary", "run", "a" * 40, review).splitlines()
    if entries is not None:
        lines = [*lines[:3], review_outcomes.encode(entries, review), *lines[3:]]
    return review_comment(number, findings, review, when, body="\n".join(lines))


def report_body() -> str:
    return (f"{rm.MARKER}\nintro\n\n### Repeated findings\n\n- [x] <!-- row:repeated:layout --> **Layout** becomes an Agent task → #81\n"
            "- [ ] <!-- row:repeated:failure-paths --> **Failure paths** becomes an Agent task\n"
            "- [ ] <!-- row:gaps:screens --> **Screens and flows** becomes an Agent task\n")


def fixture() -> dict[str, Any]:
    pulls = [
        merged(1, "2026-09-10T12:00:00Z", record(("Shows rows", "verified", "ran it", "tests/test_rows.py"), ("Bank sync", "not verified", "no fake", "-"))),
        merged(2, "2026-09-12T12:00:00Z", record(("A", "not verified", "x", "-"), ("B", "not verified", "y", "-"))),
        merged(3, "2026-09-14T12:00:00Z"),
        merged(4, "2026-08-01T00:00:00Z", updated="2026-09-02T00:00:00Z"),
        {"number": 5, "updated_at": "2026-09-20T00:00:00Z", "merged_at": None, "body": record(("Z", "not verified", "z", "-"))},
    ]
    first = [finding("Layout", "Overflows"), finding("Failure paths", "Drops rows", severity="blocking")]
    entries = [outcome("Layout", "Overflows"), outcome("Failure paths", "Drops rows", "resolved"), outcome("Undo and restore", "Loses order", "resolved")]
    late = {**dismissal(1, "Layout", "Overflows", "the page is desktop-only by design"),
            "created_at": "2026-09-20T00:00:00Z", "updated_at": "2026-09-20T00:00:00Z"}
    data: dict[str, Any] = {
        PULLS: pulls,
        history(1): [reviewed(1, first, entries), late],
        history(2): [reviewed(2, []), reviewed(2, [finding("Failure paths", "Leaks", severity="blocking"), finding("Layout", "Cramped")],
                                               review=ar.SECOND, when="2026-09-11T00:00:00Z")],
        history(3): [],
        files_of(1): [{"filename": "runway/storage/migrations/0002_add.py"}, {"filename": "README.md"}],
        files_of(2): [{"filename": "frontend/verify/flows/sync.mjs"}, {"filename": "README.md"}, {"filename": "scripts/x.sh", "previous_filename": "scripts/old.sh"}],
        files_of(3): [{"filename": "runway/static/app.css"}],
        OPEN: [{"number": 60, "title": rm.TITLE, "body": report_body()}],
        MINED: [
            {"number": 81, "user": {"login": rm.BOT}, "body": "x\n<!-- mined-from row:repeated:layout -->\n", "state": "closed",
             "created_at": "2026-08-01T00:00:00Z", "closed_at": "2026-09-05T00:00:00Z"},
            {"number": 82, "user": {"login": rm.BOT}, "body": "<!-- mined-from row:gaps:screens -->", "state": "open",
             "created_at": "2026-09-03T00:00:00Z", "closed_at": None},
            {"number": 83, "user": {"login": "someone"}, "body": "<!-- mined-from row:repeated:x -->", "state": "closed",
             "created_at": "2026-09-03T00:00:00Z", "closed_at": "2026-09-04T00:00:00Z"},
            {"number": 84, "user": {"login": rm.BOT}, "body": "<!-- mined-from row:repeated:old -->", "state": "closed",
             "created_at": "2026-05-01T00:00:00Z", "closed_at": "2026-06-01T00:00:00Z"},
            {"number": 60, "user": {"login": rm.BOT}, "body": report_body(), "state": "open", "created_at": "2026-06-01T00:00:00Z"},
            {"number": 85, "user": {"login": rm.BOT}, "body": "<!-- mined-from row:repeated:y -->", "pull_request": {}},
        ],
        f"GET repos/{REPO}": {"default_branch": "main"},
        commits(ls.CONTEXT): [{"sha": A}],
        commits(ls.INVARIANTS): [],
        commits(ls.SEMGREP): [{"sha": B}],
        commits(ls.SKILLS): [{"sha": C}, {"sha": D}],
        commits("docs/verification"): [{"sha": A}],
        detail(C): {"files": [{"filename": ".claude/skills/release-notes/SKILL.md"}]},
        detail(D): {"files": [{"filename": ".claude/skills/fleet-verify-change/SKILL.md"}, {"filename": "AGENTS.md"}]},
        newest(SINCE): [dated(E, "2026-08-30T00:00:00Z")],
        newest(NOW): [dated(F, "2026-09-30T00:00:00Z")],
        contents("AGENTS.md", E): blob(START_AGENTS),
        contents("AGENTS.md", F): blob(END_AGENTS),
        newest(NOW, "AGENTS.md"): [dated(F, "2026-09-21T00:00:00Z")],
        newest(NOW, ls.CONTEXT): [dated(A, "2026-09-30T12:00:00Z")],
        newest(NOW, ls.INVARIANTS): [],
        newest(NOW, "docs/verification/migrations.md"): [dated(A, "2026-06-03T00:00:00Z")],
        newest(NOW, "docs/verification/screens.md"): [dated(A, "2026-09-01T00:00:00Z")],
        contents(".fleet/version", "main"): blob("v1.0.0\n"),
        SYNC: [{"number": 90, "state": "open"}],
    }
    return data


class Stats:
    def __init__(self, test: unittest.TestCase) -> None:
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.answers = self.directory / "answers.json"
        self.log = self.directory / "log.jsonl"
        self.json = self.directory / "loop-stats.json"
        self.markdown = self.directory / "loop-stats.md"

    def run(self, data: dict[str, Any], *flags: str) -> subprocess.CompletedProcess[str]:
        self.answers.write_text(json.dumps(data), encoding="utf-8")
        env = {**os.environ, "PATH": f"{FAKES}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_ANSWERS": str(self.answers),
               "FAKE_GH_LOG": str(self.log), "GH_TOKEN": "fake"}
        command = [sys.executable, "-E", "-s", "-m", "fleet_tools.cli", "loop-stats", "--repo", REPO, "--now", NOW, "--days", "30",
                   "--json", str(self.json), "--markdown", str(self.markdown), *flags]
        return subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)

    def stats(self) -> dict[str, Any]:
        value = json.loads(self.json.read_text(encoding="utf-8"))
        assert isinstance(value, dict)
        return value

    def writes(self) -> list[str]:
        calls = [json.loads(line)["call"] for line in self.log.read_text(encoding="utf-8").splitlines() if line] if self.log.exists() else []
        return [call for call in calls if not call.startswith("GET ")]


class FixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stats = Stats(self)
        result = self.stats.run(fixture(), "--app-root", str(RUNWAY))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.result = result
        self.value = self.stats.stats()

    def test_the_envelope(self) -> None:
        self.assertEqual(self.value["schema"], 1)
        self.assertEqual(self.value["repo"], REPO)
        self.assertEqual((self.value["since"], self.value["generated"], self.value["days"]), (SINCE, NOW, 30))
        self.assertEqual(self.value["default_branch"], "main")
        self.assertEqual(self.value["missing"], [])
        self.assertEqual(self.stats.writes(), [])

    def test_reviews_count_the_merged_pull_requests_findings_and_outcomes(self) -> None:
        self.assertEqual(self.value["reviews"], {
            "merged": 3, "unread": 0, "with_agent_review": 2, "with_second_review": 1,
            "findings": {"raised": 5, "by_severity": {"blocking": 2, "advisory": 2, "unknown": 1}, "resolved": 2, "dismissed": 1,
                         "open": 2, "resolved_share": 0.4, "dismissed_share": 0.2, "at_merge": 5, "open_at_merge": 3,
                         "open_at_merge_share": 0.6, "indeterminate_at_merge": 0}})

    def test_verification_counts_records_dimensions_and_uncovered_paths(self) -> None:
        self.assertEqual(self.value["verification"], {
            "configured": True, "guides": 2, "merged": 3, "with_record": 2, "without_record": 1, "unparsed": 0, "verified": 1,
            "not_verified": 3, "mean_not_verified": 1.5, "uncovered_paths": 3, "pulls_with_uncovered_paths": 2, "unread_files": 0,
            "uncovered_samples": ["README.md", "scripts/old.sh", "scripts/x.sh"]})

    def test_the_report_counts_rows_ticks_and_the_issues_opened_from_them(self) -> None:
        self.assertEqual(self.value["report"], {"issue": 60, "rows": 3, "ticked": 1, "tasks_opened": 3, "tasks_opened_in_window": 1,
                                                "tasks_closed_in_window": 1})

    def test_harness_changes_on_the_default_branch(self) -> None:
        self.assertEqual(self.value["harness"], {
            "commits": {"context": 1, "invariants": 0, "semgrep": 1, "skills": 1, "verification_guides": 1, "total": 3},
            "checked_in_window": 1, "checked_items": ["Layout"],
            "days_since_change": {"AGENTS.md": 10, ls.CONTEXT: 0, ls.INVARIANTS: None, "docs/verification/migrations.md": 120,
                                  "docs/verification/screens.md": 30}})

    def test_fleet_version_and_the_sync_pull_request(self) -> None:
        self.assertEqual(self.value["fleet"], {"version": "v1.0.0", "sync_open": True})

    def test_the_markdown_has_each_group(self) -> None:
        text = self.stats.markdown.read_text(encoding="utf-8")
        for heading in ("## Loop health of `example/app`", "### Reviews", "### Verification", "### The report", "### Harness changes on `main`", "### fleet"):
            self.assertIn(heading, text)
        self.assertIn("| Resolved share | 40% |", text)
        self.assertIn("| `not verified` per record (mean) | 1.50 |", text)
        self.assertIn("| `.github/agent-review/invariants.md` | n/a (never, or not read) |", text)
        self.assertIn("Newly checked: **Layout**", text)
        self.assertNotIn("### Not read", text)

    def test_the_headline_round_trips_through_the_hidden_line(self) -> None:
        summary = loop_health.headline(self.value)
        assert summary is not None
        self.assertEqual(summary["values"]["merged"], 3)
        self.assertEqual(summary["values"]["resolved_share"], 0.4)
        self.assertEqual(summary["values"]["checked"], 1)
        self.assertEqual(loop_health.decode(loop_health.encode(summary)), summary)


class EdgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stats = Stats(self)

    def test_zero_denominators_are_null_in_json_and_n_a_in_markdown(self) -> None:
        data = fixture()
        data[PULLS] = [merged(4, "2026-08-01T00:00:00Z", updated="2026-09-02T00:00:00Z")]
        result = self.stats.run(data, "--app-root", str(RUNWAY))
        self.assertEqual(result.returncode, 0, result.stderr)
        value = self.stats.stats()
        findings = value["reviews"]["findings"]
        self.assertEqual((value["reviews"]["merged"], findings["raised"], findings["at_merge"]), (0, 0, 0))
        self.assertIsNone(findings["resolved_share"])
        self.assertIsNone(findings["dismissed_share"])
        self.assertIsNone(findings["open_at_merge_share"])
        self.assertIsNone(value["verification"]["mean_not_verified"])
        text = self.stats.markdown.read_text(encoding="utf-8")
        self.assertIn("| Resolved share | n/a |", text)
        self.assertIn("| Open at merge share | n/a |", text)
        self.assertIn("| `not verified` per record (mean) | n/a |", text)
        summary = loop_health.headline(value)
        assert summary is not None
        self.assertIsNone(summary["values"]["resolved_share"])
        self.assertIn("| … resolved | n/a |", "\n".join(loop_health.section(summary, None)))

    def test_without_the_setting_verification_is_not_configured(self) -> None:
        result = self.stats.run(fixture())
        self.assertEqual(result.returncode, 0, result.stderr)
        value = self.stats.stats()
        self.assertEqual(value["verification"], {"configured": False})
        self.assertIsNone(value["harness"]["commits"]["verification_guides"])
        self.assertEqual(value["harness"]["commits"]["total"], 3)
        self.assertEqual(sorted(value["harness"]["days_since_change"]), sorted(["AGENTS.md", ls.CONTEXT, ls.INVARIANTS]))
        self.assertIn("Not configured", self.stats.markdown.read_text(encoding="utf-8"))
        self.assertIsNone(loop_health.headline(value)["values"]["records"])  # type: ignore[index]

    def test_an_item_already_checked_or_new_and_checked_isn_t_counted(self) -> None:
        self.assertEqual(ls.newly_checked(START_AGENTS, END_AGENTS), ["Layout"])
        self.assertEqual(ls.newly_checked(END_AGENTS, END_AGENTS), [])
        moved_and_back = START_AGENTS.replace("(human-judged: a judgement)", "(checked: x)")
        self.assertEqual(ls.newly_checked(moved_and_back, END_AGENTS), [])
        self.assertEqual(ls.newly_checked("", END_AGENTS), [])

    def test_a_failing_api_call_is_reported_missing_never_zero(self) -> None:
        data = fixture()
        del data[f"GET repos/{REPO}"]
        del data[PULLS]
        result = self.stats.run(data)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = self.stats.stats()
        self.assertIsNone(value["reviews"])
        self.assertIsNone(value["verification"])
        self.assertIsNone(value["harness"])
        self.assertIsNone(value["fleet"])
        self.assertEqual(value["report"]["rows"], 3)
        self.assertEqual(len(value["missing"]), 2)
        self.assertTrue(value["missing"][0].startswith("reviews and verification: the merged pull requests: gh api failed (1)"))
        self.assertTrue(value["missing"][1].startswith("harness and fleet: the default branch: gh api failed (1)"))
        self.assertIn("loop-stats: not read: reviews and verification", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        text = self.stats.markdown.read_text(encoding="utf-8")
        self.assertIn("### Not read", text)
        self.assertEqual(text.count("Not read."), 4)
        summary = loop_health.headline(value)
        assert summary is not None
        self.assertIsNone(summary["values"]["merged"])
        self.assertEqual(summary["values"]["rows"], 3)

    def test_each_harness_part_fails_alone(self) -> None:
        data = fixture()
        del data[commits(ls.SEMGREP)]
        data[contents("AGENTS.md", F)] = blob(b"\xff\xfe not utf-8")
        data[newest(NOW, ls.CONTEXT)] = {"__status__": 502}
        data[contents(".fleet/version", "main")] = blob("main\n")
        del data[SYNC]
        result = self.stats.run(data, "--app-root", str(RUNWAY))
        self.assertEqual(result.returncode, 0, result.stderr)
        value = self.stats.stats()
        harness = value["harness"]
        self.assertIsNone(harness["commits"]["semgrep"])
        self.assertIsNone(harness["commits"]["total"])
        self.assertEqual(harness["commits"]["context"], 1)
        self.assertIsNone(harness["checked_in_window"])
        self.assertIsNone(harness["days_since_change"][ls.CONTEXT])
        self.assertEqual(harness["days_since_change"]["AGENTS.md"], 10)
        self.assertEqual(value["fleet"], {"version": None, "sync_open": None})
        missing = "\n".join(value["missing"])
        self.assertIn("commits touching .semgrep", missing)
        self.assertIn("AGENTS.md at the window's first and last commits: AGENTS.md at ffffffffffff isn't UTF-8", missing)
        self.assertIn(f"when {ls.CONTEXT} last changed", missing)
        self.assertIn(".fleet/version isn't a vX.Y.Z version", missing)
        self.assertIn("fleet/sync pull request is open", missing)
        self.assertEqual(value["reviews"]["merged"], 3)

    def test_a_missing_fleet_version_or_agents_md_is_none_not_an_error(self) -> None:
        data = fixture()
        del data[contents(".fleet/version", "main")]
        del data[contents("AGENTS.md", E)]
        result = self.stats.run(data)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = self.stats.stats()
        self.assertEqual(value["fleet"]["version"], None)
        self.assertEqual(value["harness"]["checked_in_window"], 0)
        self.assertEqual(value["missing"], [])

    def test_no_commit_before_the_window_leaves_the_checked_count_unknown(self) -> None:
        data = fixture()
        data[newest(SINCE)] = []
        self.stats.run(data)
        value = self.stats.stats()
        self.assertIsNone(value["harness"]["checked_in_window"])
        self.assertEqual(value["missing"], ["harness: no commit on main before the window to compare AGENTS.md with"])

    def test_over_the_skill_commit_cap_the_skills_count_is_unknown(self) -> None:
        data = fixture()
        data[commits(ls.SKILLS)] = [{"sha": f"{i:040x}"} for i in range(ls.MAX_SKILL_COMMITS + 1)]
        self.stats.run(data)
        value = self.stats.stats()
        self.assertIsNone(value["harness"]["commits"]["skills"])
        self.assertIn(f"over {ls.MAX_SKILL_COMMITS} commits touched .claude/skills/", "\n".join(value["missing"]))
        data[commits(ls.SKILLS)] = [{"sha": f"{i:040x}"} for i in range(ls.MAX_SKILL_COMMITS)]
        for i in range(ls.MAX_SKILL_COMMITS):
            data[detail(f"{i:040x}")] = {"files": [{"filename": ".claude/skills/a/SKILL.md"}]}
        self.stats.run(data)
        self.assertEqual(self.stats.stats()["harness"]["commits"]["skills"], ls.MAX_SKILL_COMMITS)

    def test_a_commit_with_over_a_page_of_files_is_read_page_by_page(self) -> None:
        data = fixture()
        data[detail(D)] = {"files": [{"filename": f"src/{i}.py"} for i in range(100)]}
        data[detail(D, 2)] = {"files": [{"filename": ".claude/skills/own/SKILL.md"}]}
        self.stats.run(data)
        self.assertEqual(self.stats.stats()["harness"]["commits"]["skills"], 2)

    def test_over_the_pull_request_page_cap_reviews_are_unknown(self) -> None:
        data = fixture()
        for page in range(1, ls.MAX_PAGES + 1):
            data[f"GET repos/{REPO}/pulls?state=all&sort=updated&direction=desc&per_page=100&page={page}"] = [
                {"number": page * 1000 + i, "updated_at": "2026-09-20T00:00:00Z", "merged_at": None} for i in range(100)]
        self.stats.run(data)
        value = self.stats.stats()
        self.assertIsNone(value["reviews"])
        self.assertIn(f"over {ls.MAX_PAGES * 100} pull requests were updated in the window", "\n".join(value["missing"]))

    def test_unreadable_comments_and_files_are_counted_and_said(self) -> None:
        data = fixture()
        del data[history(3)]
        del data[files_of(2)]
        self.stats.run(data, "--app-root", str(RUNWAY))
        value = self.stats.stats()
        self.assertEqual(value["reviews"]["unread"], 1)
        self.assertEqual(value["verification"]["unread_files"], 1)
        missing = "\n".join(value["missing"])
        self.assertIn("the comments of 1 merged pull request(s) couldn't be read in full", missing)
        self.assertIn("the changed files of 1 merged pull request(s) couldn't be listed", missing)

    def test_uncovered_samples_are_capped_and_counted_in_full(self) -> None:
        data = fixture()
        data[files_of(3)] = [{"filename": f"lib/{i:02}.py"} for i in range(ls.MAX_SAMPLES + 5)]
        self.stats.run(data, "--app-root", str(RUNWAY))
        value = self.stats.stats()["verification"]
        self.assertEqual(value["uncovered_paths"], 3 + ls.MAX_SAMPLES + 5)
        self.assertEqual(len(value["uncovered_samples"]), ls.MAX_SAMPLES)

    def test_an_agents_md_over_the_content_cap_is_not_read(self) -> None:
        data = fixture()
        data[contents("AGENTS.md", F)] = {**blob(END_AGENTS), "size": ls.MAX_CONTENT + 1}
        self.stats.run(data)
        value = self.stats.stats()
        self.assertIsNone(value["harness"]["checked_in_window"])
        self.assertIn(f"AGENTS.md at ffffffffffff is over {ls.MAX_CONTENT:,} bytes", "\n".join(value["missing"]))

    def test_paths_from_pull_requests_are_defused_in_markdown(self) -> None:
        data = fixture()
        data[files_of(3)] = [{"filename": "@octocat <!-- x --> <b>|`.md"}]
        self.stats.run(data, "--app-root", str(RUNWAY))
        text = self.stats.markdown.read_text(encoding="utf-8")
        self.assertNotIn("@octocat", text)
        self.assertNotIn("<!--", text)
        self.assertNotIn("<b>", text)
        self.assertIn("@​octocat &lt;!-- x --> &lt;b>\\|'.md", text)

    def test_usage_errors(self) -> None:
        self.assertEqual(cli.main(["loop-stats", "--repo", REPO, "--days", "0", "--json", "a", "--markdown", "b"]), 2)
        self.assertEqual(cli.main(["loop-stats", "--repo", "not a repo", "--json", "a", "--markdown", "b"]), 2)
        self.assertEqual(cli.main(["loop-stats", "--repo", REPO, "--now", "yesterday", "--json", "a", "--markdown", "b"]), 2)

    def test_an_unwritable_output_fails(self) -> None:
        result = self.stats.run(fixture(), "--json", str(self.stats.directory / "no" / "such" / "out.json"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("loop-stats: can't write the output", result.stderr)


class HiddenLineTest(unittest.TestCase):
    def summary(self) -> dict[str, Any]:
        return {"schema": 1, "date": "2026-10-01", "days": 30, "values": {"merged": 3, "resolved_share": 0.4, "mean_not_verified": 1.5, "rows": None}}

    def test_a_summary_round_trips(self) -> None:
        line = loop_health.encode(self.summary())
        self.assertTrue(line.startswith("<!-- loop-stats ") and line.endswith(" -->"))
        self.assertEqual(loop_health.decode(line), self.summary())
        self.assertEqual(loop_health.previous(f"{rm.MARKER}\n{line}\nrest", rm.MARKER), self.summary())

    def test_only_the_second_line_of_the_report_counts(self) -> None:
        line = loop_health.encode(self.summary())
        self.assertIsNone(loop_health.previous(f"{rm.MARKER}\nintro\n{line}", rm.MARKER))
        self.assertIsNone(loop_health.previous(f"other\n{line}", rm.MARKER))
        self.assertIsNone(loop_health.previous(rm.MARKER, rm.MARKER))

    def test_last_run_is_compared_only_over_the_same_window(self) -> None:
        current = self.summary()
        same = "\n".join(loop_health.section(current, {**current, "date": "2026-09-01"}))
        self.assertIn("| Measure | Last run (2026-09-01) | This run (2026-10-01) |", same)
        other = "\n".join(loop_health.section(current, {**current, "date": "2026-09-01", "days": 7}))
        self.assertIn("| Measure | This run (2026-10-01) |", other)
        self.assertNotIn("Last run (", other)
        self.assertIn("covered 7 days, not 30, so it isn't compared", other)
        alone = "\n".join(loop_health.section(None, {**current, "days": 7}, "missing"))
        self.assertIn("| Measure | Last run (2026-10-01) |", alone)
        self.assertIn("over the last 7 days", alone)

    def test_a_malformed_line_is_ignored(self) -> None:
        def line(value: object) -> str:
            return f"<!-- loop-stats {base64.b64encode(json.dumps(value).encode()).decode()} -->"

        good = self.summary()
        self.assertIsNotNone(loop_health.decode(line(good)))
        bad: list[object] = [
            [], {**good, "schema": 2}, {**good, "schema": True}, {**good, "extra": 1}, {k: v for k, v in good.items() if k != "days"},
            {**good, "date": "2026-02-30"}, {**good, "date": "yesterday"}, {**good, "days": 0}, {**good, "days": "30"},
            {**good, "values": {"merged": -1}}, {**good, "values": {"merged": 1.5}}, {**good, "values": {"merged": True}},
            {**good, "values": {"resolved_share": 1.5}}, {**good, "values": {"unknown_key": 1}}, {**good, "values": {"merged": "3"}},
            {**good, "values": {"merged": 10**12}}, {**good, "values": []},
        ]
        for value in bad:
            self.assertIsNone(loop_health.decode(line(value)), value)
        self.assertIsNone(loop_health.decode('<!-- loop-stats {"schema": 1} -->'))
        self.assertIsNone(loop_health.decode("<!-- loop-stats not*base64 -->"))
        self.assertIsNone(loop_health.decode("<!-- loop-stats " + base64.b64encode(b"\xff\xfe").decode() + " -->"))
        self.assertIsNone(loop_health.decode("<!-- loop-stats " + base64.b64encode(b'{"values": NaN}').decode() + " -->"))
        self.assertIsNone(loop_health.decode(line({**good, "values": {"mean_not_verified": float("inf")}}).replace("Infinity", "1e999")))
        self.assertIsNone(loop_health.decode(" " + line(good)))
        self.assertIsNone(loop_health.decode(line(good) + "\n"))

    def test_an_oversized_line_is_ignored(self) -> None:
        padded = f"<!-- loop-stats {'A' * (loop_health.MAX_LINE)} -->"
        self.assertGreater(len(padded), loop_health.MAX_LINE)
        self.assertIsNone(loop_health.decode(padded))
        big = {**self.summary(), "date": "2026-10-01", "values": {m.key: 10**9 if m.kind == loop_health.COUNT else 1 for m in loop_health.HEADLINE}}
        line = loop_health.encode(big)
        self.assertLessEqual(len(line), loop_health.MAX_LINE)
        self.assertEqual(loop_health.decode(line), big)

    def test_a_stats_file_that_isn_t_schema_1_isn_t_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "s.json"
            for text, why in (("{", "UTF-8 JSON"), ('{"schema": 2}', "schema 1"), ('{"schema": 1, "days": 30}', "headline")):
                path.write_text(text, encoding="utf-8")
                summary, reason = loop_health.read(path)
                self.assertIsNone(summary)
                self.assertIn(why, reason)
            path.write_bytes(b"\xff")
            self.assertIn("UTF-8", loop_health.read(path)[1])
            path.write_text(" " * (loop_health.MAX_STATS + 1), encoding="utf-8")
            self.assertIn("over", loop_health.read(path)[1])
            self.assertIn("can't be read", loop_health.read(Path(directory) / "missing.json")[1])


if __name__ == "__main__":
    unittest.main()
