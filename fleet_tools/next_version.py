import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

HELP = ("the next release version from the commits since the last tag (feat: a minor, a ! or BREAKING CHANGE: a major, "
        "else a patch), written to $GITHUB_OUTPUT as version, new, newest and skip")

TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
TAG_GLOB = "v[0-9]*.[0-9]*.[0-9]*"
MAJOR = re.compile(r"(^|[ \t])#major[ \t]*$|^BREAKING CHANGE:|^[a-z]+(\(.*\))?!:", re.MULTILINE)
MINOR = re.compile(r"(^|[ \t])#minor[ \t]*$|^feat(\(.*\))?:", re.MULTILINE)


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def parse(tag: str) -> tuple[int, int, int] | None:
    found = TAG.match(tag)
    return (int(found.group(1)), int(found.group(2)), int(found.group(3))) if found else None


def highest(tags: list[str]) -> str | None:
    versions = [(parsed, tag) for tag in tags if (parsed := parse(tag)) is not None]
    return max(versions)[1] if versions else None


def bump(current: str, log: str) -> str:
    parsed = parse(current)
    assert parsed is not None
    major, minor, patch = parsed
    if MAJOR.search(log):
        return f"v{major + 1}.0.0"
    if MINOR.search(log):
        return f"v{major}.{minor + 1}.0"
    return f"v{major}.{minor}.{patch + 1}"


def unused(version: str, taken: set[str]) -> str:
    while version in taken:
        parsed = parse(version)
        assert parsed is not None
        version = f"v{parsed[0]}.{parsed[1]}.{parsed[2] + 1}"
    return version


def decide(root: Path, sha: str, remote: str = "origin", branch: str = "main") -> tuple[dict[str, str], list[str]]:
    git(root, "fetch", "--quiet", remote, branch)
    head = git(root, "rev-parse", "FETCH_HEAD").strip()
    newest = head == sha
    outputs = {"newest": "true" if newest else "false"}
    existing = highest(git(root, "tag", "--points-at", "HEAD", "--list", TAG_GLOB).split())
    if existing:
        outputs |= {"version": existing, "new": "false"}
        return outputs, [f"Already tagged {existing}"]
    if not newest:
        outputs["skip"] = "true"
        return outputs, [f"::notice::{sha} isn't the newest commit on {branch} any more, so it isn't released."]
    try:
        last = git(root, "describe", "--tags", "--abbrev=0", "--first-parent", "--match", TAG_GLOB, "HEAD").strip()
    except subprocess.CalledProcessError:
        last = ""
    tags = git(root, "tag", "--list", TAG_GLOB).split()
    top = highest(tags)
    if top is None:
        version = "v1.0.0"
    else:
        log = git(root, "log", "--format=%s%n%b", f"{last}..HEAD" if last else "HEAD")
        version = bump(top, log)
    version = unused(version, set(tags))
    outputs |= {"version": version, "new": "true"}
    return outputs, [f"Next version: {version} (last in this history: {last or 'none'}; highest: {top or 'none'})"]


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the repository (default: here)")
    parser.add_argument("--sha", help="the commit being released (default: $GITHUB_SHA, else HEAD)")
    parser.add_argument("--remote", default="origin", help="the remote to fetch the branch from (default: origin)")
    parser.add_argument("--branch", default="main", help="the release branch (default: main)")
    parser.add_argument("--output", type=Path, help="where to append key=value lines (default: $GITHUB_OUTPUT, else stdout)")


def run(args: argparse.Namespace) -> int:
    root: Path = args.root
    try:
        sha = args.sha or os.environ.get("GITHUB_SHA") or git(root, "rev-parse", "HEAD").strip()
        outputs, notes = decide(root, sha, args.remote, args.branch)
    except subprocess.CalledProcessError as error:
        print(f"fleet next-version: `{' '.join(map(str, error.cmd))}` failed: {(error.stderr or '').strip()[:300]}",
              file=sys.stderr)
        return 1
    lines = "".join(f"{key}={value}\n" for key, value in outputs.items())
    output = args.output or (Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None)
    if output is None:
        sys.stdout.write(lines)
    else:
        with output.open("a", encoding="utf-8") as file:
            file.write(lines)
    for note in notes:
        print(note)
    return 0
