import re
from pathlib import Path

from fleet_tools.checks.context import Check, Context, git

MODEL_FAMILIES = ("Opus", "Sonnet", "Haiku", "Fable")
CO_AUTHOR = re.compile(r"^co-authored-by:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
SESSION = re.compile(r"^claude-session:\s*\S", re.IGNORECASE | re.MULTILINE)
GOOD_CO_AUTHOR = re.compile(r"^Claude (" + "|".join(MODEL_FAMILIES) + r") \d+(\.\d+)* <noreply@anthropic\.com>$")
CLAUDE = re.compile(r"\bclaude\b|anthropic\.com", re.IGNORECASE)


def check_commit(sha: str, message: str) -> list[str]:
    co_authors = [c.strip() for c in CO_AUTHOR.findall(message)]
    claude = [c for c in co_authors if CLAUDE.search(c)]
    if not SESSION.search(message) and not claude:
        return []
    subject = message.strip().splitlines()[0] if message.strip() else ""
    where = f"commit {sha[:10]} ({subject[:60]})"
    if not claude:
        return [f"{where}: an agent's commit (it has a Claude-Session trailer) needs a "
                "'Co-Authored-By: Claude <Model> <version> <noreply@anthropic.com>' trailer for the model that wrote it"]
    if len(claude) > 1:
        return [f"{where}: more than one Claude Co-Authored-By trailer; keep the one for the model that wrote it"]
    if not GOOD_CO_AUTHOR.match(claude[0]):
        return [f"{where}: 'Co-Authored-By: {claude[0]}' should name the model, as "
                f"'Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>' (one of {', '.join(MODEL_FAMILIES)})"]
    return []


def commits(root: Path, revision_range: str) -> list[tuple[str, str]]:
    out = git(root, "log", "--format=%H%x00%B%x1e", revision_range, "--")
    found = []
    for record in out.split("\x1e"):
        if record.strip():
            sha, _, message = record.strip("\n").partition("\0")
            found.append((sha, message))
    return found


def applies(context: Context) -> bool:
    return context.commits is not None


def run(context: Context) -> list[str]:
    assert context.commits is not None
    return [problem for sha, message in context.commits.log for problem in check_commit(sha, message)]


CHECK = Check("commits", "commit trailers", applies, run)
