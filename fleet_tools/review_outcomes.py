import argparse
import base64
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

try:
    from fleet_tools import agent_review as ar
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from fleet_tools import agent_review as ar

HELP = "a pull request's review findings and what became of each: open, resolved by a push, or dismissed by the owner"

Json = dict[str, Any]

MAX_ENTRIES = 200
MAX_OUTCOMES_LINE = 16384
MIN_REASON = 10
MAX_REASON = 300
STATUSES = ("open", "resolved")
DISMISSERS = ("OWNER",)
DISMISS = re.compile(r"^/fleet dismiss f:([0-9a-f]{8})(?:\s+(.*))?$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LINK = re.compile(r"^https://github\.com/[A-Za-z0-9_./#%?=&-]+$")
COMMENTS_JQ = "{body: .body, user: .user.login, association: .author_association, url: .html_url} | @json"


def review_for(marker: str) -> ar.Review:
    for review in ar.REVIEWS.values():
        if review.marker == marker:
            return review
    raise ValueError(f"no review has the marker {marker!r}")


def decode_record(encoded: str) -> list[Json] | None:
    try:
        value = json.loads(base64.b64decode(encoded, validate=True))
    except (ValueError, TypeError):
        return None
    if not isinstance(value, list):
        return None
    entries = []
    for item in value:
        if not isinstance(item, dict) or not ar.FINDING_ID.match(str(item.get("id", ""))) or item.get("status") not in STATUSES:
            continue
        if not ar.SHA.match(str(item.get("first", ""))):
            continue
        entry = {"id": item["id"], "status": item["status"], "kind": ar.defuse(item.get("kind"), 120),
                 "file": ar.defuse(item.get("file"), 300), "title": ar.defuse(item.get("title"), 200), "first": item["first"]}
        if item["status"] == "resolved" and ar.SHA.match(str(item.get("resolved", ""))):
            entry["resolved"] = item["resolved"]
        entries.append(entry)
    return entries


def record_of(body: str, review: ar.Review = ar.FIRST) -> list[Json] | None:
    if not body.startswith(review.marker):
        return None
    lines = body.splitlines()
    if len(lines) < ar.HIDDEN_LINES:
        return None
    found = review.outcomes.match(lines[ar.HIDDEN_LINES - 1])
    return decode_record(found.group(1)) if found else None


def encode(entries: Sequence[Json], review: ar.Review) -> str:
    rows = [{k: v for k, v in e.items() if k in ("id", "status", "kind", "file", "title", "first", "resolved")} for e in entries]
    return f"<!-- {review.key}-outcomes {base64.b64encode(json.dumps(rows, ensure_ascii=False).encode()).decode()} -->"


def cap(entries: Iterable[Json], review: ar.Review = ar.FIRST) -> list[Json]:
    kept = list(entries)
    while kept and (len(kept) > MAX_ENTRIES or len(encode(kept, review).encode()) > MAX_OUTCOMES_LINE):
        kept.pop(next((i for i, e in enumerate(kept) if e["status"] == "resolved"), 0))
    return kept


def outcomes_line(entries: Iterable[Json], review: ar.Review = ar.FIRST) -> str:
    return encode(cap(entries, review), review)


def merge(previous: Sequence[Json], findings: Sequence[Json], sha: str, full: bool, touched: Iterable[str] = ()) -> list[Json]:
    touched = set(touched)
    current = {f["id"]: f for f in findings if ar.FINDING_ID.match(str(f.get("id", "")))}
    merged: list[Json] = []
    for entry in previous:
        if entry["id"] in current:
            seen = current.pop(entry["id"])
            merged.append({**{k: v for k, v in entry.items() if k != "resolved"}, "status": "open",
                           "kind": ar.defuse(seen.get("kind"), 120), "title": ar.defuse(seen.get("title"), 200)})
        elif entry["status"] == "open" and (full or entry["file"] in touched):
            merged.append({**entry, "status": "resolved", "resolved": sha})
        else:
            merged.append(dict(entry))
    for found in current.values():
        merged.append({"id": found["id"], "status": "open", "kind": ar.defuse(found.get("kind"), 120),
                       "file": ar.defuse(found.get("file"), 300), "title": ar.defuse(found.get("title"), 200), "first": sha})
    return merged


def dismissals(comments: Iterable[Json]) -> dict[str, Json]:
    found: dict[str, Json] = {}
    for comment in comments:
        if comment.get("association") not in DISMISSERS or not isinstance(comment.get("body"), str):
            continue
        first_line = comment["body"].splitlines()[0].strip() if comment["body"].strip() else ""
        match = DISMISS.match(first_line)
        if not match:
            continue
        reason = (match.group(2) or "").strip()
        if len(reason) < MIN_REASON:
            continue
        url = str(comment.get("url") or "")
        found[match.group(1)] = {"reason": ar.defuse(reason, MAX_REASON), "url": url if LINK.match(url) else ""}
    return found


def apply(entries: Iterable[Json], dismissed: dict[str, Json]) -> list[Json]:
    out = []
    for entry in entries:
        if entry["id"] in dismissed:
            out.append({**entry, "status": "dismissed", **dismissed[entry["id"]]})
        else:
            out.append(dict(entry))
    return out


def bot_comment(comments: Iterable[Json], review: ar.Review) -> str:
    for comment in comments:
        body = comment.get("body")
        if comment.get("user") == ar.BOT and isinstance(body, str) and body.startswith(review.marker):
            return body
    return ""


def fetch_comments(repo: str, pr: int) -> list[Json]:
    if not REPO.match(repo):
        raise ValueError(f"not a repository: {repo!r}")
    done = subprocess.run(["gh", "api", "--paginate", f"repos/{repo}/issues/{int(pr)}/comments?per_page=100", "--jq", COMMENTS_JQ],
                          check=True, capture_output=True, text=True)
    return read_comments(done.stdout)


def read_comments(text: str) -> list[Json]:
    return [c for c in (json.loads(line) for line in text.splitlines() if line.strip()) if isinstance(c, dict)]


def load(repo: str, pr: int, marker: str = ar.MARKER, comments: Sequence[Json] | None = None) -> list[Json]:
    review = review_for(marker)
    comments = fetch_comments(repo, pr) if comments is None else comments
    return apply(record_of(bot_comment(comments, review), review) or [], dismissals(comments))


def section(shown: Sequence[Json]) -> list[str]:
    dismissed = [e for e in shown if e["status"] == "dismissed"]
    resolved = [e for e in shown if e["status"] == "resolved"]
    if not dismissed and not resolved:
        return []
    lines = [ar.OUTCOMES_HEADING, ""]
    for e in dismissed:
        where = f" ([comment]({e['url']}))" if e.get("url") else ""
        lines.append(f"- `f:{e['id']}` **{ar.defuse(e['title'], 200)}**: dismissed by the owner, \"{ar.defuse(e['reason'], MAX_REASON)}\"{where}.")
    for e in resolved:
        lines.append(f"- `f:{e['id']}` **{ar.defuse(e['title'], 200)}**: resolved at `{str(e.get('resolved', ''))[:7]}`.")
    if dismissed:
        lines += ["", "<sub>A dismissal is a record for improving the reviewer: it doesn't pass the review or change the merge gate.</sub>"]
    return lines


def finish(comment: str, comments: Sequence[Json], sha: str, full: bool, touched: Iterable[str] = (),
           review: ar.Review = ar.FIRST) -> str:
    if not comment.startswith(review.marker) or not ar.SHA.match(sha):
        return comment
    _, _, findings, end = ar.hidden_lines(comment, review)
    lines = comment.splitlines()
    if findings is None or end != ar.HIDDEN_LINES - 1:
        return comment
    merged = cap(merge(record_of(bot_comment(comments, review), review) or [], findings, sha, full, touched), review)
    body = lines[end:]
    extra = section(apply(merged, dismissals(comments)))
    if extra:
        footer = next((i for i, line in enumerate(body) if line.startswith(ar.FOOTER)), len(body))
        body = [*body[:footer], *extra, "", *body[footer:]]
    hidden = "\n".join([*lines[:end], encode(merged, review)])
    text = "\n".join(body)
    room = ar.MAX_COMMENT - len(hidden) - 1
    if len(text) > room:
        text = text[:max(room - 14, 0)] + "\n\n(cut short)"
    return hidden + "\n" + text


def describe(outcomes: Sequence[Json]) -> str:
    if not outcomes:
        return "No findings recorded.\n"
    lines = []
    for e in outcomes:
        line = f"f:{e['id']}  {e['status']:<9}  {e['kind']}  {e['file'] or '-'}  {e['title']}"
        if e["status"] == "dismissed":
            line += f"  ({e['reason']})"
        lines.append(line)
    return "".join(line + "\n" for line in lines)


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True, help="OWNER/NAME")
    parser.add_argument("--pr", required=True, type=int, help="the pull request's number")
    parser.add_argument("--json", action="store_true", help="print the merged outcomes as JSON")
    parser.add_argument("--second", action="store_true", help="the second review's record instead of the first's")
    parser.add_argument("--comments", metavar="FILE", help="read the comments from this JSON-lines file instead of GitHub")
    parser.add_argument("--finish", metavar="COMMENT.md", help="agent-review.yml only: merge this review's comment and write it back")
    parser.add_argument("--sha", default="", help="with --finish: the commit this review read")
    parser.add_argument("--incremental", default="false", help="with --finish: true when the review read only the diff since the last one")
    parser.add_argument("--touched", metavar="FILE", help="with --finish: the files that diff touched, one per line")


def run(args: argparse.Namespace) -> int:
    review = ar.SECOND if args.second else ar.FIRST
    try:
        comments = read_comments(Path(args.comments).read_text(encoding="utf-8")) if args.comments else fetch_comments(args.repo, args.pr)
        if args.finish:
            path = Path(args.finish)
            touched = Path(args.touched).read_text(encoding="utf-8").splitlines() if args.touched else []
            path.write_text(finish(path.read_text(encoding="utf-8"), comments, args.sha, args.incremental != "true",
                                   [t for t in touched if t], review), encoding="utf-8")
            return 0
        outcomes = load(args.repo, args.pr, review.marker, comments)
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        if args.finish:
            print(f"::warning::The finding outcomes weren't recorded this time ({type(e).__name__}); the comment is posted as it is.")
            return 0
        print(f"review-outcomes: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(outcomes, indent=2) + "\n" if args.json else describe(outcomes))
    return 0


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="review_outcomes.py", description=HELP)
    configure(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
