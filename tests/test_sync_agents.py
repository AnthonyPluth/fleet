import contextlib
import io
import random
import unittest
from pathlib import Path

from consumers import FIXTURES, RUNWAY, WAYPOINT, copy, temporary, write

from fleet_tools import agents_md, cli

ROOT = Path(__file__).resolve().parent.parent
VERSION = agents_md.own_version()
SHIPPED = agents_md.sections()
RULE = (
    "**A mistake that repeats gets a check.** When review flags the same kind of mistake on a second pull request, the "
    "fix adds a check that would have caught it: a lint rule (ESLint, or Semgrep in `.semgrep/` with a failing and a "
    "passing example), a type (strict mypy, a `NewType` or `Literal`), or a test. The item below then names that check "
    "in its parenthesis (`checked: waypoint-trip-visibility`, `checked: tests/test_offline.py`). An item stays "
    "*human-judged* only when no check can express it, and says why. Every month the \"Repeated review findings\" issue "
    "lists the kinds that came back; a row there is closed by a check, not by a new sentence here."
)
SHARED_MISTAKES = ("Failure paths", "Access that outlives the person", "Undo and restore", "GitHub Actions", "Layout",
                   "Verification that doesn't cover the change")
REGENERATE = "regenerate it with `poetry run fleet sync --to v$(cat VERSION) --root {}`"


def sync(text: str) -> str:
    return agents_md.sync(text, SHIPPED, VERSION)[0]


def run(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["sync", *args])
    return code, out.getvalue(), err.getvalue()


def fixtures() -> list[str]:
    return [(FIXTURES / "agents-md" / f"{app}-before.md").read_text(encoding="utf-8") for app in ("waypoint", "runway")]


class Sections(unittest.TestCase):
    def test_every_section_file_is_shipped_in_order(self) -> None:
        files = sorted(path.stem for path in (ROOT / agents_md.SECTIONS).glob("*.md"))
        self.assertEqual(files, sorted(agents_md.ORDER))
        self.assertEqual(list(SHIPPED), list(agents_md.ORDER))

    def test_each_names_its_heading(self) -> None:
        self.assertEqual({name: section.heading for name, section in SHIPPED.items()}, {
            "starting-work": "Starting work",
            "working-in-a-container": "Working in a container",
            "mistakes": "Mistakes that keep coming back",
            "git-and-prs": "Git and PRs",
        })

    def test_mistakes_opens_with_the_rule_verbatim(self) -> None:
        self.assertTrue(SHIPPED["mistakes"].body.startswith(RULE + "\n\n"))

    def test_mistakes_lists_the_shared_items_each_with_its_parenthesis(self) -> None:
        items = [line for line in SHIPPED["mistakes"].body.splitlines() if line.startswith("- **")]
        self.assertEqual([item[4:item.index("**", 4)] for item in items], list(SHARED_MISTAKES))
        for item in items:
            self.assertRegex(item, r"^- \*\*[^*]+\*\* \((checked: |partly checked: |human-judged: )")

    def test_git_and_prs_says_a_pull_request_for_every_change(self) -> None:
        body = SHIPPED["git-and-prs"].body
        self.assertIn("Open a pull request for every change, once it's pushed", body)
        self.assertNotIn("only when asked", body)
        for status in ('"Merge gate"', '"Agent review"', '"Second review"', "Co-Authored-By", "`make check`"):
            self.assertIn(status, body)

    def test_nothing_app_specific_in_the_blocks(self) -> None:
        for section in SHIPPED.values():
            for word in ("Codex", "SQLAlchemy", "Postgres", "oidc.", "Waypoint's", "Runway's", "http://", "https://"):
                self.assertNotIn(word, section.body, section.name)

    def test_a_section_without_front_matter_or_with_a_marker_is_refused(self) -> None:
        with self.assertRaises(agents_md.MarkerError):
            agents_md.parse_section("x", "- no front matter\n")
        with self.assertRaises(agents_md.MarkerError):
            agents_md.parse_section("x", "---\nheading: X\n---\n<!-- fleet:end x -->\n")
        with self.assertRaises(agents_md.MarkerError):
            agents_md.parse_section("x", "---\nheading: X\n---\nno newline")


class Sync(unittest.TestCase):
    def test_a_marked_block_is_replaced_and_the_rest_kept(self) -> None:
        text = ("# Guide\n\n## Starting work\n\nBefore.\n\n<!-- fleet:begin starting-work v0.0.1 -->\nold\n"
                "<!-- fleet:end starting-work -->\nAfter.\n")
        result = sync(text)
        self.assertTrue(result.startswith("# Guide\n\n## Starting work\n\nBefore.\n\n" +
                                          agents_md.render(SHIPPED["starting-work"], VERSION) + "\nAfter.\n"))

    def test_a_missing_block_goes_at_the_end_of_its_heading(self) -> None:
        text = "# Guide\n\n## Git and PRs\n\n- An app line.\n\n\n## Model routing\n\nSee CLAUDE.md.\n"
        result = sync(text)
        block = agents_md.render(SHIPPED["git-and-prs"], VERSION)
        self.assertIn(f"## Git and PRs\n\n- An app line.\n\n{block}\n\n\n## Model routing\n", result)

    def test_a_heading_inside_a_code_fence_or_a_deeper_heading_does_not_end_the_section(self) -> None:
        text = "## Git and PRs\n\n```md\n## Model routing\n```\n\n### More\n\nText.\n\n## Next\n"
        result = sync(text)
        self.assertIn("### More\n\nText.\n\n<!-- fleet:begin git-and-prs", result)
        self.assertEqual(result.count("## Git and PRs\n"), 1)

    def test_a_missing_heading_is_created_at_the_end_in_order(self) -> None:
        result = sync("# Guide\n\nIntro.")
        self.assertTrue(result.startswith("# Guide\n\nIntro.\n\n## Starting work\n\n<!-- fleet:begin starting-work"))
        positions = [result.index(f"## {SHIPPED[name].heading}\n") for name in agents_md.ORDER]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(result.endswith("<!-- fleet:end git-and-prs -->\n"))

    def test_an_empty_file_gets_every_block(self) -> None:
        result = sync("")
        self.assertTrue(result.startswith("## Starting work\n\n<!-- fleet:begin starting-work"))
        self.assertEqual(len(agents_md.blocks(result)), len(agents_md.ORDER))

    def test_the_last_heading_without_a_final_newline(self) -> None:
        result = sync("## Git and PRs\n\n- Last line")
        self.assertIn("- Last line\n\n<!-- fleet:begin git-and-prs", result)

    def test_sync_reports_what_changed(self) -> None:
        text = sync("")
        self.assertEqual(agents_md.sync(text, SHIPPED, VERSION)[1], [])
        stale = text.replace(f"starting-work {VERSION}", "starting-work v0.0.1")
        self.assertEqual(agents_md.sync(stale, SHIPPED, VERSION)[1], ["replaced starting-work"])

    def test_marker_errors(self) -> None:
        for text in (
            "<!-- fleet:begin mistakes v1.0.0 -->\n",
            "<!-- fleet:end mistakes -->\n",
            "<!-- fleet:begin mistakes v1.0.0 -->\n<!-- fleet:begin git-and-prs v1.0.0 -->\n",
            "<!-- fleet:begin mistakes v1.0.0 -->\n<!-- fleet:end git-and-prs -->\n",
            "<!-- fleet:begin mistakes v1.0.0 -->\n<!-- fleet:end mistakes -->\n" * 2,
            "<!-- fleet:begin mistakes 1.0 -->\n",
            "Text <!-- fleet:end mistakes --> inline.\n",
            "<!-- fleet:begin retired v1.0.0 -->\n<!-- fleet:end retired -->\n",
        ):
            with self.subTest(text=text), self.assertRaises(agents_md.MarkerError):
                sync(text)


class Property(unittest.TestCase):
    PIECES = ("", "\n", "\n\n", "Plain text.\n", "- An app item.\n", "## Other heading\n", "### Deeper\n", "```\n",
              "Typography \u2019 \u2013 \u2212 \u00e9\n", "  indented\n", "<!-- an ordinary comment -->\n", "#hashtag\n", "\t\n")

    def mutate(self, rng: random.Random, text: str) -> str:
        lines = text.splitlines(keepends=True)
        protected: set[int] = set()
        for block in agents_md.blocks(text):
            start = text[:block.start].count("\n")
            protected.update(range(start + 1, text[:block.end].count("\n") + 1))
        for _ in range(rng.randint(1, 8)):
            choices = [index for index in range(len(lines) + 1) if index not in protected]
            at = rng.choice(choices)
            lines.insert(at, rng.choice(self.PIECES))
            protected = {index + 1 if index >= at else index for index in protected}
        for name in [block.name for block in agents_md.blocks("".join(lines))]:
            if rng.random() < 0.5:
                old = "".join(lines)
                block = next(each for each in agents_md.blocks(old) if each.name == name)
                body = rng.choice(("", "stale\n", "- edited item\n\n", SHIPPED[block.name].body[:-2] + "\n"))
                new = old[:block.body_start] + body + old[block.body_end:]
                if rng.random() < 0.5:
                    new = new.replace(f"{block.name} {block.version} -->", f"{block.name} v0.0.{rng.randint(0, 9)} -->")
                lines = new.splitlines(keepends=True)
        return "".join(lines)

    def test_sync_is_idempotent_and_keeps_every_byte_outside_the_markers(self) -> None:
        rng = random.Random(9)
        bases = [text for text in fixtures()] + [sync(text) for text in fixtures()]
        for case in range(300):
            original = self.mutate(rng, rng.choice(bases))
            with self.subTest(case=case):
                once = sync(original)
                self.assertEqual(sync(once), once)
                inserted = len(agents_md.ORDER) - len(agents_md.blocks(original))
                if not inserted:
                    self.assertEqual(agents_md.outside(once), agents_md.outside(original))
                for block in agents_md.blocks(once):
                    self.assertEqual(once[block.body_start:block.body_end], SHIPPED[block.name].body)
                    self.assertEqual(block.version, VERSION)

    def test_an_inserted_block_is_all_that_is_added(self) -> None:
        for original in fixtures():
            once = sync(original)
            names = {block.name for block in agents_md.blocks(original)}
            stripped = once
            for block in reversed(agents_md.blocks(once)):
                if block.name not in names:
                    stripped = stripped[:block.start - 1] + stripped[block.end + 1:]
            self.assertEqual(agents_md.outside(stripped), agents_md.outside(original))


class Fixtures(unittest.TestCase):
    def test_each_fixture_consumer_is_its_before_file_synced(self) -> None:
        for app, consumer in (("waypoint", WAYPOINT), ("runway", RUNWAY)):
            with self.subTest(app=app):
                before = (FIXTURES / "agents-md" / f"{app}-before.md").read_text(encoding="utf-8")
                after = (consumer / "AGENTS.md").read_text(encoding="utf-8")
                self.assertEqual(sync(before), after, REGENERATE.format(consumer.relative_to(ROOT)))
                self.assertEqual((consumer / ".fleet/version").read_text(encoding="utf-8"), VERSION + "\n")

    def test_the_app_items_follow_the_mistakes_block(self) -> None:
        for consumer, first in ((WAYPOINT, "Tests that share the database"), (RUNWAY, "Tests that share the database")):
            text = (consumer / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn(f"<!-- fleet:end mistakes -->\n\n- **{first}**", text)

    def test_fleets_own_agents_md_carries_the_current_blocks(self) -> None:
        text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(sync(text), text, REGENERATE.format("."))
        self.assertEqual(len(agents_md.blocks(text)), len(agents_md.ORDER))


class Command(unittest.TestCase):
    def test_sync_writes_the_blocks_and_the_version(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        write(root, "AGENTS.md", (FIXTURES / "agents-md" / "waypoint-before.md").read_text(encoding="utf-8"))
        (root / ".fleet/version").unlink()
        code, out, err = run("--to", VERSION, "--root", str(root))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("AGENTS.md: inserted working-in-a-container under \"## Working in a container\"\n", out)
        self.assertIn(f".fleet/version: {VERSION}\n", out)
        self.assertEqual((root / "AGENTS.md").read_text(encoding="utf-8"),
                         (WAYPOINT / "AGENTS.md").read_text(encoding="utf-8"))
        code, out, _ = run("--to", VERSION, "--root", str(root))
        self.assertEqual((code, out), (0, f"fleet sync: {root.name} is at fleet {VERSION} already; nothing changed.\n"))

    def test_sync_without_an_agents_md_creates_one(self) -> None:
        root = temporary(self)
        self.assertEqual(run("--to", VERSION, "--root", str(root))[0], 0)
        self.assertEqual(len(agents_md.blocks((root / "AGENTS.md").read_text(encoding="utf-8"))), len(agents_md.ORDER))

    def test_another_version_is_refused(self) -> None:
        root = temporary(self)
        code, _, err = run("--to", "v99.0.0", "--root", str(root))
        self.assertEqual(code, 2)
        self.assertIn(f"this is fleet {VERSION}; run `fleet sync --to v99.0.0` from fleet at v99.0.0", err)
        self.assertFalse((root / "AGENTS.md").exists())

    def test_a_marker_error_exits_2_and_writes_nothing(self) -> None:
        root = temporary(self)
        write(root, "AGENTS.md", "<!-- fleet:begin mistakes v0.0.1 -->\n")
        code, _, err = run("--to", VERSION, "--root", str(root))
        self.assertEqual(code, 2)
        self.assertIn("never closed", err)
        self.assertFalse((root / ".fleet").exists())
