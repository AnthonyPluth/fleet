import base64
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from fleet_tools import agent_review as ar
from fleet_tools import cli
from fleet_tools import review_outcomes as ro

ROOT = cli.ROOT
SELF_TEST = ROOT / ".github" / "scripts" / "finding-outcomes-self-test.sh"
Json = dict[str, Any]
A, B, C = "a" * 40, "b" * 40, "c" * 40
URL = "https://github.com/example/app/pull/1#issuecomment-7"


def finding(title: str = "Drops rows", file: str = "app/a.py", kind: str = "Failure paths", severity: str = "blocking") -> Json:
    return {"severity": severity, "kind": kind, "title": title, "detail": "d", "file": file}


def entry(title: str = "Drops rows", file: str = "app/a.py", kind: str = "Failure paths", status: str = "open",
          first: str = A, **more: Any) -> Json:
    return {"id": ar.finding_id(kind, file, title), "status": status, "kind": kind, "file": file, "title": title, "first": first, **more}


def bot(body: str) -> Json:
    return {"user": ar.BOT, "association": "NONE", "url": "", "body": body}


def dismiss(ident: str, reason: str = "the finding is wrong because", association: str = "OWNER", url: str = URL, more: str = "") -> Json:
    return {"user": "owner", "association": association, "url": url, "body": f"/fleet dismiss f:{ident} {reason}{more}"}


def comment(findings: list[Json], sha: str = A, review: ar.Review = ar.FIRST) -> str:
    return ar.render(findings, "s", "https://example.com/run", sha, review)


class Merge(unittest.TestCase):
    def test_a_new_finding_is_open_at_this_commit(self) -> None:
        merged = ro.merge([], ar.with_ids([finding()]), A, full=True)
        self.assertEqual(merged, [entry()])

    def test_open_becomes_resolved_on_a_full_review_that_no_longer_reports_it(self) -> None:
        merged = ro.merge([entry()], [], B, full=True)
        self.assertEqual(merged, [entry(status="resolved", resolved=B)])

    def test_still_open_after_an_incremental_review_that_doesn_t_touch_the_file(self) -> None:
        merged = ro.merge([entry()], [], B, full=False, touched=["app/b.py", "README.md"])
        self.assertEqual(merged, [entry()])
        merged = ro.merge([entry(file="")], [], B, full=False, touched=["app/a.py"])
        self.assertEqual(merged, [entry(file="")])

    def test_resolved_after_an_incremental_review_whose_diff_touched_the_file(self) -> None:
        merged = ro.merge([entry()], [], B, full=False, touched=["app/a.py"])
        self.assertEqual(merged, [entry(status="resolved", resolved=B)])

    def test_a_finding_still_reported_stays_open_with_its_first_commit(self) -> None:
        merged = ro.merge([entry()], ar.with_ids([finding()]), B, full=True)
        self.assertEqual(merged, [entry()])

    def test_resolved_becomes_open_when_the_finding_comes_back(self) -> None:
        merged = ro.merge([entry(status="resolved", resolved=B)], ar.with_ids([finding(title="drops ROWS")]), C, full=True)
        self.assertEqual(merged, [entry(title="drops ROWS")])
        self.assertNotIn("resolved", merged[0])

    def test_a_resolved_entry_stays_resolved_and_a_finding_without_an_id_is_ignored(self) -> None:
        merged = ro.merge([entry(status="resolved", resolved=B)], [finding()], C, full=True)
        self.assertEqual(merged, [entry(status="resolved", resolved=B)])

    def test_the_record_round_trips_through_line_four(self) -> None:
        entries = [entry(), entry(title="Nit", file="", kind="other", status="resolved", first=A, resolved=B)]
        line = ro.outcomes_line(entries)
        self.assertRegex(line, r"^<!-- agent-review-outcomes [A-Za-z0-9+/=]+ -->$")
        self.assertEqual(ro.decode_record(line.split(" ")[2]), entries)
        second = ro.outcomes_line(entries, ar.SECOND)
        self.assertTrue(second.startswith("<!-- second-review-outcomes "))
        body = "\n".join([ar.MARKER, f"<!-- agent-review-state sha={A} verdict=pass -->", ar.findings_line([]), line, "## x"])
        self.assertEqual(ro.record_of(body), entries)
        self.assertIsNone(ro.record_of(body, ar.SECOND))
        self.assertIsNone(ro.record_of("\n".join(body.splitlines()[:3])))

    def test_malformed_entries_are_dropped_on_reading(self) -> None:
        raw = [entry(), {"id": "nope", "status": "open", "first": A}, {"id": "1a2b3c4d", "status": "weird", "first": A},
               {"id": "1a2b3c4d", "status": "open", "first": "short"}, "text", {"id": "1a2b3c4d", "status": "resolved", "first": A, "resolved": "x"}]
        decoded = ro.decode_record(base64.b64encode(json.dumps(raw).encode()).decode())
        assert decoded is not None
        self.assertEqual(len(decoded), 2)
        self.assertEqual(decoded[0], entry())
        self.assertEqual(decoded[1]["status"], "resolved")
        self.assertNotIn("resolved", decoded[1])
        self.assertIsNone(ro.decode_record("not base64!"))
        self.assertIsNone(ro.decode_record(base64.b64encode(b'{"a": 1}').decode()))


class Caps(unittest.TestCase):
    def test_the_size_cap_drops_the_oldest_resolved_entries_first(self) -> None:
        entries = [entry(title=f"finding {i} " + "x" * 150, file=f"app/{i}.py", status="resolved" if i % 2 else "open", resolved=B)
                   for i in range(60)]
        kept = ro.cap(entries)
        self.assertLessEqual(len(ro.outcomes_line(kept).encode()), ro.MAX_OUTCOMES_LINE)
        self.assertLess(len(kept), len(entries))
        dropped = [e for e in entries if e not in kept]
        self.assertTrue(all(e["status"] == "resolved" for e in dropped))
        self.assertEqual(dropped, [e for e in entries if e["status"] == "resolved"][:len(dropped)])
        self.assertEqual([e for e in kept if e["status"] == "open"], [e for e in entries if e["status"] == "open"])

    def test_only_open_entries_are_dropped_when_no_resolved_one_is_left(self) -> None:
        entries = [entry(title=f"finding {i} " + "x" * 150, file=f"app/{i}.py") for i in range(120)]
        kept = ro.cap(entries)
        self.assertLess(len(kept), 120)
        self.assertEqual(kept, entries[len(entries) - len(kept):])

    def test_at_most_two_hundred_entries(self) -> None:
        entries = [entry(title=f"f{i}", file="", kind="k", status="resolved" if i < 10 else "open", first=A) for i in range(230)]
        for e in entries:
            e.pop("resolved", None)
        with mock.patch.object(ro, "MAX_OUTCOMES_LINE", 10**6):
            kept = ro.cap(entries)
        self.assertEqual(len(kept), ro.MAX_ENTRIES)
        self.assertEqual(kept[0]["title"], "f30")
        self.assertTrue(all(e["status"] == "open" for e in kept))
        self.assertEqual(kept[-1]["title"], "f229")

    def test_the_comment_with_all_four_lines_stays_under_github_s_limit(self) -> None:
        findings = [finding(title=f"finding {i} " + "y" * 300, file="f" * 400 + f"{i}.py", kind="k" * 300) for i in range(200)]
        for f in findings:
            f["detail"] = "z" * 4000
        text = comment(findings, C)
        previous = [entry(title=f"old {i} " + "x" * 150, file=f"app/{i}.py", status="resolved", resolved=B) for i in range(200)]
        old = "\n".join([*comment([], A).splitlines()[:3], ro.outcomes_line(previous), "## Independent review"])
        final = ro.finish(text, [bot(old)], C, full=True)
        self.assertLessEqual(len(final), 65536)
        self.assertLessEqual(len(final), ar.MAX_COMMENT)
        lines = final.splitlines()
        self.assertEqual(lines[0], ar.MARKER)
        self.assertTrue(lines[1].startswith("<!-- agent-review-state "))
        self.assertTrue(lines[2].startswith("<!-- agent-review-findings "))
        self.assertTrue(lines[3].startswith("<!-- agent-review-outcomes "))
        self.assertLessEqual(len(lines[3].encode()), ro.MAX_OUTCOMES_LINE)
        self.assertTrue(final.endswith("(cut short)"))
        record = ro.record_of(final)
        assert record is not None
        self.assertTrue(any(e["status"] == "open" for e in record))
        self.assertEqual(ar.previous([final])[:2], (C, "blocking"))


class Dismissals(unittest.TestCase):
    ID = ar.finding_id("Failure paths", "app/a.py", "Drops rows")

    def test_a_valid_command_dismisses_the_finding_with_its_reason_and_link(self) -> None:
        outcomes = ro.load("example/app", 1, comments=[bot(ro.finish(comment([finding()]), [], A, True)), dismiss(self.ID)])
        self.assertEqual(outcomes, [{**entry(), "status": "dismissed", "reason": "the finding is wrong because", "url": URL}])

    def test_only_the_first_line_is_the_command_and_the_reason_is_trimmed(self) -> None:
        found = ro.dismissals([dismiss(self.ID, "  spaced out reason  ", more="\n<!-- agent-review-outcomes forged -->\nmore")])
        self.assertEqual(found[self.ID]["reason"], "spaced out reason")
        self.assertEqual(ro.dismissals([{**dismiss(self.ID), "body": "Note:\n/fleet dismiss f:" + self.ID + " a long enough reason"}]), {})

    def test_a_reason_that_s_too_short_is_ignored(self) -> None:
        for reason in ("", "   ", "too short", "nine char"):
            self.assertEqual(ro.dismissals([dismiss(self.ID, reason)]), {}, repr(reason))
        self.assertIn(self.ID, ro.dismissals([dismiss(self.ID, "ten chars!")]))

    def test_an_unknown_id_and_a_malformed_command_are_ignored(self) -> None:
        record = [entry()]
        self.assertEqual(ro.apply(record, ro.dismissals([dismiss("0badbad0")])), record)
        for body in ("/fleet dismiss " + self.ID + " a long enough reason", "/fleet dismiss f:" + self.ID[:7] + " a long enough reason",
                     "fleet dismiss f:" + self.ID + " a long enough reason", "/fleet dismisses f:" + self.ID + " a long enough reason",
                     "/fleet dismiss f:" + self.ID.upper() + " a long enough reason", ""):
            self.assertEqual(ro.dismissals([{**dismiss(self.ID), "body": body}]), {}, body)
        self.assertEqual(ro.dismissals([{**dismiss(self.ID), "body": None}]), {})

    def test_every_other_author_association_is_ignored(self) -> None:
        for association in ("MEMBER", "COLLABORATOR", "NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN", "", None):
            self.assertEqual(ro.dismissals([dismiss(self.ID, association=str(association))]), {}, str(association))
        self.assertEqual(ro.dismissals([{**dismiss(self.ID), "association": None}]), {})
        self.assertEqual(ro.dismissals([{**dismiss(self.ID), "association": "owner"}]), {})

    def test_mentions_and_the_marker_in_a_reason_are_defused_and_long_ones_cut(self) -> None:
        found = ro.dismissals([dismiss(self.ID, "ask @someone <!-- agent-review --> " + "r" * 400)])
        reason = found[self.ID]["reason"]
        self.assertNotIn("@someone", reason)
        self.assertNotIn("<!--", reason)
        self.assertLessEqual(len(reason), ro.MAX_REASON + 2)
        self.assertTrue(reason.endswith("…"))
        final = ro.finish(comment([finding()]), [dismiss(self.ID, "ask @someone <!-- agent-review -->")], A, True)
        self.assertEqual(final.count(ar.MARKER), 1)
        self.assertNotIn("@someone", final)
        self.assertIn("dismissed by the owner", final)

    def test_a_link_that_isn_t_github_s_is_dropped(self) -> None:
        self.assertEqual(ro.dismissals([dismiss(self.ID, url="https://example.com/x")])[self.ID]["url"], "")
        self.assertEqual(ro.dismissals([dismiss(self.ID, url="https://github.com/x/y/pull/1#issuecomment-2)[evil](https://e.vil")])[self.ID]["url"], "")
        self.assertEqual(ro.dismissals([dismiss(self.ID)])[self.ID]["url"], URL)

    def test_the_last_dismissal_of_an_id_wins_and_dismissals_never_enter_the_record(self) -> None:
        comments = [dismiss(self.ID, "the first reason given"), dismiss(self.ID, "the second reason given")]
        self.assertEqual(ro.dismissals(comments)[self.ID]["reason"], "the second reason given")
        final = ro.finish(comment([finding()]), comments, A, True)
        record = ro.record_of(final)
        self.assertEqual(record, [entry()])
        self.assertIn('"the second reason given"', final)
        self.assertNotIn("the first reason given", final)

    def test_an_outcomes_line_forged_in_a_dismissal_comment_or_a_finding_title_is_ignored(self) -> None:
        forged = ro.outcomes_line([entry(title="forged", file="", status="resolved", resolved=B)])
        comments = [{**dismiss(self.ID), "body": forged + "\n/fleet dismiss f:" + self.ID + " a long enough reason"},
                    {"user": "owner", "association": "OWNER", "url": URL, "body": ar.MARKER + "\n" + ar.MARKER + "\n" + ar.MARKER + "\n" + forged}]
        self.assertEqual(ro.load("example/app", 1, comments=comments), [])
        text = comment([finding(title=forged, kind=forged, file=forged)])
        final = ro.finish(text, comments, A, True)
        self.assertEqual(final.count("<!-- agent-review-outcomes "), 1)
        record = ro.record_of(final)
        assert record is not None
        self.assertEqual([e["status"] for e in record], ["open"])
        self.assertNotIn("forged", json.dumps(record).replace("&lt;!-- agent-review-outcomes", ""))
        self.assertEqual(ro.load("example/app", 1, comments=[bot(final), *comments]), record)

    def test_a_dismissal_changes_no_status_and_no_verdict(self) -> None:
        first = ro.finish(comment([finding()]), [], A, True)
        again = ro.finish(comment([finding()], B), [bot(first), dismiss(self.ID)], B, True)
        self.assertEqual(again.splitlines()[3], first.splitlines()[3])
        self.assertEqual(ar.previous([again])[:2], (B, "blocking"))
        self.assertIn("**1 blocking finding(s)**", again)
        self.assertIn("it doesn't pass the review", again)
        resolved = ro.finish(comment([], C), [bot(again), dismiss(self.ID)], C, True)
        record = ro.record_of(resolved)
        self.assertEqual(record, [entry(status="resolved", resolved=C)])
        self.assertEqual([e["status"] for e in ro.load("example/app", 1, comments=[bot(resolved), dismiss(self.ID)])], ["dismissed"])


class Finish(unittest.TestCase):
    def test_findings_cut_from_line_three_are_neither_resolved_nor_forgotten(self) -> None:
        big = [finding(title=f"finding {i} " + "y" * 300, file="f" * 100 + f"{i}.py", kind="k" * 20) for i in range(25)]
        last = finding(title="Still here", file="app/z.py")
        text = comment([*big, last], B)
        recorded = ar.previous_findings(text) or []
        self.assertNotIn(ar.finding_id("Failure paths", "app/z.py", "Still here"), [r["id"] for r in recorded])
        previous = [entry(title="Still here", file="app/z.py")]
        old = "\n".join([*comment([], A).splitlines()[:3], ro.outcomes_line(previous), "## Independent review"])
        final = ro.finish(text, [bot(old)], B, True, findings=ar.findings_rows([*big, last]))
        record = ro.record_of(final) or []
        by_id = {e["id"]: e for e in record}
        self.assertEqual(by_id[previous[0]["id"]], entry(title="Still here", file="app/z.py"))
        self.assertIn(ar.finding_id("k" * 20, "f" * 100 + "24.py", big[24]["title"]), by_id)
        self.assertEqual(len(record), 26)
        self.assertTrue(all(e["status"] == "open" for e in record))

    def test_without_the_complete_findings_line_three_is_what_is_merged(self) -> None:
        text = comment([finding()], B)
        self.assertEqual(ro.record_of(ro.finish(text, [], B, True)), [entry(first=B)])
        self.assertEqual(ro.record_of(ro.finish(text, [], B, True, findings=[])), [])

    def test_a_first_review_gets_line_four_and_no_outcomes_section(self) -> None:
        final = ro.finish(comment([finding()]), [], A, True)
        lines = final.splitlines()
        self.assertTrue(lines[3].startswith("<!-- agent-review-outcomes "))
        self.assertEqual(lines[4], "## Independent review")
        self.assertNotIn(ar.OUTCOMES_HEADING, final)
        self.assertEqual(ro.record_of(final), [entry()])

    def test_a_resolved_finding_is_shown_before_the_footer(self) -> None:
        first = ro.finish(comment([finding()]), [], A, True)
        final = ro.finish(comment([], B), [bot(first)], B, True)
        lines = final.splitlines()
        self.assertIn(ar.OUTCOMES_HEADING, lines)
        self.assertIn(f"- `f:{Dismissals.ID}` **Drops rows**: resolved at `{B[:7]}`.", lines)
        self.assertLess(lines.index(ar.OUTCOMES_HEADING), next(i for i, line in enumerate(lines) if line.startswith(ar.FOOTER)))
        self.assertNotIn("dismiss", final)
        self.assertEqual(ar.previous([final])[:2], (B, "pass"))
        self.assertNotIn(ar.OUTCOMES_HEADING, ar.previous([final])[2])

    def test_an_incremental_review_passes_the_touched_files_through(self) -> None:
        first = ro.finish(comment([finding(), finding(title="Nit", file="app/b.py", kind="other", severity="advisory")]), [], A, True)
        final = ro.finish(comment([], B), [bot(first)], B, False, ["app/b.py"])
        record = ro.record_of(final)
        assert record is not None
        self.assertEqual([(e["file"], e["status"]) for e in record], [("app/a.py", "open"), ("app/b.py", "resolved")])

    def test_the_second_review_keeps_its_own_record(self) -> None:
        first = ro.finish(comment([finding()]), [], A, True)
        second = ro.finish(comment([finding(title="Other")], A, ar.SECOND), [bot(first)], A, True, review=ar.SECOND)
        self.assertTrue(second.splitlines()[3].startswith("<!-- second-review-outcomes "))
        self.assertEqual([e["title"] for e in ro.record_of(second, ar.SECOND) or []], ["Other"])
        self.assertIsNone(ro.record_of(second))
        later = ro.finish(comment([], B, ar.SECOND), [bot(first), bot(second)], B, True, review=ar.SECOND)
        self.assertEqual([e["status"] for e in ro.record_of(later, ar.SECOND) or []], ["resolved"])
        self.assertEqual(ro.record_of(ro.finish(comment([], B), [bot(second), bot(first)], B, True)), [entry(status="resolved", resolved=B)])

    def test_a_comment_it_can_t_place_the_line_in_is_left_alone(self) -> None:
        for text in ("Thanks!", ar.MARKER + "\n## Independent review\n", comment([finding()], ""), comment([finding()], A, ar.SECOND)):
            self.assertEqual(ro.finish(text, [], A, True), text, text[:40])
        self.assertEqual(ro.finish(comment([finding()]), [], "not-a-sha", True), comment([finding()]))

    def test_only_the_bot_s_own_comment_with_the_marker_is_the_previous_record(self) -> None:
        first = ro.finish(comment([finding()]), [], A, True)
        impostor = {"user": "someone", "association": "OWNER", "url": URL, "body": first}
        final = ro.finish(comment([], B), [impostor], B, True)
        self.assertEqual(ro.record_of(final), [])
        final = ro.finish(comment([], B), [bot("Thanks!"), impostor, bot(first)], B, True)
        self.assertEqual(ro.record_of(final), [entry(status="resolved", resolved=B)])


class Command(unittest.TestCase):
    def run_command(self, *args: str, comments: list[Json]) -> tuple[int, str, str]:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
            f.write("".join(json.dumps(c) + "\n" for c in comments))
        self.addCleanup(os.unlink, f.name)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["review-outcomes", "--repo", "example/app", "--pr", "1", "--comments", f.name, *args])
        return code, out.getvalue(), err.getvalue()

    def test_it_prints_one_line_per_finding_or_json(self) -> None:
        first = ro.finish(comment([finding(), finding(title="Nit", file="", kind="other", severity="advisory")]), [], A, True)
        comments = [bot(first), dismiss(Dismissals.ID)]
        code, out, _ = self.run_command(comments=comments)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], f"f:{Dismissals.ID}  dismissed  Failure paths  app/a.py  Drops rows  (the finding is wrong because)")
        self.assertRegex(out.splitlines()[1], r"^f:[0-9a-f]{8}  open       other  -  Nit$")
        code, out, _ = self.run_command("--json", comments=comments)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), ro.load("example/app", 1, comments=comments))
        code, out, _ = self.run_command(comments=[])
        self.assertEqual((code, out), (0, "No findings recorded.\n"))

    def test_it_fetches_the_comments_with_gh_when_no_file_is_given(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps(dismiss(Dismissals.ID)) + "\n", stderr="")
        with mock.patch("subprocess.run", return_value=done) as run:
            self.assertEqual(ro.load("example/app", 7), [])
        self.assertEqual(run.call_args.args[0][:4], ["gh", "api", "--paginate", "repos/example/app/issues/7/comments?per_page=100"])
        self.assertEqual(run.call_args.args[0][4], "--jq")
        with self.assertRaises(ValueError):
            ro.fetch_comments("not a repo", 1)
        failing = subprocess.CalledProcessError(1, "gh")
        with mock.patch("subprocess.run", side_effect=failing), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["review-outcomes", "--repo", "example/app", "--pr", "1"]), 1)
        self.assertIn("CalledProcessError", err.getvalue())

    def test_finish_writes_the_comment_back_and_never_fails_the_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path, touched, comments = Path(tmp) / "comment.md", Path(tmp) / "touched.txt", Path(tmp) / "comments.jsonl"
            first = ro.finish(comment([finding()]), [], A, True)
            comments.write_text(json.dumps(bot(first)) + "\n")
            path.write_text(comment([], B))
            touched.write_text("app/a.py\n\n")
            args = ["--repo", "example/app", "--pr", "1", "--comments", str(comments), "--finish", str(path), "--sha", B]
            self.assertEqual(ro.main([*args, "--incremental", "true", "--touched", str(touched)]), 0)
            self.assertEqual(ro.record_of(path.read_text()), [entry(status="resolved", resolved=B)])
            path.write_text(comment([], B))
            rows = Path(tmp) / "findings.json"
            rows.write_text(json.dumps(ar.findings_rows([finding()])))
            self.assertEqual(ro.main([*args, "--findings", str(rows)]), 0)
            self.assertEqual(ro.record_of(path.read_text()), [entry()])
            path.write_text(comment([], B))
            rows.write_text("")
            self.assertEqual(ro.main([*args, "--findings", str(rows)]), 0)
            self.assertEqual(ro.record_of(path.read_text()), [entry(status="resolved", resolved=B)])
            path.write_text(comment([], B))
            rows.write_text('{"not": "rows"}')
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ro.main([*args, "--findings", str(rows)]), 0)
            self.assertIn("::warning::", out.getvalue())
            self.assertEqual(path.read_text(), comment([], B))
            path.write_text(comment([], B))
            touched.write_text("")
            self.assertEqual(ro.main([*args, "--incremental", "true", "--touched", str(touched)]), 0)
            self.assertEqual(ro.record_of(path.read_text()), [entry()])
            path.write_text(comment([finding()], B, ar.SECOND))
            self.assertEqual(ro.main([*args, "--second"]), 0)
            self.assertEqual([e["status"] for e in ro.record_of(path.read_text(), ar.SECOND) or []], ["open"])
            failing = subprocess.CalledProcessError(1, "gh")
            unchanged = comment([], C)
            path.write_text(unchanged)
            with mock.patch("subprocess.run", side_effect=failing), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(ro.main(["--repo", "example/app", "--pr", "1", "--finish", str(path), "--sha", C]), 0)
            self.assertIn("::warning::", out.getvalue())
            self.assertEqual(path.read_text(), unchanged)

    def test_the_module_runs_standalone_with_an_isolated_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "comment.md"
            path.write_text(comment([finding()]))
            comments = Path(tmp) / "comments.jsonl"
            comments.write_text("")
            done = subprocess.run(["python3", "-I", str(ROOT / "fleet_tools" / "review_outcomes.py"), "--finish", str(path), "--repo", "x/y",
                                   "--pr", "1", "--sha", A, "--comments", str(comments)], capture_output=True, text=True, cwd=tmp)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(ro.record_of(path.read_text()), [entry()])


@unittest.skipUnless(Path("/bin/bash").exists(), "needs bash")
class SelfTest(unittest.TestCase):
    def test_the_stub_reviewer_self_test_passes(self) -> None:
        done = subprocess.run(["bash", str(SELF_TEST)], capture_output=True, text=True, env={"PATH": os.environ.get("PATH", "")})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lines = done.stdout.splitlines()
        self.assertRegex(lines[0], r"^f:[0-9a-f]{8} open after the blocking review$")
        self.assertRegex(lines[1], r"^f:[0-9a-f]{8} resolved after the passing review$")
        self.assertRegex(lines[2], r"^f:[0-9a-f]{8} dismissed by the owner; the record is unchanged$")


if __name__ == "__main__":
    unittest.main()
