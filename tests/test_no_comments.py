import contextlib
import io
import unittest

from consumers import RUNWAY, WAYPOINT, commit, copy, write

from fleet_tools import cli, no_comments

WHY = "a comment or docstring: say it in the code's names, or in the commit message or docs"


def run(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["no-comments", *args])
    return code, out.getvalue(), err.getvalue()


class PythonComments(unittest.TestCase):
    def test_a_comment_is_flagged_with_its_line(self) -> None:
        self.assertEqual(no_comments.problems("a.py", "x = 1\n# why\ny = 2  # also\n"), [f"a.py:2: {WHY}", f"a.py:3: {WHY}"])

    def test_directives_and_a_shebang_are_not_comments(self) -> None:
        source = "#!/usr/bin/env python3\nimport os  # noqa: F401\nx: int = y  # type: ignore[assignment]\n"
        self.assertEqual(no_comments.problems("a.py", source), [])

    def test_a_docstring_or_a_bare_string_is_flagged(self) -> None:
        source = 'def f():\n    "says what it does"\n    return 1\n\n\nclass C:\n    x = 1\n    """about x"""\n'
        self.assertEqual([p.split(" ")[0] for p in no_comments.problems("a.py", source)], ["a.py:2:", "a.py:8:"])

    def test_a_string_that_is_used_is_not_a_docstring(self) -> None:
        self.assertEqual(no_comments.problems("a.py", 'x = "text"\nprint("text")\n'), [])

    def test_a_hash_inside_a_string_is_not_a_comment(self) -> None:
        self.assertEqual(no_comments.problems("a.py", 'x = "# not a comment"\n'), [])

    def test_other_files_are_not_read(self) -> None:
        self.assertEqual(no_comments.problems("a.ts", "// a note\n"), [])


class CssComments(unittest.TestCase):
    def test_a_css_comment_is_flagged(self) -> None:
        self.assertEqual(len(no_comments.problems("a.css", "a { color: red; }\n/* why */\n")), 1)

    def test_a_svelte_style_block_is_checked_and_its_script_is_not(self) -> None:
        source = "<script>\n  // eslint's job\n</script>\n<style>\n  a { /* why */ color: red; }\n</style>\n"
        self.assertEqual([p.split(" ")[0] for p in no_comments.problems("A.svelte", source)], ["A.svelte:5:"])

    def test_a_svelte_template_comment_is_flagged_and_a_directive_in_it_is_not(self) -> None:
        source = ("<script>\n  const html = '<!-- not a comment -->';\n</script>\n<p>hi</p>\n<!-- a note -->\n"
                  "<!-- svelte-ignore a11y_x -->\n")
        self.assertEqual([p.split(" ")[0] for p in no_comments.problems("A.svelte", source)], ["A.svelte:5:"])

    def test_a_banner_that_a_minifier_keeps_is_not_a_comment(self) -> None:
        self.assertEqual(no_comments.problems("a.css", "/*! font license */\na { color: red; }\n"), [])


class FixtureConsumers(unittest.TestCase):
    def test_waypoint_s_tracked_files_carry_none(self) -> None:
        root = copy(self, WAYPOINT)
        code, out, _ = run("--root", str(root))
        self.assertEqual(code, 0)
        self.assertIn("no comments or docstrings in waypoint, tests, tools, frontend/src", out)

    def test_a_comment_fires_in_waypoint_only_under_its_paths(self) -> None:
        root = copy(self, WAYPOINT)
        write(root, "waypoint/domain/trips.py", "x = 1  # why\n")
        write(root, "docs/notes.py", "y = 2  # outside the configured paths\n")
        write(root, "frontend/src/node_modules/lib.py", "z = 3  # vendored\n")
        commit(root, "feat: trips")
        code, _, err = run("--root", str(root))
        self.assertEqual(code, 1)
        self.assertEqual(err, f"waypoint/domain/trips.py:1: {WHY}\n")

    def test_an_untracked_file_is_not_read(self) -> None:
        root = copy(self, WAYPOINT)
        write(root, "waypoint/scratch.py", "# not committed\n")
        self.assertEqual(run("--root", str(root))[0], 0)

    def test_skip_replaces_the_default(self) -> None:
        root = copy(self, WAYPOINT)
        pyproject = root / "pyproject.toml"
        pyproject.write_text(pyproject.read_text().replace('"frontend/src"]\n', '"frontend/src"]\nskip = ["generated/"]\n'))
        write(root, "waypoint/generated/api.py", "# made by a generator\n")
        commit(root, "chore: generated")
        self.assertEqual(run("--root", str(root))[0], 0)

    def test_runway_is_left_alone_because_it_doesn_t_opt_in(self) -> None:
        root = copy(self, RUNWAY)
        write(root, "runway/budget.py", "x = 1  # Runway's convention allows this\n")
        commit(root, "feat: budget")
        code, out, _ = run("--root", str(root))
        self.assertEqual(code, 0)
        self.assertIn("no [tool.fleet.no-comments]", out)

    def test_a_bad_setting_is_a_config_error(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        pyproject = root / "pyproject.toml"
        pyproject.write_text(pyproject.read_text().replace('paths = ["waypoint", "tests", "tools", "frontend/src"]',
                                                           'paths = "waypoint"'))
        code, _, err = run("--root", str(root))
        self.assertEqual(code, 2)
        self.assertIn("[tool.fleet.no-comments] paths must be a list of strings", err)


if __name__ == "__main__":
    unittest.main()
