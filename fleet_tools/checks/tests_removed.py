import re
import subprocess
from pathlib import Path

from fleet_tools.checks.context import Check, Context, git, show

DEFAULT_TEST_PATHS = ("tests/", "frontend/")
PY_TEST = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)\s*\(", re.MULTILINE)
PY_CLASS = re.compile(r"^class\s+(\w+)", re.MULTILINE)
TS_TEST = re.compile(r"""\b(?:it|test)\(\s*(["'`])((?:(?!\1).)+)\1""")
TS_FILE = re.compile(r"\.test\.(ts|mjs|js)$")
SKIP = re.compile(r"\b(?:unittest\.skip(?:If|Unless)?\b|skipTest\(|(?:it|test|describe)\.(?:skip|only|todo)\b|xit\(|xdescribe\()")
REMOVES = re.compile(r"^removes-test:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
SKIPS = re.compile(r"^skips-test:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
STRING = re.compile(r"""(["'`])(?:\\.|(?!\1).)*\1""")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")


def prefixes(paths: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(path.rstrip("/") + "/" for path in paths)


def is_test_file(path: str, test_paths: tuple[str, ...] = DEFAULT_TEST_PATHS) -> bool:
    if not path.startswith(prefixes(test_paths)):
        return False
    if path.endswith(".py"):
        return Path(path).name.startswith("test_")
    return TS_FILE.search(path) is not None


def test_names(path: str, text: str) -> set[str]:
    if path.endswith(".py"):
        names = set()
        cls = ""
        for line in text.splitlines():
            if found := PY_CLASS.match(line):
                cls = found.group(1)
            elif found := PY_TEST.match(line):
                names.add(f"{path}::{cls + '.' if cls and line.startswith(' ') else ''}{found.group(1)}")
        return names
    return {f"{path}::{found.group(2)}" for found in TS_TEST.finditer(text)}


def removed_tests(root: Path, base: str, head: str = "HEAD",
                  test_paths: tuple[str, ...] = DEFAULT_TEST_PATHS) -> list[str]:
    gone: list[str] = []
    listed = git(root, "ls-tree", "-r", "--name-only", base, "--", *(p.rstrip("/") for p in test_paths))
    for path in listed.splitlines():
        if not is_test_file(path, test_paths):
            continue
        before = test_names(path, show(root, base, path) or "")
        text = show(root, head, path)
        now = test_names(path, text) if text is not None else set()
        gone += sorted(before - now)
    return gone


def skipped_name(path: str, lines: list[str], index: int) -> str:
    if path.endswith(".py"):
        here = lines[index].lstrip() if index < len(lines) else ""
        steps = range(index + 1, len(lines)) if here.startswith("@") else range(min(index, len(lines) - 1), -1, -1)
        for k in steps:
            if found := PY_DEF.match(lines[k]):
                return found.group(1)
        return ""
    if index >= len(lines):
        return ""
    found = TS_TEST.search(lines[index].replace(".skip(", "(").replace(".only(", "(").replace(".todo(", "("))
    return found.group(2) if found else ""


def added_skips(root: Path, base: str, head: str = "HEAD",
                test_paths: tuple[str, ...] = DEFAULT_TEST_PATHS) -> list[tuple[str, str, str]]:
    out = []
    diff = git(root, "diff", "--relative", "--unified=0", base, head, "--", *(p.rstrip("/") for p in test_paths))
    path, line_no = "", 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif found := HUNK.match(line):
            line_no = int(found.group(1))
        elif line.startswith("+") and not line.startswith("+++"):
            if path and is_test_file(path, test_paths) and SKIP.search(STRING.sub("", line)):
                lines = (show(root, head, path) or "").splitlines()
                out.append((path, skipped_name(path, lines, line_no - 1), line[1:].strip()[:100]))
            line_no += 1
    return out


def _names(declared: list[str]) -> set[str]:
    out = set()
    for d in (d.strip() for d in declared):
        if d:
            parts = re.split(r"\s+[\u2014\u2013]\s+", d, maxsplit=1)
            out.add(parts[0].strip() if len(parts) == 2 else d.split()[0])
    return out


def check_tests(removed: list[str], skipped: list[tuple[str, str, str]], messages: list[str]) -> list[str]:
    removals = _names([r for m in messages for r in REMOVES.findall(m)])
    skips = _names([r for m in messages for r in SKIPS.findall(m)])
    problems = []
    for name in removed:
        short = name.split("::", 1)[1]
        if not {name, short, short.rsplit(".", 1)[-1]} & removals:
            problems.append(f"the test {name} is gone: keep it, or say why on a commit with a "
                            f"'Removes-Test: {short} — <why>' trailer")
    for path, test, text in skipped:
        names = {f"{path}::{test}", test} if test else {path, Path(path).name}
        if not names & skips:
            problems.append(f"a test is skipped or narrowed ({path}: {text}): say why on a commit with a "
                            f"'Skips-Test: {test or Path(path).name} — <why>' trailer")
    return problems


def test_paths_of(context: Context) -> tuple[str, ...]:
    return tuple(context.config.strings("test-paths")) if context.config.has("test-paths") else DEFAULT_TEST_PATHS


def applies(context: Context) -> bool:
    return context.commits is not None and bool(test_paths_of(context))


def run(context: Context) -> list[str]:
    commits = context.commits
    assert commits is not None
    paths = test_paths_of(context)
    try:
        removed = removed_tests(context.root, commits.base, commits.head, paths)
        skipped = added_skips(context.root, commits.base, commits.head, paths)
    except subprocess.CalledProcessError as error:
        return [f"couldn't compare the tests with {commits.base}: {(error.stderr or '').strip()[:200]}"]
    return check_tests(removed, skipped, commits.messages)


CHECK = Check("tests-removed", "no test removed or skipped without a reason", applies, run)
