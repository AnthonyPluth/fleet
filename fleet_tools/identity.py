import importlib.metadata
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMMIT = re.compile(r"[0-9a-f]{40}")
TAG = re.compile(r"v\d+\.\d+\.\d+")
DISTRIBUTION = "fleet"
PACKAGE = "fleet_tools"


def git(root: Path, *args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True, timeout=60).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None


def is_checkout(root: Path) -> bool:
    return (root / ".git").exists()


def checkout_commit(root: Path) -> str | None:
    found = git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    return found if found and COMMIT.fullmatch(found) else None


def installed_commit(root: Path, distribution: str = DISTRIBUTION) -> str | None:
    try:
        found = importlib.metadata.distribution(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None
    if Path(str(found.locate_file(PACKAGE))).resolve() != (root / PACKAGE).resolve():
        return None
    try:
        data = json.loads(found.read_text("direct_url.json") or "null")
    except ValueError:
        return None
    vcs = data.get("vcs_info") if isinstance(data, dict) else None
    commit = vcs.get("commit_id") if isinstance(vcs, dict) and vcs.get("vcs") == "git" else None
    return commit if isinstance(commit, str) and COMMIT.fullmatch(commit) else None


def commit(root: Path | None = None) -> str | None:
    root = root or ROOT
    return checkout_commit(root) if is_checkout(root) else installed_commit(root)


def tag(root: Path | None = None) -> str | None:
    root = root or ROOT
    if not is_checkout(root):
        return None
    found = git(root, "describe", "--exact-match", "--tags", "--match", "v[0-9]*", "HEAD")
    return found if found and TAG.fullmatch(found) else None


def describe(root: Path | None = None) -> str:
    root = root or ROOT
    found = commit(root)
    if found is None:
        return ("fleet at an unknown commit (neither a git checkout nor installed from git with a commit in its "
                "direct_url.json)")
    named = tag(root)
    return f"fleet {named} (commit {found})" if named else f"fleet at commit {found}"
