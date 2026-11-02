import argparse
import base64
import binascii
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fleet_tools import (
    agents_md,
    config,
    review_outcomes,
    verification,
    verification_guides,
)
from fleet_tools import review_mine as rm
from fleet_tools.agent_review import (
    FIRST,
    NO_BLOCK,
    SECOND,
    SEVERITIES,
    SHA,
    Review,
    defuse,
)

HELP = ("loop health: per repository over the window, the reviews of merged pull requests and what became of their "
        "findings, verification records and uncovered paths, the report's rows and the issues opened from them, and the "
        "harness changes on the default branch, as JSON and Markdown")

Json = dict[str, Any]

SCHEMA = 1
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH = re.compile(r"^[^\x00-\x20\x7f~^:?*\[\\]{1,255}$")
CONTEXT = ".github/agent-review/context.md"
INVARIANTS = ".github/agent-review/invariants.md"
SKILLS = ".claude/skills"
FLEET_SKILLS = ".claude/skills/fleet-"
SEMGREP = ".semgrep"
AGENTS = "AGENTS.md"
VERSION_FILE = ".fleet/version"
SYNC_BRANCH = "fleet/sync"
UNKNOWN = "unknown"
PER_PAGE = 100
MAX_PAGES = 30
MAX_SKILL_COMMITS = 100
MAX_FILE_PAGES = 30
MAX_CONTENT = 1_000_000
MAX_SAMPLES = 20
MAX_PATH = 300
MAX_TITLE = 200
MAX_MISSING = 300


class Unreadable(Exception):
    pass


@dataclass
class Run:
    repo: str
    days: int
    now: datetime
    since: str
    until: str
    app_root: Path | None
    missing: list[str] = field(default_factory=list)

    def lack(self, what: str, error: BaseException) -> None:
        self.missing.append(f"{what}: {reason(error)}")


def reason(error: BaseException) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        text = str(error.stderr or "").strip().splitlines()
        detail = verification_guides.clean(text[-1] if text else "")[:MAX_MISSING]
        return f"gh api failed ({error.returncode}){': ' + detail if detail else ''}"
    return verification_guides.clean(str(error))[:MAX_MISSING] or type(error).__name__


def not_found(error: subprocess.CalledProcessError) -> bool:
    return "HTTP 404" in str(error.stderr or "")


def share(part: int, whole: int) -> float | None:
    return None if whole == 0 else round(part / whole, 4)


def cleaned(text: object, limit: int) -> str:
    return verification_guides.clean(str(text or "").replace("\r", " ").replace("\n", " "))[:limit]


def merged_pulls(run: Run) -> list[rm.Pull]:
    found: dict[int, rm.Pull] = {}
    for page in range(1, MAX_PAGES + 1):
        batch = rm.gh(f"repos/{run.repo}/pulls?state=all&sort=updated&direction=desc&per_page={PER_PAGE}&page={page}")
        if not isinstance(batch, list):
            raise Unreadable("the pull request list isn't a list")
        for pull in batch:
            if not isinstance(pull, dict) or not isinstance(pull.get("number"), int):
                continue
            merged_at, body = pull.get("merged_at"), pull.get("body")
            if isinstance(merged_at, str) and run.since <= merged_at <= run.until:
                found.setdefault(pull["number"], rm.Pull(pull["number"], True, body if isinstance(body, str) else "", merged_at))
        last = batch[-1] if batch else None
        if len(batch) < PER_PAGE or not isinstance(last, dict) or str(last.get("updated_at") or "") < run.since:
            return [found[n] for n in sorted(found)]
    raise Unreadable(f"over {MAX_PAGES * PER_PAGE} pull requests were updated in the window")


def bot_comments(comments: list[Json], review: Review) -> list[Json]:
    out = []
    for comment in comments:
        user, body = comment.get("user"), comment.get("body")
        if isinstance(user, dict) and user.get("login") == rm.BOT and isinstance(body, str) and body.startswith(review.marker):
            out.append(comment)
    return out


def raised_on(repo: str, number: int, comments: list[Json]) -> dict[str, list[tuple[str, str]]]:
    ready = rm.shaped(comments)
    found: dict[str, list[tuple[str, str]]] = {}
    for review in (FIRST, SECOND):
        posted = bot_comments(comments, review)
        if not posted:
            continue
        parsed = rm.findings_of(max(posted, key=rm.stamp))
        line = parsed[1] if parsed is not None and parsed[0] is review else []
        levels = {f["id"]: f["severity"] for f in line if f["id"]}
        entries = review_outcomes.load(repo, number, review.marker, ready)
        ids = {str(entry["id"]) for entry in entries}
        kept = [(levels.get(str(entry["id"]), UNKNOWN), str(entry["status"])) for entry in entries]
        kept += [(f["severity"], "open") for f in line if not f["id"] or f["id"] not in ids]
        found[review.key] = kept
    return found


def reviews(run: Run, pulls: list[rm.Pull]) -> Json:
    merged = {each.number: each.merged_at for each in pulls}
    per_pull, unread = rm.histories(run.repo, set(merged))
    reviewed: Counter[str] = Counter()
    severity: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    for number, comments in sorted(per_pull.items()):
        for key, kept in raised_on(run.repo, number, comments).items():
            reviewed[key] += 1
            severity.update(level for level, _ in kept)
            statuses.update(status for _, status in kept)
    if unread:
        run.missing.append(f"reviews: the comments of {unread} merged pull request(s) couldn't be read in full (an error, or over "
                           f"{rm.MAX_COMMENT_PAGES * rm.PER_PAGE} comments); their reviews and findings aren't counted")
    snapshot, indeterminate = rm.at_merge(per_pull, merged)
    at_merge = open_at_merge = 0
    for number, comments in sorted(snapshot.items()):
        for kept in raised_on(run.repo, number, comments).values():
            at_merge += len(kept)
            open_at_merge += sum(1 for _, status in kept if status == "open")
    raised = sum(statuses.values())
    findings = {"raised": raised, "by_severity": {**{level: severity[level] for level in SEVERITIES}, UNKNOWN: severity[UNKNOWN]},
                "resolved": statuses["resolved"], "dismissed": statuses["dismissed"], "open": statuses["open"],
                "resolved_share": share(statuses["resolved"], raised), "dismissed_share": share(statuses["dismissed"], raised),
                "at_merge": at_merge, "open_at_merge": open_at_merge, "open_at_merge_share": share(open_at_merge, at_merge),
                "indeterminate_at_merge": indeterminate}
    return {"merged": len(pulls), "unread": unread, "with_agent_review": reviewed[FIRST.key],
            "with_second_review": reviewed[SECOND.key], "findings": findings}


def guide_settings(run: Run) -> tuple[str | None, list[verification_guides.Guide], bool] | None:
    if run.app_root is None:
        return None
    conf = config.load(run.app_root)
    if not conf.has(verification_guides.KEY):
        return None
    relative = verification_guides.directory(conf)
    if relative is None:
        return None, [], True
    guides, errors = verification_guides.load(run.app_root, relative)
    for skipped in errors[:20]:
        run.missing.append(f"verification: a guide is skipped: {verification_guides.clean(str(skipped))[:MAX_MISSING]}")
    return relative, guides, not errors


def verification_of(run: Run, pulls: list[rm.Pull], guides: list[verification_guides.Guide], complete: bool = True) -> Json:
    counts: Counter[str] = Counter()
    uncovered: set[str] = set()
    pulls_uncovered = 0
    unread_files = 0
    for each in pulls:
        try:
            verification.block(each.body.replace("\r\n", "\n").split("\n"))
            dimensions = verification.parse(each.body)
        except verification.ParseError as error:
            counts["without_record" if error.rule.startswith(NO_BLOCK) else "unparsed"] += 1
        else:
            counts["with_record"] += 1
            for dimension in dimensions:
                counts[dimension.status] += 1
        try:
            paths = rm.changed_files(run.repo, each.number)
        except (subprocess.CalledProcessError, rm.FilesCapped):
            unread_files += 1
            continue
        if not complete:
            continue
        _, missed = verification_guides.areas(guides, paths)
        if missed:
            pulls_uncovered += 1
            uncovered.update(missed)
    if unread_files:
        run.missing.append(f"verification: the changed files of {unread_files} merged pull request(s) couldn't be listed in full "
                           f"(GitHub returns at most {rm.MAX_FILES} files per pull request); their paths aren't counted")
    if not complete:
        run.missing.append("verification: a guide couldn't be loaded, so the guides and the paths no guide covers aren't counted")
    return {"configured": True, "guides": len(guides) if complete else None, "merged": len(pulls), "with_record": counts["with_record"],
            "without_record": counts["without_record"], "unparsed": counts["unparsed"], "verified": counts["verified"],
            "not_verified": counts["not verified"], "mean_not_verified": share(counts["not verified"], counts["with_record"]),
            "uncovered_paths": len(uncovered) if complete else None,
            "pulls_with_uncovered_paths": pulls_uncovered if complete else None, "unread_files": unread_files,
            "uncovered_samples": [cleaned(path, MAX_PATH) for path in sorted(uncovered)[:MAX_SAMPLES]]}


def report_of(run: Run) -> Json:
    issue = rm.report_issue(run.repo)
    body = str(issue.get("body") or "") if issue else ""
    rows = rm.ticks_of(body) if body.startswith(rm.MARKER) else {}
    listed = rm.gh("--paginate", f"repos/{run.repo}/issues?state=all&labels={rm.LABEL}&per_page={PER_PAGE}")
    if not isinstance(listed, list):
        raise Unreadable("the review-mining issue list isn't a list")
    opened = in_window = closed = 0
    for each in listed:
        if not isinstance(each, dict) or "pull_request" in each or not isinstance(each.get("number"), int):
            continue
        user, text = each.get("user"), each.get("body")
        if not isinstance(user, dict) or user.get("login") != rm.BOT or not isinstance(text, str):
            continue
        if not any(rm.MINED.match(line.strip()) for line in text.splitlines()):
            continue
        opened += 1
        if run.since <= str(each.get("created_at") or "") <= run.until:
            in_window += 1
        closed_at = each.get("closed_at")
        if each.get("state") == "closed" and isinstance(closed_at, str) and run.since <= closed_at <= run.until:
            closed += 1
    return {"issue": int(issue["number"]) if issue else None, "rows": len(rows), "ticked": sum(1 for ticked, _ in rows.values() if ticked),
            "tasks_opened": opened, "tasks_opened_in_window": in_window, "tasks_closed_in_window": closed}


def default_branch(run: Run) -> str:
    found = rm.gh(f"repos/{run.repo}")
    branch = found.get("default_branch") if isinstance(found, dict) else None
    if not isinstance(branch, str) or not BRANCH.match(branch):
        raise Unreadable("the repository has no readable default branch")
    return branch


def shas(listed: object) -> list[str]:
    if not isinstance(listed, list):
        raise Unreadable("a commit list isn't a list")
    out = []
    for item in listed:
        sha = item.get("sha") if isinstance(item, dict) else None
        if not isinstance(sha, str) or not SHA.match(sha):
            raise Unreadable("a commit in a list has no SHA")
        out.append(sha)
    return out


def commits(run: Run, branch: str, path: str) -> list[str]:
    return shas(rm.gh("--paginate", f"repos/{run.repo}/commits?sha={quote(branch, safe='')}&path={quote(path, safe='/')}"
                                    f"&since={run.since}&until={run.until}&per_page={PER_PAGE}"))


def touches_app_skill(run: Run, sha: str) -> bool:
    for page in range(1, MAX_FILE_PAGES + 1):
        found = rm.gh(f"repos/{run.repo}/commits/{sha}?per_page={PER_PAGE}&page={page}")
        listed = found.get("files") if isinstance(found, dict) else None
        if not isinstance(listed, list):
            raise Unreadable(f"commit {sha[:7]} has no file list")
        for item in listed:
            for name in ("filename", "previous_filename"):
                value = item.get(name) if isinstance(item, dict) else None
                if isinstance(value, str) and value.startswith(SKILLS + "/") and not value.startswith(FLEET_SKILLS):
                    return True
        if len(listed) < PER_PAGE:
            return False
    raise Unreadable(f"commit {sha[:7]} changes over {MAX_FILE_PAGES * PER_PAGE} files")


def app_skill_commits(run: Run, branch: str) -> list[str]:
    candidates = commits(run, branch, SKILLS)
    if len(candidates) > MAX_SKILL_COMMITS:
        raise Unreadable(f"over {MAX_SKILL_COMMITS} commits touched {SKILLS}/ in the window")
    return [sha for sha in candidates if touches_app_skill(run, sha)]


def second_before(stamp: str) -> str:
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        raise Unreadable("the window's start isn't ISO 8601") from None
    return (parsed.astimezone(UTC) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def newest(run: Run, branch: str, until: str, path: str = "") -> Json | None:
    where = f"&path={quote(path, safe='/')}" if path else ""
    listed = rm.gh(f"repos/{run.repo}/commits?sha={quote(branch, safe='')}{where}&until={until}&per_page=1")
    shas(listed)
    return listed[0] if listed else None


def content_at(run: Run, path: str, ref: str) -> str | None:
    try:
        found = rm.gh(f"repos/{run.repo}/contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}")
    except subprocess.CalledProcessError as error:
        if not_found(error):
            return None
        raise
    if not isinstance(found, dict) or found.get("type") != "file" or found.get("encoding") != "base64":
        raise Unreadable(f"{path} at {ref[:12]} isn't a file the API returns whole")
    if not isinstance(found.get("size"), int) or found["size"] > MAX_CONTENT or not isinstance(found.get("content"), str):
        raise Unreadable(f"{path} at {ref[:12]} is over {MAX_CONTENT:,} bytes")
    try:
        return base64.b64decode(found["content"]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise Unreadable(f"{path} at {ref[:12]} isn't UTF-8") from None


def newly_checked(start: str, end: str) -> list[str]:
    before, after = rm.mistakes(start), rm.mistakes(end)
    return sorted(item.title for key, item in after.items()
                  if item.state == rm.CHECKED and key in before and before[key].state != rm.CHECKED)


def committed(item: Json | None) -> datetime | None:
    commit = item.get("commit") if isinstance(item, dict) else None
    committer = commit.get("committer") if isinstance(commit, dict) else None
    when = committer.get("date") if isinstance(committer, dict) else None
    if not isinstance(when, str):
        raise Unreadable("a commit has no committer date")
    try:
        parsed = datetime.fromisoformat(when.replace("Z", "+00:00"))
    except ValueError:
        raise Unreadable("a commit's committer date isn't ISO 8601") from None
    return parsed if parsed.tzinfo else None


def harness(run: Run, branch: str, guides_dir: str | None, guide_files: list[str]) -> Json:
    found: dict[str, list[str] | None] = {}
    for key, path in (("context", CONTEXT), ("invariants", INVARIANTS), ("semgrep", SEMGREP)):
        try:
            found[key] = commits(run, branch, path)
        except (subprocess.CalledProcessError, Unreadable, ValueError) as error:
            run.lack(f"harness: commits touching {path}", error)
            found[key] = None
    try:
        found["skills"] = app_skill_commits(run, branch)
    except (subprocess.CalledProcessError, Unreadable, ValueError) as error:
        run.lack(f"harness: commits touching the app's own skills ({SKILLS}/ but fleet-*)", error)
        found["skills"] = None
    found["verification_guides"] = None
    if guides_dir is not None:
        try:
            found["verification_guides"] = commits(run, branch, guides_dir)
        except (subprocess.CalledProcessError, Unreadable, ValueError) as error:
            run.lack(f"harness: commits touching {guides_dir}", error)
    complete = all(value is not None for key, value in found.items() if key != "verification_guides" or guides_dir is not None)
    union = {sha for value in found.values() for sha in value or []}
    counts: Json = {key: (len(value) if value is not None else None)
                    for key, value in sorted(found.items())}
    counts["total"] = len(union) if complete else None
    checked: int | None = None
    titles: list[str] = []
    try:
        base, head = newest(run, branch, second_before(run.since)), newest(run, branch, run.until)
        if base is None or head is None:
            run.missing.append(f"harness: no commit on {branch} before the window to compare {AGENTS} with")
        else:
            titles = newly_checked(content_at(run, AGENTS, base["sha"]) or "", content_at(run, AGENTS, head["sha"]) or "")
            checked = len(titles)
    except (subprocess.CalledProcessError, Unreadable, ValueError) as error:
        run.lack(f"harness: {AGENTS} at the window's first and last commits", error)
    ages: dict[str, int | None] = {}
    for path in [AGENTS, CONTEXT, INVARIANTS, *guide_files]:
        try:
            last = newest(run, branch, run.until, path)
            when = committed(last) if last is not None else None
        except (subprocess.CalledProcessError, Unreadable, ValueError) as error:
            run.lack(f"harness: when {path} last changed", error)
            ages[cleaned(path, MAX_PATH)] = None
            continue
        ages[cleaned(path, MAX_PATH)] = max((run.now - when).days, 0) if when is not None else None
    return {"commits": counts, "checked_in_window": checked, "checked_items": [cleaned(t, MAX_TITLE) for t in titles],
            "days_since_change": ages}


def fleet_of(run: Run, branch: str) -> Json:
    version: str | None = None
    try:
        text = content_at(run, VERSION_FILE, branch)
        if text is not None:
            if not agents_md.VERSION.fullmatch(text.strip()):
                raise Unreadable(f"{VERSION_FILE} isn't a vX.Y.Z version")
            version = text.strip()
    except (subprocess.CalledProcessError, Unreadable, ValueError) as error:
        run.lack(f"fleet: {VERSION_FILE}", error)
    sync_open: bool | None = None
    try:
        owner = run.repo.split("/", 1)[0]
        listed = rm.gh("--paginate", f"repos/{run.repo}/pulls?state=open&head={owner}:{SYNC_BRANCH}&per_page={PER_PAGE}")
        if not isinstance(listed, list):
            raise Unreadable("the pull request list isn't a list")
        sync_open = any(isinstance(pull, dict) and pull.get("state") == "open" for pull in listed)
    except (subprocess.CalledProcessError, Unreadable, ValueError) as error:
        run.lack(f"fleet: whether a {SYNC_BRANCH} pull request is open", error)
    return {"version": version, "sync_open": sync_open}


def collect(run: Run) -> Json:
    stats: Json = {"schema": SCHEMA, "repo": run.repo, "generated": run.until, "since": run.since, "days": run.days,
                   "default_branch": None, "reviews": None, "verification": None, "report": None, "harness": None, "fleet": None}
    settings: tuple[str | None, list[verification_guides.Guide], bool] | None = None
    configured = True
    try:
        settings = guide_settings(run)
        configured = settings is not None
    except config.ConfigError as error:
        run.lack("verification: [tool.fleet.verification]", error)
    pulls: list[rm.Pull] | None = None
    try:
        pulls = merged_pulls(run)
    except (subprocess.CalledProcessError, Unreadable, ValueError) as error:
        run.lack("reviews and verification: the merged pull requests", error)
    if pulls is not None:
        try:
            stats["reviews"] = reviews(run, pulls)
        except (subprocess.CalledProcessError, Unreadable, ValueError) as error:
            run.lack("reviews", error)
        if not configured:
            stats["verification"] = {"configured": False}
        elif settings is not None:
            try:
                stats["verification"] = verification_of(run, pulls, settings[1], settings[2])
            except (subprocess.CalledProcessError, Unreadable, ValueError) as error:
                run.lack("verification", error)
    try:
        stats["report"] = report_of(run)
    except (subprocess.CalledProcessError, Unreadable, ValueError) as error:
        run.lack("report", error)
    try:
        branch = default_branch(run)
    except (subprocess.CalledProcessError, Unreadable, ValueError) as error:
        run.lack("harness and fleet: the default branch", error)
    else:
        stats["default_branch"] = branch
        guides_dir, guides, _ = settings if settings is not None else (None, [], True)
        stats["harness"] = harness(run, branch, guides_dir, [guide.source for guide in guides])
        stats["fleet"] = fleet_of(run, branch)
    stats["missing"] = list(run.missing)
    return stats


def md(text: object, limit: int = MAX_PATH) -> str:
    return defuse(str(text).replace("\r", " ").replace("\n", " "), limit).replace("<", "&lt;").replace("|", "\\|").replace("`", "'")


def value(item: object) -> str:
    if item is None:
        return "n/a"
    if isinstance(item, bool):
        return "yes" if item else "no"
    if isinstance(item, float):
        return f"{round(item * 100)}%"
    return md(item)


def table(rows: list[tuple[str, object]]) -> list[str]:
    return ["| Measure | Value |", "| --- | --- |", *(f"| {label} | {value(item)} |" for label, item in rows)]


def get(node: object, *path: str) -> object:
    for name in path:
        node = node.get(name) if isinstance(node, dict) else None
    return node


def render(stats: Json) -> str:
    lines = [f"## Loop health of `{stats['repo']}`", "",
             f"The last {stats['days']} days ({stats['since'][:10]} to {stats['generated'][:10]}), from fleet's `loop-stats` "
             f"(schema {SCHEMA}). `n/a`: nothing to divide by, or not read (see below).", ""]
    r = stats.get("reviews")
    lines += ["### Reviews", ""]
    if r is None:
        lines.append("Not read.")
    else:
        f = r["findings"]
        lines += table([
            ("Merged pull requests", r["merged"]), ("… with an Agent review", r["with_agent_review"]),
            ("… with a Second review", r["with_second_review"]), ("… whose comments couldn't be read", r["unread"]),
            ("Findings raised", f["raised"]), ("… blocking", f["by_severity"]["blocking"]),
            ("… advisory", f["by_severity"]["advisory"]), ("… severity unknown (not on the newest findings line)", f["by_severity"][UNKNOWN]),
            ("Resolved", f["resolved"]), ("Resolved share", f["resolved_share"]), ("Dismissed", f["dismissed"]),
            ("Dismissed share", f["dismissed_share"]), ("Still open", f["open"]),
            ("Findings at merge (from the comments before it)", f["at_merge"]), ("Open at merge", f["open_at_merge"]),
            ("Open at merge share", f["open_at_merge_share"]),
            ("Pull requests whose comments changed at or after the merge (left out of at merge)", f["indeterminate_at_merge"])])
    v = stats.get("verification")
    lines += ["", "### Verification", ""]
    if v is None:
        lines.append("Not read.")
    elif not v.get("configured"):
        lines.append("Not configured (`[tool.fleet.verification]` isn't set on the default branch).")
    else:
        mean = v["mean_not_verified"]
        lines += table([
            ("Verification guides", v["guides"]), ("Merged pull requests", v["merged"]), ("… with a record", v["with_record"]),
            ("… without one", v["without_record"]), ("… whose record doesn't parse", v["unparsed"]),
            ("Dimensions `verified`", v["verified"]), ("Dimensions `not verified`", v["not_verified"]),
            ("`not verified` per record (mean)", f"{mean:.2f}" if isinstance(mean, float) else None),
            ("Changed paths no guide covers", v["uncovered_paths"]),
            ("… on how many merged pull requests", v["pulls_with_uncovered_paths"])])
        if v["uncovered_samples"]:
            lines += ["", "Uncovered paths (first " + str(MAX_SAMPLES) + "): " + ", ".join(f"`{md(p)}`" for p in v["uncovered_samples"])]
    rep = stats.get("report")
    lines += ["", "### The report", ""]
    if rep is None:
        lines.append("Not read.")
    else:
        lines += table([
            ("Report issue", f"#{rep['issue']}" if rep["issue"] else "none open"), ("Rows in it now", rep["rows"]),
            ("… ticked", rep["ticked"]), ("Issues opened from rows (all time)", rep["tasks_opened"]),
            ("… opened in the window", rep["tasks_opened_in_window"]), ("… closed in the window", rep["tasks_closed_in_window"])])
    h = stats.get("harness")
    lines += ["", f"### Harness changes on `{md(stats.get('default_branch') or '?')}`", ""]
    if h is None:
        lines.append("Not read.")
    else:
        c = h["commits"]
        lines += table([
            (f"Commits touching `{CONTEXT}`", c["context"]), (f"… `{INVARIANTS}`", c["invariants"]),
            ("… the verification guides", c["verification_guides"]), (f"… the app's own skills (`{SKILLS}/`, not `fleet-*`)", c["skills"]),
            (f"… `{SEMGREP}/`", c["semgrep"]), ("Distinct commits", c["total"]),
            ("Mistakes items newly `checked:`", h["checked_in_window"])])
        if h["checked_items"]:
            lines += ["", "Newly checked: " + ", ".join(f"**{md(t)}**" for t in h["checked_items"])]
        lines += ["", "| Guidance file | Days since it last changed |", "| --- | --- |"]
        lines += [f"| `{md(path)}` | {value(days) if days is not None else 'n/a (never, or not read)'} |"
                  for path, days in h["days_since_change"].items()]
    fl = stats.get("fleet")
    lines += ["", "### fleet", ""]
    if fl is None:
        lines.append("Not read.")
    else:
        lines += table([(f"`{VERSION_FILE}`", fl["version"]), (f"A `{SYNC_BRANCH}` pull request open", fl["sync_open"])])
    if stats["missing"]:
        lines += ["", "### Not read", "", *(f"- {md(item, MAX_MISSING + 80)}" for item in stats["missing"])]
    return "\n".join(lines) + "\n"


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--days", type=int, default=30, help="the window, in days before now (default 30)")
    parser.add_argument("--json", type=Path, required=True, metavar="OUT.json", help="where to write the numbers")
    parser.add_argument("--markdown", type=Path, required=True, metavar="OUT.md", help="where to write them as Markdown")
    parser.add_argument("--app-root", type=Path,
                        help="a checkout of the default branch, for [tool.fleet.verification] and its guides "
                             "(default: none, so no verification numbers)")
    parser.add_argument("--now", help="the end of the window as an ISO 8601 time (tests; default: now)")


def run(args: argparse.Namespace) -> int:
    if args.days < 1 or args.days > 3650:
        print("loop-stats: --days must be between 1 and 3650", file=sys.stderr)
        return 2
    if not REPO.match(args.repo):
        print("loop-stats: --repo must be owner/name", file=sys.stderr)
        return 2
    try:
        now = rm.parse_now(args.now)
    except ValueError:
        print("loop-stats: --now isn't an ISO 8601 time", file=sys.stderr)
        return 2
    state = Run(args.repo, args.days, now, rm.window(now, args.days), rm.window(now, 0), args.app_root)
    stats = collect(state)
    try:
        args.json.write_text(json.dumps(stats, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
        args.markdown.write_text(render(stats), encoding="utf-8", errors="replace")
    except OSError as error:
        print(f"loop-stats: can't write the output ({type(error).__name__}: {error})", file=sys.stderr)
        return 1
    for item in stats["missing"]:
        print(f"loop-stats: not read: {item}", file=sys.stderr)
    print(f"loop-stats: wrote {args.json} and {args.markdown}" + (f" ({len(stats['missing'])} part(s) not read)" if stats["missing"] else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fleet loop-stats", description=HELP)
    configure(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
