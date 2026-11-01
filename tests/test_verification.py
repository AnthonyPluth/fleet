import contextlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fleet_tools import cli, verification
from fleet_tools.verification import BEGIN, END, HEADER, ParseError, parse

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "tests" / "fixtures" / "events"
WORKFLOWS = ROOT / ".github" / "workflows"
SEPARATOR = "| --- | --- | --- | --- |"
ROW = "| The page renders | verified | `make verify`, flow `page` | `screenshots/flow-page-desktop.png` |"
GAP = "| Live sync | not verified | no fake for the provider; one under `tests/fakes/` would make it testable | - |"


def body_of(name: str) -> str:
    return str(json.loads((EVENTS / f"{name}.json").read_text(encoding="utf-8"))["pull_request"]["body"])


def record(*rows: str, header: str = HEADER, separator: str | None = SEPARATOR, before: str = "## What\n\nA change.\n\n",
           after: str = "") -> str:
    table = [header, *([separator] if separator is not None else []), *rows]
    return before + "\n".join([BEGIN, *table, END]) + "\n" + after


def show(*args: str, stdin: str = "") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", io.StringIO(stdin)):
        code = cli.main(["verification", "show", *args])
    return code, out.getvalue(), err.getvalue()


class Parsing(unittest.TestCase):
    def test_a_valid_record_gives_one_dimension_per_row(self) -> None:
        found = parse(body_of("pr-valid"))
        self.assertEqual([(d.dimension, d.status) for d in found], [
            ("Budget page renders the new total", "verified"),
            ("Empty budget shows the hint", "verified"),
            ("Import of a `|`-separated statement", "not verified"),
        ])
        self.assertEqual(found[0].how, "`make verify`, flow `budget-overview`")
        self.assertEqual(found[0].evidence, "`screenshots/flow-budget-overview-desktop.png`")
        self.assertEqual(found[2].evidence, "-")

    def test_crlf_endings_parse_the_same(self) -> None:
        body = body_of("pr-valid")
        self.assertIn("\n", body)
        self.assertEqual(parse(body.replace("\n", "\r\n")), parse(body))

    def test_a_table_without_a_separator_row_still_parses(self) -> None:
        self.assertEqual(len(parse(record(ROW, separator=None))), 1)

    def test_text_between_the_markers_that_isnt_the_table_is_ignored(self) -> None:
        body = record(ROW, GAP, before="", after="") .replace(BEGIN, BEGIN + "\nA note the agent left.\n")
        self.assertEqual([d.status for d in parse(body)], ["verified", "not verified"])

    def test_everything_outside_the_markers_is_ignored(self) -> None:
        body = record(ROW, before="| Dimension | Status | How | Evidence |\n| x | verified | | |\n\n", after="\n| stray | row |\n")
        self.assertEqual(len(parse(body)), 1)

    def test_markers_inside_fenced_code_dont_count(self) -> None:
        fenced = "```md\n" + BEGIN + "\n" + HEADER + "\n| in a fence | verified | a | b |\n" + END + "\n```\n\n"
        self.assertEqual([d.dimension for d in parse(record(ROW, before=fenced))], ["The page renders"])
        tilde = "~~~\n" + BEGIN + "\n~~~\n"
        self.assertEqual(len(parse(record(ROW, before=tilde))), 1)
        with self.assertRaises(ParseError) as caught:
            parse(fenced)
        self.assertEqual(caught.exception.line, fenced.rstrip("\n").count("\n") + 1)

    def test_a_fence_opened_in_a_list_item_or_blockquote_still_hides_its_markers(self) -> None:
        example = BEGIN + "\n" + HEADER + "\n| --- | --- | --- | --- |\n| in an example | verified | a | b |\n" + END + "\n"
        for prefix in ("- ", "* ", "+ ", "1. ", "12) ", "> ", "> - ", "  - ", ">> "):
            with self.subTest(prefix=prefix):
                close = re.sub(r"[-*+0-9.)]", " ", prefix)
                fenced = prefix + "```md\n" + example + close + "```\n\n"
                self.assertEqual([d.dimension for d in parse(fenced + record(GAP, before=""))], ["Live sync"])
                self.assertEqual([d.dimension for d in parse(prefix + "~~~\n" + example + close + "~~~\n" + record(GAP, before=""))], ["Live sync"])
                with self.assertRaises(ParseError) as caught:
                    parse(fenced)
                self.assertIn("no verification block", caught.exception.rule)

    def test_a_closing_fence_matches_the_opening_one(self) -> None:
        example = BEGIN + "\n" + HEADER + "\n| in an example | verified | a | b |\n" + END + "\n"
        for shut in ("```", "~~~", "``` not a close"):
            with self.subTest(shut=shut), self.assertRaises(ParseError) as caught:
                parse("````\n" + example + shut + "\n" + record(GAP, before="") + "````\n")
            self.assertIn("no verification block", caught.exception.rule)
        self.assertEqual([d.dimension for d in parse("```\n" + example + "`````\n" + record(GAP, before=""))], ["Live sync"])
        self.assertEqual([d.dimension for d in parse("- ```\n" + example + "  ```\n" + record(GAP, before=""))], ["Live sync"])
        self.assertEqual([d.dimension for d in parse("- ```\n" + example + "     ```\n" + record(GAP, before=""))], ["Live sync"])
        with self.assertRaises(ParseError):
            parse("- ```\n" + example + "```\n" + record(GAP, before=""))
        with self.assertRaises(ParseError):
            parse("- ```\n" + example + "      ```\n" + record(GAP, before=""))

    def test_an_unclosed_fence_hides_everything_after_it(self) -> None:
        with self.assertRaises(ParseError) as caught:
            parse("- ```\n" + record(GAP, before=""))
        self.assertEqual((caught.exception.line, "never closed" in caught.exception.rule), (1, True))
        with self.assertRaises(ParseError) as caught:
            parse(BEGIN + "\n" + HEADER + "\n" + GAP + "\n> ```\n" + END + "\n")
        self.assertEqual(caught.exception.line, 1)
        self.assertIn("no closing", caught.exception.rule)
        with self.assertRaises(ParseError) as caught:
            parse(record(GAP, before="") + "```\n" + record(ROW, before=""))
        self.assertEqual(caught.exception.line, 1 + record(GAP, before="").count("\n"))
        self.assertIn("never closed", caught.exception.rule)

    def test_a_fence_line_in_another_container_doesnt_close_a_fence(self) -> None:
        fake = HEADER + "\n| in an example | verified | a | b |\n" + END + "\n"
        real = record(GAP, before="")
        for inner in ("- ```", "> ```", "1. ```", "    ```", "> - ```", "* ~~~"):
            with self.subTest(inner=inner):
                body = "```\n" + BEGIN + "\n" + inner + "\n" + fake + "```\n\n" + real
                self.assertEqual([d.dimension for d in parse(body)], ["Live sync"])
        for outer, close, stray in (("- ```", "  ```", "```"), ("> ```", "> ```", "```"), ("> ```", "> ```", "- ```"),
                                    ("- ```", "  ```", "> ```"), ("1. ```", "   ```", "- ```")):
            with self.subTest(outer=outer, stray=stray):
                body = outer + "\n" + BEGIN + "\n" + stray + "\n" + fake + close + "\n\n" + real
                self.assertEqual([d.dimension for d in parse(body)], ["Live sync"])

    def test_a_fence_whose_closing_context_is_ambiguous_stays_open(self) -> None:
        fake = HEADER + "\n| in an example | verified | a | b |\n" + END + "\n"
        real = record(GAP, before="")
        for outer, stray in (("- ```", "```"), ("> ```", "```"), ("```", "    ```"), ("> ```", ">> ```")):
            with self.subTest(outer=outer, stray=stray), self.assertRaises(ParseError) as caught:
                parse(outer + "\n" + BEGIN + "\n" + stray + "\n" + fake + real)
            self.assertEqual(caught.exception.line, 1)
            self.assertIn("never closed", caught.exception.rule)

    def test_every_container_prefix_pair_keeps_an_example_out_of_the_record(self) -> None:
        fake = HEADER + "\n| in an example | verified | a | b |\n" + END + "\n"
        real = record(GAP, before="")
        openers = ["", "> ", "- ", "1. ", "- - ", "> - ", "1. - ", ">> ", "  ", "   - ", "> > - ", "* + "]
        closers = {opener: re.sub(r"[-*+0-9.)]", " ", opener) for opener in openers}
        for opener in openers:
            for closer in {*closers.values(), "- ", "> ", "    ", "> > "}:
                with self.subTest(opener=opener, closer=closer):
                    body = opener + "```md\n" + BEGIN + "\n" + fake + closer + "```\n\n" + real
                    try:
                        found = [d.dimension for d in parse(body)]
                    except ParseError as error:
                        self.assertIn("never closed", error.rule)
                        found = []
                    self.assertNotIn("in an example", found)
                    if closer == closers[opener]:
                        self.assertEqual(found, ["Live sync"])

    def test_malformed_container_punctuation_before_a_fence_fails_closed(self) -> None:
        real = record(GAP, before="")
        for line in ("-- ```", ">-```", "1.2 ```", "-```", "...```", "> >> - 1. ```"):
            with self.subTest(line=line), self.assertRaises(ParseError) as caught:
                parse(line + "\n" + BEGIN + "\n" + HEADER + "\n| in an example | verified | a | b |\n" + END + "\n```\n" + real)
            self.assertIn("never closed", caught.exception.rule)
        with self.assertRaises(ParseError):
            parse(("> " * 40) + "```\n" + BEGIN + "\n" + HEADER + "\n| x | verified | a | b |\n" + END + "\n" + ("> " * 40) + "```\n" + real)

    def test_a_marker_inside_a_list_item_or_blockquote_is_an_error(self) -> None:
        for prefix in ("- ", "> ", "1. ", "> - ", "  - "):
            with self.subTest(prefix=prefix), self.assertRaises(ParseError) as caught:
                parse(prefix + BEGIN + "\n" + HEADER + "\n" + GAP + "\n" + prefix + END + "\n")
            self.assertEqual(caught.exception.line, 1)
            self.assertIn("inside a list item or blockquote", caught.exception.rule)
            with self.assertRaises(ParseError) as caught:
                parse(record(GAP, before="") + prefix + END + "\n")
            self.assertEqual(caught.exception.line, 1 + record(GAP, before="").count("\n"))
        self.assertEqual(len(parse("- ```\n  " + BEGIN + "\n  ```\n" + record(GAP, before=""))), 1)

    def test_a_very_long_run_of_whitespace_parses_in_linear_time(self) -> None:
        run = 60000
        for prefix in ("", "> ", "- ", "1. ", ">> - "):
            for blank in (" ", "\t", " \t"):
                with self.subTest(prefix=prefix, blank=blank):
                    line = prefix + (blank * run)[:run] + "x"
                    started = time.monotonic()
                    self.assertEqual([d.dimension for d in parse(line + "\n" + record(GAP, before=""))], ["Live sync"])
                    self.assertIsNone(verification.fence_of(line))
                    self.assertIsNone(verification.fence_of(prefix + ("- " * run)[:run] + "x"))
                    self.assertFalse(verification.prefixed_marker(line))
                    self.assertFalse(verification.is_separator("|" + " " * run + "x"))
                    self.assertFalse(verification.is_separator("|" + " " * run + "|"))
                    self.assertLess(time.monotonic() - started, 0.5)

    def test_an_escaped_pipe_is_a_literal_pipe_in_a_cell(self) -> None:
        found = parse(record("| a \\| b | verified | `x \\| y` | out \\| put |"))
        self.assertEqual((found[0].dimension, found[0].how, found[0].evidence), ("a | b", "`x | y`", "out | put"))

    def check_rejects(self, body: str, line: int, rule: str) -> None:
        with self.assertRaises(ParseError) as caught:
            parse(body)
        self.assertEqual(caught.exception.line, line, str(caught.exception))
        self.assertIn(rule, caught.exception.rule)
        self.assertEqual(str(caught.exception), f"line {line}: {caught.exception.rule}")

    def test_missing_markers(self) -> None:
        self.check_rejects(body_of("pr-none"), body_of("pr-none").rstrip("\n").count("\n") + 1, "no verification block")
        self.check_rejects("", 1, "no verification block")
        self.check_rejects("## What\n\n" + BEGIN + "\n" + HEADER + "\n" + ROW + "\n", 3, "no closing")
        self.check_rejects(END + "\n" + HEADER + "\n" + ROW + "\n", 3, "no verification block")

    def test_a_marker_must_be_alone_on_its_line(self) -> None:
        self.check_rejects("text " + BEGIN + "\n" + HEADER + "\n" + ROW + "\n" + END + "\n", 4, "no verification block")

    def test_two_blocks(self) -> None:
        body = body_of("pr-two")
        second = [i for i, line in enumerate(body.split("\n"), start=1) if line == BEGIN][1]
        self.check_rejects(body, second, "a second verification block")
        self.check_rejects(record(ROW) + END + "\n", 10, "stray closing marker")

    def test_a_missing_or_misspelt_header(self) -> None:
        self.check_rejects(record(ROW, header="| Dimension | Status | How | Evidenc |"), 6, "header isn't exactly")
        self.check_rejects(record(ROW, header="| Dimension | Status | How | Evidence"), 6, "header isn't exactly")
        self.check_rejects(record(ROW, header="|Dimension|Status|How|Evidence|"), 6, "header isn't exactly")
        self.check_rejects(BEGIN + "\n\n" + END + "\n", 1, "no table")
        self.check_rejects(record(), 6, "no rows")

    def test_an_unknown_status(self) -> None:
        for status in ("Verified", "verified ✓", "unverified", "partly verified", "", "-"):
            with self.subTest(status=status):
                self.check_rejects(record(f"| a | {status} | how | evidence |"), 8, "isn't `verified` or `not verified`")

    def test_a_verified_row_with_empty_how_or_evidence(self) -> None:
        for how, evidence in (("", "e"), ("-", "e"), ("—", "e"), ("h", ""), ("h", "-"), ("h", " — ")):
            with self.subTest(how=how, evidence=evidence):
                self.check_rejects(record(f"| a | verified | {how} | {evidence} |"), 8, "a `verified` row needs")

    def test_a_not_verified_row_with_empty_how(self) -> None:
        for how in ("", "-", "—"):
            with self.subTest(how=how):
                self.check_rejects(record(f"| a | not verified | {how} | - |"), 8, "a `not verified` row needs")
        self.assertEqual(parse(record("| a | not verified | why | |"))[0].evidence, "")

    def test_a_row_with_the_wrong_number_of_cells_or_no_dimension(self) -> None:
        self.check_rejects(record("| a | verified | how |"), 8, "3 cells, not 4")
        self.check_rejects(record("| a | verified | how | e | extra |"), 8, "5 cells, not 4")
        self.check_rejects(record("| | verified | how | e |"), 8, "Dimension cell is empty")

    def test_the_line_number_points_at_the_bad_row(self) -> None:
        self.check_rejects(record(ROW, GAP, "| bad | maybe | h | e |", before="x\n\ny\n"), 9, "isn't `verified`")

    def test_a_description_over_the_size_limit_is_an_error_not_a_cut(self) -> None:
        body = record(ROW, after="x" * verification.MAX_BODY)
        self.check_rejects(body, 1, "over 65,536 characters")
        self.assertEqual(len(parse(record(ROW, after="x" * (verification.MAX_BODY - len(record(ROW)))))), 1)

    def test_a_block_over_100_rows_is_an_error(self) -> None:
        rows = [f"| dimension {i} | verified | how | evidence |" for i in range(verification.MAX_ROWS + 1)]
        self.check_rejects(record(*rows), 8 + verification.MAX_ROWS, "over 100 rows")
        self.assertEqual(len(parse(record(*rows[:-1]))), verification.MAX_ROWS)

    def test_a_cell_over_1000_characters_is_an_error(self) -> None:
        self.check_rejects(record(f"| a | verified | {'h' * (verification.MAX_CELL + 1)} | e |"), 8, "over 1,000 characters")
        self.assertEqual(len(parse(record(f"| a | verified | {'h' * verification.MAX_CELL} | e |"))[0].how), verification.MAX_CELL)

    def test_the_parser_runs_nothing_and_keeps_cells_as_text(self) -> None:
        cell = "`$(rm -rf /)` ${{ github.token }} <!-- x --> @someone"
        found = parse(record(f"| {cell} | verified | {cell} | {cell} |"))
        self.assertEqual((found[0].dimension, found[0].how, found[0].evidence), (cell, cell, cell))


class Show(unittest.TestCase):
    def test_body_file_json_round_trips_the_fixture_record(self) -> None:
        code, out, err = show("--body-file", "-", "--json", stdin=body_of("pr-valid"))
        self.assertEqual((code, err), (0, ""))
        found = json.loads(out)
        self.assertIsNone(found["pr"])
        self.assertEqual(found["dimensions"], [
            {"dimension": "Budget page renders the new total", "status": "verified", "how": "`make verify`, flow `budget-overview`",
             "evidence": "`screenshots/flow-budget-overview-desktop.png`"},
            {"dimension": "Empty budget shows the hint", "status": "verified", "how": "`tests/test_budget.py::EmptyBudget`",
             "evidence": "`test_an_empty_budget_shows_the_hint` passed"},
            {"dimension": "Import of a `|`-separated statement", "status": "not verified",
             "how": "no fixture statement uses a pipe; a fixture in `tests/fixtures/statements/` would make it testable", "evidence": "-"},
        ])
        self.assertEqual(sorted(found), ["dimensions", "pr"])

    def test_body_file_reads_a_path_too(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "body.md"
            path.write_text(body_of("pr-valid"), encoding="utf-8")
            code, out, _ = show("--body-file", str(path))
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("verified      Budget page renders the new total\n"))
        self.assertIn("not verified  Import of a `|`-separated statement\n", out)
        self.assertIn("              how: no fixture statement uses a pipe", out)
        self.assertNotIn("evidence: -", out)

    def test_text_output_is_defused(self) -> None:
        cell = "::error::forged %0A @someone <!-- fleet-verification -->"
        code, out, err = show("--body-file", "-", stdin=record(f"| {cell} | verified | {cell} | {cell} |"))
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("@someone", out)
        self.assertNotIn("<!--", out)
        self.assertIn("&lt;!-- fleet-verification --&gt;" if "&gt;" in out else "&lt;!-- fleet-verification -->", out)
        self.assertTrue(all(not line.startswith("::") for line in out.splitlines()))

    def test_json_output_carries_the_cells_as_data(self) -> None:
        cell = "@someone <!-- x --> ::error::y"
        code, out, _ = show("--body-file", "-", "--json", stdin=record(f"| {cell} | not verified | {cell} | |"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["dimensions"][0]["how"], cell)

    def test_a_record_that_doesnt_parse_exits_1_with_the_line(self) -> None:
        code, out, err = show("--body-file", "-", stdin=body_of("pr-malformed"))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("line 6: a `verified` row needs", err)
        code, _, err = show("--body-file", "-", "--json", stdin=body_of("pr-none"))
        self.assertEqual(code, 1)
        self.assertIn("no verification block", err)

    def test_an_unreadable_file_exits_1(self) -> None:
        code, _, err = show("--body-file", "/nonexistent/body.md")
        self.assertEqual(code, 1)
        self.assertIn("can't read the description", err)

    def test_pr_reads_the_description_through_gh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "gh"
            log = Path(directory) / "log"
            fake.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$FAKE_LOG\"\ncat \"$FAKE_BODY\"\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            body = Path(directory) / "body.md"
            body.write_text(body_of("pr-valid"), encoding="utf-8")
            env = {"PATH": f"{directory}{os.pathsep}{os.environ.get('PATH', '')}", "FAKE_LOG": str(log), "FAKE_BODY": str(body)}
            with mock.patch.dict(os.environ, env):
                code, out, err = show("--pr", "12", "--json")
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["api", "repos/{owner}/{repo}/pulls/12", "--jq", ".body"])
        found = json.loads(out)
        self.assertEqual(found["pr"], 12)
        self.assertEqual(len(found["dimensions"]), 3)

    def test_a_failing_gh_exits_1(self) -> None:
        failing = subprocess.CalledProcessError(1, ["gh", "api"], stderr="HTTP 404")
        with mock.patch("subprocess.run", side_effect=failing):
            code, _, err = show("--pr", "12")
        self.assertEqual(code, 1)
        self.assertIn("`gh api` failed: HTTP 404", err)

    def test_pr_and_body_file_exclude_each_other_and_one_is_required(self) -> None:
        for args in (["--pr", "1", "--body-file", "-"], []):
            with self.subTest(args=args), self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
                cli.main(["verification", "show", *args])
            self.assertEqual(caught.exception.code, 2)


class Template(unittest.TestCase):
    def test_fleets_pull_request_template_has_the_section_after_testing(self) -> None:
        text = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
        headings = [line for line in text.splitlines() if line.startswith("## ")]
        self.assertEqual(headings, ["## What", "## Testing", "## Verification"])
        section = text.split("## Verification", 1)[1]
        self.assertEqual([line for line in section.splitlines() if line.startswith("<!-- ") or line.startswith("| ")][-4:],
                         [BEGIN, HEADER, SEPARATOR, END])
        self.assertIn("`not verified`", section)
        self.assertIn("Verification", text.split("## Testing", 1)[1].split("## Verification")[0])
        with self.assertRaises(ParseError) as caught:
            parse(text)
        self.assertIn("no rows", caught.exception.rule)

    def test_the_adopting_doc_gives_the_apps_the_same_section_and_the_keys(self) -> None:
        text = (ROOT / "docs" / "adopting.md").read_text(encoding="utf-8")
        for phrase in ("[tool.fleet.verification]", "required = false", "exempt-authors", BEGIN, END, HEADER, "types: [opened, synchronize, reopened, ready_for_review, edited]",
                       "fleet verification show", "--pr-event"):
            self.assertIn(phrase, text)


class Skill(unittest.TestCase):
    def test_the_skill_carries_the_rules_and_names_no_app(self) -> None:
        text = (ROOT / "plugins" / "fleet" / "skills" / "verify-change" / "SKILL.md").read_text(encoding="utf-8")
        for phrase in ("name: verify-change", "[tool.fleet.verification]", HEADER, "`verified` or `not verified`", "\\|",
                       "Never mark a dimension `verified`", "Every gap goes in as `not verified`", "verify skill", "Testing",
                       "fleet verification show --pr N"):
            self.assertIn(phrase, text)
        for word in ("Waypoint", "Runway", "Darkroom", "AnthonyPluth", "http"):
            self.assertNotIn(word, text)
        example = text.split("```md\n", 1)[1].split("```", 1)[0]
        self.assertEqual([d.status for d in parse(example)], ["verified", "verified", "not verified"])

    def test_the_fixture_consumers_carry_it_vendored(self) -> None:
        for consumer in ("consumer-runway", "consumer-waypoint"):
            path = ROOT / "tests" / "fixtures" / consumer / ".claude" / "skills" / "fleet-verify-change" / "SKILL.md"
            self.assertTrue(path.is_file(), consumer)
            self.assertIn("name: fleet-verify-change", path.read_text(encoding="utf-8"))


class WorkflowsNeverInterpolateTheDescription(unittest.TestCase):
    def test_checks_yml_reads_the_description_from_the_event_file_only(self) -> None:
        text = (WORKFLOWS / "checks.yml").read_text(encoding="utf-8")
        self.assertNotIn("github.event.pull_request.body", text)
        self.assertIn('--pr-event "$GITHUB_EVENT_PATH"', text)

    def test_no_workflow_puts_the_description_in_a_run_script_or_a_with(self) -> None:
        env_line = re.compile(r"^\s+[A-Z_]+: \$\{\{ github\.event\.pull_request\.body \}\}$")
        for path in sorted(WORKFLOWS.glob("*.yml")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if "github.event.pull_request.body" in line:
                    self.assertRegex(line, env_line, f"{path.name}:{number}: the description reaches a script only as a step's "
                                                     "environment variable, never interpolated into it")


if __name__ == "__main__":
    unittest.main()
