import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

from fleet_tools import config

HELP = "fail on comments and docstrings in Python, CSS and Svelte (opt-in: only where [tool.fleet.no-comments] exists)"

DEFAULT_SKIP = (".semgrep/examples/", "node_modules/")
SUFFIXES = (".py", ".css", ".svelte")
PY_DIRECTIVE = re.compile(r"#\s*(noqa|type:|pragma|nosemgrep|nosec|fmt:|isort|ruff:|pyright|mypy|pylint|-\*-|coding[:=])")
CSS_COMMENT = re.compile(r"/\*(?!!|\s*stylelint-)[\s\S]*?\*/")
STYLE_BLOCK = re.compile(r"<style\b[^>]*>([\s\S]*?)</style>")
CODE_BLOCK = re.compile(r"<(script|style)\b[^>]*>[\s\S]*?</\1>")
HTML_COMMENT = re.compile(r"<!--(?!\s*(eslint-|svelte-ignore|prettier-ignore))[\s\S]*?-->")


def python_comments(source: str) -> list[int]:
    found = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type != tokenize.COMMENT:
            continue
        if tok.start[0] == 1 and tok.string.startswith("#!"):
            continue
        if PY_DIRECTIVE.match(tok.string):
            continue
        found.append(tok.start[0])
    return found


def python_docstrings(source: str) -> list[int]:
    return sorted(node.lineno for node in ast.walk(ast.parse(source))
                  if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))


def css_comments(source: str) -> list[int]:
    return [source.count("\n", 0, m.start()) + 1 for m in CSS_COMMENT.finditer(source)]


def svelte_style_comments(source: str) -> list[int]:
    found = []
    for block in STYLE_BLOCK.finditer(source):
        offset = source.count("\n", 0, block.start(1))
        found += [offset + line for line in css_comments(block.group(1))]
    return found


def svelte_template_comments(source: str) -> list[int]:
    blanked = CODE_BLOCK.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), source)
    return [blanked.count("\n", 0, m.start()) + 1 for m in HTML_COMMENT.finditer(blanked)]


def problems(path: str, source: str) -> list[str]:
    if path.endswith(".py"):
        lines = python_comments(source) + python_docstrings(source)
    elif path.endswith(".css"):
        lines = css_comments(source)
    elif path.endswith(".svelte"):
        lines = svelte_style_comments(source) + svelte_template_comments(source)
    else:
        return []
    return [f"{path}:{n}: a comment or docstring: say it in the code's names, or in the commit message or docs" for n in lines]


def tracked_files(root: Path, paths: list[str], skip: tuple[str, ...]) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z", "--", *paths], cwd=root, capture_output=True, text=True,
                         check=True).stdout
    return [p for p in out.split("\0") if p and not any(s in p for s in skip)]


def check(root: Path, paths: list[str], skip: tuple[str, ...] = DEFAULT_SKIP) -> list[str]:
    found: list[str] = []
    for path in tracked_files(root, paths, skip):
        if path.endswith(SUFFIXES) and (root / path).is_file():
            found += problems(path, (root / path).read_text(encoding="utf-8"))
    return found


def settings(conf: config.Config) -> tuple[list[str], tuple[str, ...]]:
    section = conf.section("no-comments")
    paths, skip = section.get("paths", []), section.get("skip", list(DEFAULT_SKIP))
    for key, value in (("paths", paths), ("skip", skip)):
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise config.ConfigError(f"[tool.fleet.no-comments] {key} must be a list of strings")
    return paths, tuple(skip)


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, help="the consumer's checkout (default: the nearest pyproject.toml up from here)")


def run(args: argparse.Namespace) -> int:
    try:
        root = (args.root or config.find_root(Path.cwd())).resolve()
        conf = config.load(root)
        if not conf.has("no-comments"):
            print("fleet no-comments: no [tool.fleet.no-comments] in pyproject.toml, so nothing to check")
            return 0
        paths, skip = settings(conf)
    except config.ConfigError as error:
        print(f"fleet no-comments: {error}", file=sys.stderr)
        return 2
    found = check(root, paths, skip)
    for line in found:
        print(line, file=sys.stderr)
    if not found:
        print(f"fleet no-comments: no comments or docstrings in {', '.join(paths) if paths else 'the tracked files'}")
    return 1 if found else 0
