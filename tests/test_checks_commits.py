import unittest

from consumers import (
    AGENT,
    RUNWAY,
    WAYPOINT,
    checks,
    commit,
    copy,
    empty_consumer,
    ran,
    write,
)

from fleet_tools.checks import commits

SESSION = "Claude-Session: https://claude.ai/code/session_x"


class Commits(unittest.TestCase):
    def test_a_person_s_commit_needs_nothing(self) -> None:
        self.assertEqual(commits.check_commit("a" * 40, "fix: something\n\nWhy it changed."), [])

    def test_an_agent_s_commit_naming_its_model_passes(self) -> None:
        for model in ("Opus 4.5", "Sonnet 4", "Haiku 4.5", "Fable 5.1"):
            msg = f"fix: x\n\nCo-Authored-By: Claude {model} <noreply@anthropic.com>\n{SESSION}\n"
            self.assertEqual(commits.check_commit("a" * 40, msg), [], model)

    def test_an_agent_s_commit_without_the_trailer_fails(self) -> None:
        problems = commits.check_commit("a" * 40, f"fix: x\n\n{SESSION}\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("needs a 'Co-Authored-By: Claude", problems[0])

    def test_a_trailer_that_doesnt_name_the_model_fails(self) -> None:
        for trailer in ("Claude <noreply@anthropic.com>", "Claude Opus <noreply@anthropic.com>",
                        "Claude Opus 4.5 <claude@example.com>", "claude-opus-4-5 <noreply@anthropic.com>",
                        "Claude Gemini 2 <noreply@anthropic.com>"):
            problems = commits.check_commit("a" * 40, f"fix: x\n\nCo-authored-by: {trailer}\n")
            self.assertEqual(len(problems), 1, trailer)
            self.assertIn("should name the model", problems[0])

    def test_two_claude_trailers_fail(self) -> None:
        msg = ("fix: x\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n"
               "Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>\n")
        self.assertIn("more than one", commits.check_commit("a" * 40, msg)[0])

    def test_another_person_as_co_author_is_left_alone(self) -> None:
        self.assertEqual(commits.check_commit("a" * 40, "fix: x\n\nCo-Authored-By: Pat Doe <pat@example.com>\n"), [])


class FixtureConsumers(unittest.TestCase):
    def test_an_agent_s_commit_without_its_model_fires_in_each_consumer(self) -> None:
        for fixture in (WAYPOINT, RUNWAY):
            with self.subTest(fixture=fixture.name):
                root = copy(self, fixture)
                write(root, "README.md", "x\n")
                commit(root, "docs: a readme\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n" + SESSION)
                write(root, "README.md", "y\n")
                commit(root, "docs: again\n\n" + AGENT)
                problems = checks(root, "HEAD~2..HEAD")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("(docs: a readme): 'Co-Authored-By: Claude <noreply@anthropic.com>' should name", problems[0])

    def test_the_log_is_read_from_the_range(self) -> None:
        root = copy(self, RUNWAY)
        first = commit(root, "fix: one\n\n" + AGENT)
        second = commit(root, "fix: two\n\nbody\n\n" + SESSION)
        self.assertEqual([sha for sha, _ in commits.commits(root, "HEAD~2..HEAD")], [second, first])
        self.assertIn("(fix: two): an agent's commit", checks(root, "HEAD~1")[0])

    def test_without_commits_nothing_is_read(self) -> None:
        root = empty_consumer(self)
        commit(root, "fix: x\n\n" + SESSION)
        self.assertNotIn("commits", ran(root))
        self.assertIn("commits", ran(root, "HEAD~1..HEAD"))
        self.assertEqual(len(checks(root, "HEAD~1..HEAD")), 1)


if __name__ == "__main__":
    unittest.main()
