import argparse
import json
import re
import subprocess
import sys
from typing import Any

TITLE = "Deep security scan due"
LABELS = ["manual", "security"]
STAMP = re.compile(r"CLAUDE-SECURITY-REVISION-([0-9a-f]{12})(?:-dirty)?\.json")
FULL_SHA = re.compile(r"(?<![0-9a-zA-Z])[0-9a-f]{40}(?![0-9a-zA-Z])")


def gh(*args: str, stdin: str | None = None) -> Any:
    result = subprocess.run(["gh", "api", *args], capture_output=True, text=True, input=stdin, check=True)
    decoder = json.JSONDecoder()
    text = result.stdout.strip()
    pages: list[Any] = []
    index = 0
    while index < len(text):
        page, end = decoder.raw_decode(text, index)
        pages.append(page)
        index = end
        while index < len(text) and text[index].isspace():
            index += 1
    if len(pages) == 1:
        return pages[0]
    joined: list[Any] = []
    for page in pages:
        joined.extend(page)
    return joined


def issues(repo: str, state: str) -> list[dict[str, Any]]:
    listed = gh("--paginate", f"repos/{repo}/issues?state={state}&per_page=100")
    return [issue for issue in listed if "pull_request" not in issue and issue.get("title") == TITLE]


def recorded_revision(repo: str, issue: dict[str, Any]) -> str | None:
    texts = [issue.get("body") or ""]
    comments = gh("--paginate", f"repos/{repo}/issues/{issue['number']}/comments?per_page=100")
    texts.extend(comment.get("body") or "" for comment in comments)
    text = "\n".join(texts)
    stamp = STAMP.search(text)
    if stamp:
        return stamp.group(1)
    sha = FULL_SHA.search(text)
    return sha.group(0) if sha else None


def commits_since(repo: str, revision: str, branch: str) -> int | None:
    try:
        comparison = gh(f"repos/{repo}/compare/{revision}...{branch}")
    except subprocess.CalledProcessError:
        return None
    ahead = comparison.get("ahead_by")
    return ahead if isinstance(ahead, int) else None


def body(fleet_ref: str, branch: str, last: dict[str, Any] | None, revision: str | None, since: int | None) -> str:
    runbook = f"https://github.com/AnthonyPluth/fleet/blob/{fleet_ref}/docs/deep-scan.md"
    lines = [
        f"A deep security scan with the claude-security plugin is due. The runbook is fleet's [docs/deep-scan.md]({runbook}).",
        "",
    ]
    if last is None:
        lines.append("No earlier scan is recorded: no closed issue with this title exists.")
    elif revision is None:
        lines.append(f"The last scan's issue, #{last['number']}, names no revision stamp, so the commits since it can't be counted.")
    else:
        count = "an unknown number of" if since is None else str(since)
        lines.append(f"The last scan (#{last['number']}) covered revision `{revision}`; `{branch}` has {count} commits since.")
    lines.extend([
        "",
        "When the scan is done, record here the revision stamp's file name (`CLAUDE-SECURITY-REVISION-<sha12>.json`), "
        "the effort and the counts by severity, nothing else from the report, then close this issue. "
        "The next reminder reads the revision from it.",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f'open one "{TITLE}" issue unless one is open')
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--fleet-ref", required=True, help="the fleet ref the runbook link points at")
    parser.add_argument("--dry-run", action="store_true", help="print the issue instead of opening it")
    args = parser.parse_args(argv)
    open_issues = issues(args.repo, "open")
    if open_issues:
        numbers = ", ".join(f"#{issue['number']}" for issue in open_issues)
        print(f"deep-scan-due: {numbers} is open; nothing to do")
        return 0
    closed = sorted(issues(args.repo, "closed"), key=lambda issue: issue.get("closed_at") or "", reverse=True)
    last = closed[0] if closed else None
    revision = recorded_revision(args.repo, last) if last else None
    branch = gh(f"repos/{args.repo}")["default_branch"]
    since = commits_since(args.repo, revision, branch) if revision else None
    text = body(args.fleet_ref, branch, last, revision, since)
    if args.dry_run:
        print(f"deep-scan-due: would open {TITLE!r} with labels {', '.join(LABELS)}:\n\n{text}")
        return 0
    payload = json.dumps({"title": TITLE, "body": text, "labels": LABELS})
    created = gh("-X", "POST", f"repos/{args.repo}/issues", "--input", "-", stdin=payload)
    print(f"deep-scan-due: opened #{created['number']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
