import contextlib
import io
import json
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from consumers import AGENT, RUNWAY, WAYPOINT, commit, copy, git, write

from fleet_tools import cli, config
from fleet_tools.checks import runner
from fleet_tools.checks import verification as check
from fleet_tools.checks import verification_guides as guides_check
from fleet_tools.checks.context import Advisory, Context, PullRequest

EVENTS = Path(__file__).resolve().parent / "fixtures" / "events"
CASES = ("pr-valid", "pr-none", "pr-two", "pr-malformed", "pr-draft", "pr-exempt")
FAILING = ("pr-none", "pr-two", "pr-malformed")


def run(*args: str, actions: bool = False) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    env = {"GITHUB_ACTIONS": "true"} if actions else {}
    with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        if not actions:
            os.environ.pop("GITHUB_ACTIONS", None)
        code = cli.main(["checks", *args])
    return code, out.getvalue(), err.getvalue()


def event(name: str) -> str:
    return str(EVENTS / f"{name}.json")


def require(root: Path) -> None:
    pyproject = root / "pyproject.toml"
    pyproject.write_text(pyproject.read_text(encoding="utf-8").replace("required = false", "required = true"), encoding="utf-8")


def outcome(root: Path, name: str) -> tuple[list[str], list[str], bool]:
    pull_request = runner.pull_request_of(EVENTS / f"{name}.json")
    context = Context(root, config.load(root), None, pull_request)
    problems, warnings, ran = runner.check(context, [check.CHECK])
    return problems, warnings, bool(ran)


class Payload(unittest.TestCase):
    def test_the_fixture_payloads_cover_each_case(self) -> None:
        for name in CASES:
            with self.subTest(name=name):
                payload = json.loads((EVENTS / f"{name}.json").read_text(encoding="utf-8"))
                self.assertEqual(sorted(payload["pull_request"]), ["base", "body", "draft", "head", "number", "user"])
        self.assertTrue(json.loads((EVENTS / "pr-draft.json").read_text(encoding="utf-8"))["pull_request"]["draft"])
        self.assertEqual(json.loads((EVENTS / "pr-exempt.json").read_text(encoding="utf-8"))["pull_request"]["user"]["login"],
                         "dependabot[bot]")

    def test_the_payload_gives_the_body_author_and_draft(self) -> None:
        found = runner.pull_request_of(EVENTS / "pr-draft.json")
        assert found is not None
        self.assertEqual((found.author, found.draft), ("octocat", True))
        self.assertIn("## Testing", found.body)
        found = runner.pull_request_of(EVENTS / "pr-valid.json")
        assert found is not None
        self.assertFalse(found.draft)

    def test_a_payload_without_a_pull_request_is_no_pull_request(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        push = write(root, "push.json", json.dumps({"ref": "refs/heads/main", "pusher": {"name": "octocat"}}))
        self.assertIsNone(runner.pull_request_of(push))
        code, out, err = run("--root", str(root), "--pr-event", str(push))
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("verification record", out)

    def test_a_null_body_and_a_missing_user_are_empty_strings(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        path = write(root, "pr.json", json.dumps({"pull_request": {"body": None, "draft": "yes"}}))
        self.assertEqual(runner.pull_request_of(path), PullRequest("", "", False))

    def test_an_unreadable_payload_exits_2(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        bad = write(root, "bad.json", "{not json")
        code, _, err = run("--root", str(root), "--pr-event", str(bad))
        self.assertEqual(code, 2)
        self.assertIn("fleet checks: --pr-event", err)
        code, _, err = run("--root", str(root), "--pr-event", str(root / "missing.json"))
        self.assertEqual(code, 2)


class Advisory_(unittest.TestCase):
    def test_a_valid_record_passes(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        self.assertEqual(outcome(root, "pr-valid"), ([], [], True))

    def test_each_failing_payload_is_one_warning(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        for name in FAILING:
            with self.subTest(name=name):
                problems, warnings, ran = outcome(root, name)
                self.assertEqual((problems, len(warnings), ran), ([], 1, True))
                self.assertTrue(warnings[0].startswith("the pull request's description, line "))
        self.assertIn("line 19: a second verification block", outcome(root, "pr-two")[1][0])
        self.assertIn("line 6: a `verified` row needs", outcome(root, "pr-malformed")[1][0])
        self.assertIn("no verification block", outcome(root, "pr-none")[1][0])

    def test_a_warning_is_a_line_and_exit_0(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        code, out, err = run("--root", str(root), "--pr-event", event("pr-none"))
        self.assertEqual(code, 0)
        self.assertTrue(err.startswith("warning: the pull request's description, line "), err)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("a verification record in the description", out)
        code, _, err = run("--root", str(root), "--pr-event", event("pr-none"), actions=True)
        self.assertEqual(code, 0)
        self.assertTrue(err.startswith("::warning::the pull request's description, line "), err)

    def test_a_draft_and_an_exempt_author(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        problems, warnings, ran = outcome(root, "pr-draft")
        self.assertEqual((problems, len(warnings), ran), ([], 1, True))
        self.assertEqual(outcome(root, "pr-exempt"), ([], [], True))


class Required(unittest.TestCase):
    def test_each_failing_payload_is_a_problem_on_a_ready_pull_request(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        require(root)
        for name in FAILING:
            with self.subTest(name=name):
                problems, warnings, _ = outcome(root, name)
                self.assertEqual((len(problems), warnings), (1, []))
                self.assertNotIsInstance(problems[0], Advisory)
        code, out, err = run("--root", str(root), "--pr-event", event("pr-malformed"))
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(len(err.splitlines()), 1)
        self.assertIn("line 6: a `verified` row needs a non-empty How", err)
        code, _, err = run("--root", str(root), "--pr-event", event("pr-none"), actions=True)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("::error::the pull request's description, line "), err)

    def test_a_valid_record_a_draft_and_an_exempt_author_still_pass(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        require(root)
        self.assertEqual(outcome(root, "pr-valid"), ([], [], True))
        self.assertEqual(outcome(root, "pr-exempt"), ([], [], True))
        problems, warnings, _ = outcome(root, "pr-draft")
        self.assertEqual((problems, len(warnings)), ([], 1))
        self.assertEqual(run("--root", str(root), "--pr-event", event("pr-draft"))[0], 0)

    def test_the_exempt_list_can_be_changed(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        pyproject = root / "pyproject.toml"
        pyproject.write_text(pyproject.read_text(encoding="utf-8") + 'exempt-authors = ["octocat"]\n', encoding="utf-8")
        self.assertEqual(outcome(root, "pr-none"), ([], [], True))
        problems, warnings, _ = outcome(root, "pr-exempt")
        self.assertEqual((problems, len(warnings)), ([], 1))

    def test_bad_keys_are_config_errors(self) -> None:
        for extra in ('required = "yes"\n', 'exempt-authors = "dependabot[bot]"\n', "exempt-authors = [1]\n"):
            with self.subTest(extra=extra):
                root = copy(self, RUNWAY, repository=False)
                pyproject = root / "pyproject.toml"
                text = pyproject.read_text(encoding="utf-8")
                pyproject.write_text(text.replace("required = false\n", "") + extra, encoding="utf-8")
                code, _, err = run("--root", str(root), "--pr-event", event("pr-valid"))
                self.assertEqual(code, 2)
                self.assertIn("[tool.fleet.verification]", err)


class Silent(unittest.TestCase):
    def test_without_the_table_the_check_doesnt_run(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        for name in CASES:
            with self.subTest(name=name):
                self.assertEqual(outcome(root, name), ([], [], False))
        code, out, err = run("--root", str(root), "--pr-event", event("pr-none"))
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("verification", out)

    def test_without_pr_event_the_check_doesnt_run(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        require(root)
        context = Context(root, config.load(root))
        self.assertFalse(check.CHECK.applies(context))
        code, out, err = run("--root", str(root))
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("verification", out)

    def test_other_checks_keep_their_exit_codes_beside_a_warning(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        write(root, "runway/storage/migrations/versions/0002_second_head.py", "down_revision = None\nrevision = '0002'\n")
        code, out, err = run("--root", str(root), "--pr-event", event("pr-none"))
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.splitlines()[0].startswith("warning: "))
        self.assertTrue(all(not line.startswith("warning: ") for line in err.splitlines()[1:]))
        self.assertGreaterEqual(len(err.splitlines()), 2)


class Forgery(unittest.TestCase):
    FORGED = "::error::forged %0A\n@someone <!-- fleet-verification -->"

    def test_messages_never_quote_the_description(self) -> None:
        for body in (self.FORGED, f"## Verification\n\n<!-- fleet-verification -->\n| Dimension | Status | How | Evidence |\n"
                                  f"| --- | --- | --- | --- |\n| {self.FORGED.replace(chr(10), ' ')} | maybe | x | y |\n<!-- /fleet-verification -->\n"):
            for required, draft in ((False, False), (True, False), (True, True)):
                with self.subTest(required=required, draft=draft, body=body[:20]):
                    found = check.check_description(body, required, draft)
                    self.assertEqual(len(found), 1)
                    message = found[0]
                    for forged in ("forged", "@someone", "<!-- fleet-verification -->", "\n", "\r", "%0A\n"):
                        self.assertNotIn(forged, message.replace("%0A", "\n") if forged == "\n" else message)
                    self.assertTrue(message.startswith("the pull request's description, line "))
                    self.assertEqual(isinstance(message, Advisory), not required or draft)

    def test_escaping_for_workflow_commands(self) -> None:
        self.assertEqual(check.escape("a%b\r\nc"), "a%25b%0D%0Ac")

    def test_a_forged_command_in_a_cell_is_data_and_a_forged_marker_is_rejected(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        body = ("<!-- fleet-verification -->\n| Dimension | Status | How | Evidence |\n| --- | --- | --- | --- |\n"
                "| ::error::forged %0A @someone | verified | `::error::x` | `%0A` |\n<!-- /fleet-verification -->\n")
        path = write(root, "pr.json", json.dumps({"pull_request": {"body": body, "draft": False, "user": {"login": "octocat"}}}))
        code, out, err = run("--root", str(root), "--pr-event", str(path), actions=True)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("a verification record in the description", out)
        body = body.replace("@someone", self.FORGED.replace(chr(10), " "))
        path = write(root, "pr.json", json.dumps({"pull_request": {"body": body, "draft": False, "user": {"login": "octocat"}}}))
        code, out, err = run("--root", str(root), "--pr-event", str(path), actions=True)
        self.assertEqual(code, 0)
        self.assertTrue(err.startswith("::warning::the pull request's description, line 4: ambiguous"), err)
        self.assertNotIn("forged", err)


def guides_outcome(root: Path, commits: str | None = None) -> tuple[list[str], list[str], bool]:
    found = runner.resolve(root, commits) if commits else None
    problems, warnings, ran = runner.check(Context(root, config.load(root), found), [guides_check.CHECK])
    return problems, warnings, bool(ran)


class Guides(unittest.TestCase):
    def test_the_fixture_s_guides_pass(self) -> None:
        root = copy(self, RUNWAY)
        self.assertEqual(guides_outcome(root), ([], [], True))
        code, out, err = run("--root", str(root))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("the verification guides", out)

    def test_a_stale_glob_is_a_problem(self) -> None:
        root = copy(self, RUNWAY)
        guide = root / "docs/verification/screens.md"
        guide.write_text(guide.read_text(encoding="utf-8").replace("- `screenshots/**`", "- `screenshots/**`\n- `runway/web/**`"),
                         encoding="utf-8")
        commit(root, "docs: a guide naming a removed directory")
        problems, warnings, _ = guides_outcome(root)
        self.assertEqual(warnings, [])
        self.assertEqual(problems, ["docs/verification/screens.md, line 10: the glob `runway/web/**` matches no tracked file, so the "
                                    "guide is stale; fix the glob or the guide (the fleet-verification-guide skill updates it)"])
        code, out, err = run("--root", str(root))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("runway/web/**", err)

    def test_a_glob_matching_only_an_untracked_file_is_stale(self) -> None:
        root = copy(self, RUNWAY)
        guide = root / "docs/verification/migrations.md"
        guide.write_text(guide.read_text(encoding="utf-8").replace("- `tests/test_migrations.py`", "- `tests/test_migrations.py`\n- `notes/*.txt`"),
                         encoding="utf-8")
        commit(root, "docs: a glob for an ignored file")
        write(root, "notes/scratch.txt", "x")
        problems, _, _ = guides_outcome(root)
        self.assertEqual(len(problems), 1)
        self.assertIn("`notes/*.txt` matches no tracked file", problems[0])

    def test_a_guide_that_doesnt_parse_is_a_problem(self) -> None:
        root = copy(self, RUNWAY)
        write(root, "docs/verification/broken.md", "# Broken\n\n## Paths\n\n- `tests/**`\n")
        commit(root, "docs: a broken guide")
        problems, _, _ = guides_outcome(root)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("docs/verification/broken.md, line 6: no `## How to verify` section"), problems[0])

    def test_an_uncovered_changed_path_is_a_warning(self) -> None:
        root = copy(self, RUNWAY)
        base = git(root, "rev-parse", "HEAD").strip()
        write(root, "frontend/verify/flows/budget.json", "{}\n")
        write(root, "runway/server/app.py", "app = 1\n")
        write(root, "docs/verification/screens.md", (root / "docs/verification/screens.md").read_text(encoding="utf-8") + "\n")
        commit(root, "feat: a server\n\n" + AGENT)
        problems, warnings, _ = guides_outcome(root, f"{base}..HEAD")
        self.assertEqual(problems, [])
        self.assertEqual(warnings, ["no verification guide covers 1 changed path(s): runway/server/app.py (the "
                                    "fleet-verification-guide skill writes or extends one)"])
        code, _, err = run("--root", str(root), "--commits", f"{base}..HEAD")
        self.assertEqual(code, 0, err)
        self.assertIn("warning: no verification guide covers 1 changed path(s): runway/server/app.py", err)

    def test_both_names_of_a_renamed_path_are_checked(self) -> None:
        root = copy(self, RUNWAY)
        base = git(root, "rev-parse", "HEAD").strip()
        git(root, "mv", "tests/test_budget.py", "tests/test_totals.py")
        commit(root, "refactor: rename a test\n\n" + AGENT)
        _, warnings, _ = guides_outcome(root, f"{base}..HEAD")
        self.assertEqual(len(warnings), 1)
        self.assertIn("covers 2 changed path(s): tests/test_budget.py, tests/test_totals.py", warnings[0])
        guide = root / "docs/verification/screens.md"
        guide.write_text(guide.read_text(encoding="utf-8").replace("- `screenshots/**`", "- `screenshots/**`\n- `tests/test_totals.py`"),
                         encoding="utf-8")
        commit(root, "docs: cover the renamed test\n\n" + AGENT)
        _, warnings, _ = guides_outcome(root, f"{base}..HEAD")
        self.assertIn("covers 1 changed path(s): tests/test_budget.py", warnings[0])

    def test_many_uncovered_paths_are_one_warning(self) -> None:
        root = copy(self, RUNWAY)
        base = git(root, "rev-parse", "HEAD").strip()
        for index in range(25):
            write(root, f"runway/domain/m{index:02}.py", "x = 1\n")
        commit(root, "feat: modules\n\n" + AGENT)
        _, warnings, _ = guides_outcome(root, f"{base}..HEAD")
        self.assertEqual(len(warnings), 1)
        self.assertIn("covers 25 changed path(s): runway/domain/m00.py,", warnings[0])
        self.assertTrue(warnings[0].endswith("runway/domain/m19.py and 5 more (the fleet-verification-guide skill writes or extends one)"))

    def test_messages_are_escaped_for_workflow_commands(self) -> None:
        root = copy(self, RUNWAY)
        base = git(root, "rev-parse", "HEAD").strip()
        write(root, "runway/100%\x1bodd.py", "x = 1\n")
        commit(root, "feat: an odd name\n\n" + AGENT)
        _, warnings, _ = guides_outcome(root, f"{base}..HEAD")
        self.assertIn("runway/100%25?odd.py", warnings[0])

    def test_it_runs_only_where_guides_is_set_in_a_checkout(self) -> None:
        self.assertEqual(guides_outcome(copy(self, WAYPOINT)), ([], [], False))
        self.assertEqual(guides_outcome(copy(self, RUNWAY, repository=False)), ([], [], False))
        root = copy(self, RUNWAY)
        pyproject = root / "pyproject.toml"
        pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('guides = "docs/verification"\n', ""), encoding="utf-8")
        self.assertEqual(guides_outcome(root), ([], [], False))

    def test_a_missing_directory_and_a_bad_key(self) -> None:
        root = copy(self, RUNWAY)
        pyproject = root / "pyproject.toml"
        pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('"docs/verification"', '"docs/nowhere"'), encoding="utf-8")
        problems, _, _ = guides_outcome(root)
        self.assertEqual(len(problems), 1)
        self.assertIn("doesn't exist inside the repository", problems[0])
        for bad in ('"../elsewhere"', '"."'):
            pyproject.write_text(re.sub(r'guides = "[^"]*"', f"guides = {bad}", pyproject.read_text(encoding="utf-8")), encoding="utf-8")
            code, _, err = run("--root", str(root))
            self.assertEqual(code, 2, bad)
            self.assertIn("[tool.fleet.verification] guides", err)
            self.assertIn("not `.`, `..` or absolute", err)


if __name__ == "__main__":
    unittest.main()
