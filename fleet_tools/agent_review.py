import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

HELP = "the independent reviews' decisions (detect, wanted, previous, plan, risky, report), for agent-review.yml"

Json = dict[str, Any]


class Review(NamedTuple):
    key: str
    context: str
    title: str
    extra_prompt: str

    @property
    def marker(self) -> str:
        return f"<!-- {self.key} -->"

    @property
    def state(self) -> re.Pattern[str]:
        return re.compile(rf"^<!-- {self.key}-state sha=([0-9a-f]{{40}}) verdict=(pass|blocking)(?: record=([0-9a-f]{{16}}))?(?: run=([0-9]+))? -->$")

    @property
    def findings(self) -> re.Pattern[str]:
        return re.compile(rf"^<!-- {self.key}-findings ([A-Za-z0-9+/=]*) -->$")

    @property
    def outcomes(self) -> re.Pattern[str]:
        return re.compile(rf"^<!-- {self.key}-outcomes ([A-Za-z0-9+/=]*) -->$")


FIRST = Review("agent-review", "Agent review", "Independent review", "")
SECOND = Review("second-review", "Second review", "Second review", "second-review.md")
REVIEWS = {"first": FIRST, "second": SECOND}
MARKER = FIRST.marker
SHA = re.compile(r"^[0-9a-f]{40}$")
SESSION = re.compile(r"^claude-session:\s*\S", re.IGNORECASE | re.MULTILINE)
CO_AUTHOR = re.compile(r"^co-authored-by:.*(\bclaude\b|anthropic\.com)", re.IGNORECASE | re.MULTILINE)
BODY_SIGNS = ("Generated with [Claude Code]", "claude.ai/code/session_")
SEVERITIES = ("blocking", "advisory")
MAX_COMMENT = 60000
MAX_FINDINGS_LINE = 8192
HIDDEN_LINES = 4
OUTCOMES_HEADING = "### Outcomes"
FOOTER = "<sub>From a fresh session"
FINDING_ID = re.compile(r"^[0-9a-f]{8}$")
SECRETS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
WITHHELD = "error_secret_in_answer"
STATUS_CONTEXT = FIRST.context
BOT = "github-actions[bot]"
LABEL_EVENTS = ("labeled", "unlabeled")
NOT_WANTED = {
    "label": "A label event doesn't start the first review",
    "reviewed": "Already reviewed at this commit: marking the pull request ready doesn't review it again",
    "other_label": "The label isn't one the second review reacts to",
    "not_owner": "A label removed by anyone but the repository owner leaves the second review as it was",
    "reviewed_second": "Already reviewed a second time at this commit",
}
NOT_RISKY = "Not a risky pull request"
DEFAULT_TRIGGER_LABELS = "safety-critical,security"
ALWAYS_RISKY = (".github/**", ".claude/**")
EVENT_KEYS = ("action", "sender", "label", "labels", "files")
VERIFICATION_FILE = "verification.md"
VERIFICATION_PROMPT = "verification-review.md"
VERIFICATION_CHANGED = "verification-changed.md"
NO_BLOCK = "no verification block"
RECORD_HASH = re.compile(r"^[0-9a-f]{16}$")
RUN_LINK = re.compile(r"/actions/runs/[0-9]+$")
RESTORED = "#restored"
UNPARSED = ("NOTE: this record doesn't parse ({error}). Its format is `fleet checks`' job; judge what it covers from the text as "
            "it stands.\n\n")

LOCKFILES = ("poetry.lock", "uv.lock", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml")
IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".heic", ".bmp", ".tif", ".tiff")
SAMPLE_DATA = re.compile(r"(^|/)tests/|fixture|demo|sample|example|seed", re.IGNORECASE)

USAGE = """usage: agent_review.py [second] COMMAND ...   (second: the "Second review"'s marker, status and rules)
  agent_review.py detect                          commit messages (JSON lines) on stdin, $BODY: true or false
  agent_review.py wanted ACTION                   the head's statuses ([context, state, creator, description] JSON
                                                  lines) on stdin, $SENDER, $OWNER, $LABEL, $LABELS, $TRIGGER_LABELS
                                                  ($FAKE_EVENT, self-test only): wanted=true|false, reason=...,
                                                  reviewed=true|false and pending=true|false for $GITHUB_OUTPUT
  agent_review.py event-class                     $ACTION, $LABEL, $TRIGGER_LABELS: class=push|label|other-label for
                                                  $GITHUB_OUTPUT (the second review's concurrency group; the label
                                                  matched against $TRIGGER_LABELS as `wanted` matches it)
  agent_review.py previous COMMENTS.jsonl OUT.md  the last review's commit and verdict, its text to OUT.md
  agent_review.py plan                            gather the change into $OUT_DIR ($GENERATED left out of the diff; with
                                                  $VERIFICATION=true, $BODY's verification record to verification.md,
                                                  reread when its hash differs from $PREV_RECORD); mode, description,
                                                  incremental, touched (base64, one file per line), record (the hash)
  agent_review.py risky FILES.txt [REASON.txt]    $LABELS (JSON), $TRIGGER_LABELS, $RISKY_PATHS and the changed files:
                                                  risky=true|false and reason=... for $GITHUB_OUTPUT (and REASON.txt)
  agent_review.py report OUT.json                 the reviewer's answer as the verdict, description and comment
                                                  ($RISKY_REASON goes into the second review's comment, $RUN_ID into
                                                  its state line)
  agent_review.py second runs COMMENTS.jsonl      the head's statuses ([context, state, creator, description,
                                                  target_url] JSON lines) on stdin, $RUN_ID, $RUN_URL: the run ids to
                                                  look up (this run's, the statuses', the comment's)
  agent_review.py second publish SHA RUNS.jsonl COMMENTS.jsonl STATE
                                                  the statuses on stdin, the looked-up runs ([id, workflow_id, event,
                                                  head_sha, head_branch, head repository, [pull numbers], status] JSON
                                                  lines), $PR, $HEAD_REF, $DESCRIPTION:
                                                  status=, description= (empty: post nothing), decided_by=, comment=
  agent_review.py second settle SHA RUNS.jsonl    the statuses on stdin: the status to post again (state, target_url,
                                                  description, one per line) when the newest isn't the decided one"""


def is_agent(messages: list[str], body: str) -> bool:
    if any(sign in body for sign in BODY_SIGNS):
        return True
    return any(SESSION.search(m) or CO_AUTHOR.search(m) for m in messages)


def split_list(text: str, separator: str = ",") -> list[str]:
    return [item.strip() for item in text.split(separator) if item.strip()]


def event_from_env(env: dict[str, str]) -> dict[str, Any]:
    try:
        labels = json.loads(env.get("LABELS") or "[]")
    except ValueError:
        labels = []
    event: dict[str, Any] = {
        "action": env.get("ACTION", ""),
        "sender": env.get("SENDER", ""),
        "owner": env.get("OWNER", ""),
        "label": env.get("LABEL", ""),
        "labels": [str(name) for name in labels] if isinstance(labels, list) else [],
        "files": None,
        "trigger_labels": split_list(env.get("TRIGGER_LABELS", DEFAULT_TRIGGER_LABELS)),
        "run_url": env.get("RUN_URL", "") if RUN_LINK.search(env.get("RUN_URL", "")) else "",
    }
    for token in env.get("FAKE_EVENT", "").split():
        key, _, value = token.partition("=")
        if key in EVENT_KEYS:
            event[key] = split_list(value) if key in ("labels", "files") else value
    return event


def reviewed_here(statuses: Sequence[Sequence[str]], review: Review, own: str = "") -> bool:
    for status in statuses:
        if len(status) >= 3 and status[0] == review.context and status[2] == BOT:
            if own and len(status) >= 5 and status[4] in (own, own + RESTORED):
                return False
            if review is FIRST:
                return True
            return status[1] in ("success", "failure") and (len(status) < 4 or status[3] != NOT_RISKY)
    return False


def pending_first(action: str, statuses: Sequence[Sequence[str]], review: Review, event: dict[str, Any]) -> bool:
    if review is FIRST or action not in LABEL_EVENTS:
        return False
    return wanted(action, statuses, review, event)[0] and not reviewed_here(statuses, review, event.get("run_url", ""))


def wanted(action: str, statuses: Sequence[Sequence[str]], review: Review = FIRST,
           event: dict[str, Any] | None = None) -> tuple[bool, str]:
    event = event or {}
    if action in LABEL_EVENTS:
        if review is FIRST:
            return False, NOT_WANTED["label"]
        if event.get("label", "") not in event.get("trigger_labels", []):
            return False, NOT_WANTED["other_label"]
        if action == "unlabeled" and (not event.get("owner") or event.get("sender") != event.get("owner")):
            return False, NOT_WANTED["not_owner"]
        return True, ""
    if action == "ready_for_review" and reviewed_here(statuses, review, event.get("run_url", "")):
        return False, NOT_WANTED["reviewed"]
    return True, ""


def event_class(event: dict[str, Any]) -> str:
    if event.get("action") not in LABEL_EVENTS:
        return "push"
    return "label" if event.get("label", "") in event.get("trigger_labels", []) else "other-label"


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    pattern = pattern.strip().lstrip("/")
    out = ""
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**/", index):
            out += "(?:.*/)?"
            index += 3
        elif pattern.startswith("**", index):
            out += ".*"
            index += 2
        elif char == "*":
            out += "[^/]*"
            index += 1
        elif char == "?":
            out += "[^/]"
            index += 1
        else:
            out += re.escape(char)
            index += 1
    return re.compile(f"^{out}$")


def matches(path: str, pattern: str) -> bool:
    return bool(glob_to_regex(pattern).match(path))


def changed_paths(files_text: str) -> list[str]:
    paths: list[str] = []
    for line in files_text.splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 2:
            continue
        paths += [p for p in parts[1:] if p]
    return paths


def risky(labels: Iterable[str], paths: Iterable[str], trigger_labels: Iterable[str],
          patterns: Iterable[str], always: Iterable[str] = ALWAYS_RISKY) -> tuple[bool, str]:
    reasons = [f"labelled `{label}`" for label in labels if label in set(trigger_labels)]
    globs = [*(p for p in always if p.strip()), *(p for p in patterns if p.strip())]
    for path in paths:
        for pattern in globs:
            if matches(path, pattern):
                reasons.append(f"changes `{path}` (matches `{pattern.strip()}`)")
                break
    if not reasons:
        return False, NOT_RISKY
    reason = "; ".join(reasons)
    return True, reason if len(reason) <= 1000 else reason[:1000] + " …"


def hidden_lines(body: str, review: Review = FIRST) -> tuple[str, str, list[Json] | None, int]:
    lines = body.splitlines()
    sha = verdict = ""
    findings: list[Json] | None = None
    index = 1
    for index in range(1, min(len(lines), HIDDEN_LINES)):
        if (state := review.state.match(lines[index])) and not sha:
            sha, verdict = state.group(1), state.group(2)
        elif (found := review.findings.match(lines[index])) and findings is None:
            findings = decode_findings(found.group(1))
        elif index == HIDDEN_LINES - 1 and review.outcomes.match(lines[index]):
            index += 1
            break
        else:
            break
    else:
        index = min(len(lines), HIDDEN_LINES)
    return sha, verdict, findings, index


def previous(bodies: list[str], review: Review = FIRST) -> tuple[str, str, str]:
    for body in bodies:
        if not body.startswith(review.marker):
            continue
        sha, verdict, _, end = hidden_lines(body, review)
        if not sha:
            return "", "", ""
        return sha, verdict, "\n".join(without_outcomes(body.splitlines()[end:])).strip() + "\n"
    return "", "", ""


def previous_record(bodies: list[str], review: Review = FIRST) -> str:
    for body in bodies:
        if not body.startswith(review.marker):
            continue
        lines = body.splitlines()
        for index in range(1, min(len(lines), HIDDEN_LINES)):
            if state := review.state.match(lines[index]):
                return state.group(3) or ""
        return ""
    return ""


def without_outcomes(lines: list[str]) -> list[str]:
    try:
        start = lines.index(OUTCOMES_HEADING)
    except ValueError:
        return lines
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith(FOOTER)), len(lines))
    return [*lines[:start], *lines[end:]]


def previous_findings(body: str, review: Review = FIRST) -> list[Json] | None:
    if not body.startswith(review.marker):
        return None
    return hidden_lines(body, review)[2]


def decode_findings(encoded: str) -> list[Json] | None:
    try:
        value = json.loads(base64.b64decode(encoded, validate=True))
    except (ValueError, TypeError):
        return None
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return None
    return [dict(item) for item in value]


def plain_title(title: object) -> str:
    kept = "".join(c for c in str(title or "").lower() if not unicodedata.category(c).startswith(("P", "S")))
    return " ".join(kept.split())


def finding_id(kind: object, file: object, title: object) -> str:
    key = "\n".join([str(kind or ""), str(file or ""), plain_title(title)])
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def with_ids(findings: list[Json]) -> list[Json]:
    return [{**f, "id": finding_id(f["kind"], f.get("file"), f["title"])} for f in findings]


def findings_rows(findings: list[Json]) -> list[Json]:
    return [{"id": finding_id(f["kind"], f.get("file"), f["title"]), "severity": f["severity"], "kind": defuse(f["kind"], 120),
             "title": defuse(f["title"], 200), "file": defuse(f.get("file") or "", 300), "check": defuse(f.get("check") or "", 300)}
            for f in findings]


def findings_line(findings: list[Json], review: Review = FIRST) -> str:
    rows = findings_rows(findings)
    while True:
        line = f"<!-- {review.key}-findings {base64.b64encode(json.dumps(rows, ensure_ascii=False).encode()).decode()} -->"
        if len(line.encode()) <= MAX_FINDINGS_LINE or not rows:
            return line
        rows.pop()


def git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout


def is_image(path: str) -> bool:
    return path.lower().endswith(IMAGES)


def generated_from(text: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def left_out(path: str, generated: Sequence[str] = ()) -> bool:
    if path.startswith(".github/"):
        return False
    if is_image(path) or path in generated:
        return True
    if SAMPLE_DATA.search(path):
        return False
    return path.rsplit("/", 1)[-1] in LOCKFILES


def diff(repo: str, *revs: str, paths: list[str] | None = None, exclude: list[str] | None = None) -> str:
    spec = [f":(literal){p}" for p in paths] if paths is not None else ["."]
    spec += [f":(exclude,literal){p}" for p in exclude or []]
    return git(repo, "diff", "--no-ext-diff", "--no-textconv", *revs, "--", *spec)


def own_files(repo: str, main: str, rev: str) -> list[str]:
    out = git(repo, "diff", "--no-renames", "--name-only", "-z", f"{main}...{rev}")
    return [p for p in out.split("\0") if p]


def is_ancestor(repo: str, old: str, new: str) -> bool:
    if not (SHA.match(old) and SHA.match(new)) or old == new:
        return False
    return subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", old, new], capture_output=True).returncode == 0


def own_change(repo: str, main: str, rev: str) -> str:
    return git(repo, "diff", "--no-ext-diff", "--no-textconv", "--full-index", "--binary", f"{main}...{rev}")


def only_main_merged(repo: str, main: str, prev: str, head: str) -> bool:
    if not is_ancestor(repo, prev, head):
        return False
    added = git(repo, "rev-list", "--parents", f"{prev}..{head}", f"^{main}").splitlines()
    if not added or any(len(line.split()) < 3 for line in added):
        return False
    if sorted(own_files(repo, main, prev)) != sorted(own_files(repo, main, head)):
        return False
    return own_change(repo, main, prev) == own_change(repo, main, head)


def record_parser() -> ModuleType:
    try:
        from fleet_tools import verification
    except ModuleNotFoundError:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from fleet_tools import verification
    return verification


def verification_record(body: str) -> tuple[str, str] | None:
    verification = record_parser()
    text = body.replace("\r\n", "\n")
    lines = text.split("\n")
    try:
        begin, end = verification.block(lines)
        kept = lines[begin - 1:end]
    except verification.ParseError as error:
        begins = [index for index, line in enumerate(lines) if line.strip() == verification.BEGIN]
        if error.rule.startswith(NO_BLOCK) or not begins:
            return None
        kept = lines[begins[0]:]
    note = ""
    try:
        verification.parse(text)
    except verification.ParseError as error:
        note = UNPARSED.format(error=error)
    record = "\n".join(kept).rstrip("\n")
    if len(record) > verification.MAX_BODY:
        record = record[:verification.MAX_BODY] + "\n(cut: the record is over the size limit)"
    return record + "\n", note


def record_hash(record: str) -> str:
    return hashlib.sha256(record.encode()).hexdigest()[:16]


def append_prompt(out: Path, trusted: str, name: str) -> None:
    with open(out / "prompt.md", "a", encoding="utf-8") as f:
        f.write("\n" + Path(trusted, name).read_text(encoding="utf-8"))


def plan(env: dict[str, str], review: Review = FIRST) -> dict[str, str]:
    repo, out, main, head = env["REPO_DIR"], Path(env["OUT_DIR"]), env["MAIN"], env["HEAD"]
    generated = generated_from(env.get("GENERATED", ""))
    omitted = [p for p in own_files(repo, main, head) if left_out(p, generated)]
    (out / "diff.patch").write_text(diff(repo, f"{main}...{head}", exclude=omitted))
    (out / "omitted.txt").write_text("".join(p + "\n" for p in omitted))
    (out / "files.txt").write_text(git(repo, "diff", "--name-status", f"{main}...{head}"))
    (out / "commits.txt").write_text(git(repo, "log", "--format=commit %H%n%B", f"{main}..{head}"))
    record = ""
    found = verification_record(env.get("BODY", "")) if env.get("VERIFICATION") == "true" else None
    if found:
        text, note = found
        (out / VERIFICATION_FILE).write_text(note + text, encoding="utf-8")
        record = record_hash(text)
        append_prompt(out, env["TRUSTED"], VERIFICATION_PROMPT)
    if review.extra_prompt:
        append_prompt(out, env["TRUSTED"], review.extra_prompt)
    result = {"mode": "review", "description": "", "incremental": "false", "touched": "", "record": record}
    prev = env.get("PREV_SHA", "")
    try:
        same_record = env.get("VERIFICATION") != "true" or record == env.get("PREV_RECORD", "")
        if env.get("PREV_VERDICT") == "pass" and env.get("PREV_STATE") == "success" and same_record \
                and only_main_merged(repo, main, prev, head):
            return {**result, "mode": "carry",
                    "description": f"Carried forward from the review of {prev[:7]}: only main merged in since"}
    except Exception as e:
        print(f"::warning::Reviewing again: couldn't compare with the last review ({type(e).__name__}).")
    try:
        body = Path(env["PREV_BODY"]).read_text() if env.get("PREV_BODY") else ""
        if body and is_ancestor(repo, prev, head):
            own = sorted(set(own_files(repo, main, head)) | set(own_files(repo, main, prev)))
            files = [p for p in own if not left_out(p, generated)]
            since = diff(repo, prev, head, paths=files) if files else ""
            touched = git(repo, "diff", "--name-only", prev, head, "--", *[f":(literal){p}" for p in own]) if own else ""
            (out / "since-last-review.patch").write_text(since)
            (out / "previous-review.md").write_text(body)
            append_prompt(out, env["TRUSTED"], "incremental.md")
            if record and record != env.get("PREV_RECORD", ""):
                append_prompt(out, env["TRUSTED"], VERIFICATION_CHANGED)
            result["incremental"] = "true"
            result["touched"] = base64.b64encode(touched.encode()).decode()
    except Exception as e:
        print(f"::warning::Reviewing the whole change: the re-review couldn't be prepared ({type(e).__name__}).")
        for name in ("since-last-review.patch", "previous-review.md"):
            (out / name).unlink(missing_ok=True)
        result["incremental"] = "false"
        result["touched"] = ""
    return result


def structured(output: Json) -> Json | None:
    if isinstance(output.get("structured_output"), dict):
        return dict(output["structured_output"])
    text = output.get("result")
    if not isinstance(text, str):
        return None
    blocks = re.findall(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    for candidate in [text, *reversed(blocks)]:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return dict(value)
    return None


def findings_of(answer: Json) -> list[Json] | None:
    found = answer.get("findings")
    if not isinstance(found, list):
        return None
    out: list[Json] = []
    for f in found:
        if not isinstance(f, dict) or f.get("severity") not in SEVERITIES:
            return None
        if not isinstance(f.get("title"), str) or not isinstance(f.get("kind"), str) or not f["kind"].strip():
            return None
        if f.get("check") is not None and not isinstance(f["check"], str):
            return None
        out.append(dict(f))
    return out


def defuse(text: object, limit: int = 4000) -> str:
    s = str(text or "").replace("@", "@​").replace("<!--", "&lt;!--")
    return s if len(s) <= limit else s[:limit] + " …"


def render(findings: list[Json], summary: str, run_url: str, sha: str = "", review: Review = FIRST, reason: str = "",
           record: str = "", run: str = "") -> str:
    blocking = [f for f in findings if f["severity"] == "blocking"]
    advisory = [f for f in findings if f["severity"] == "advisory"]
    lines = [review.marker]
    if SHA.match(sha):
        state = f"<!-- {review.key}-state sha={sha} verdict={'blocking' if blocking else 'pass'}"
        state += f" record={record}" if RECORD_HASH.match(record) else ""
        lines.append(state + (f" run={run}" if run.isdigit() and run.isascii() else "") + " -->")
    lines.append(findings_line(findings, review))
    lines += [f"## {review.title}", ""]
    if blocking:
        lines.append(f"**{len(blocking)} blocking finding(s)**: the merge gate stays red until a new push clears them.")
    else:
        lines.append("No blocking findings.")
    if reason:
        lines += ["", f"Reviewed a second time because the pull request is {defuse(reason, 1000)}."]
    if summary:
        lines += ["", defuse(summary, 2000)]
    for title, group in (("Blocking", blocking), ("Advisory", advisory)):
        if not group:
            continue
        lines += ["", f"### {title}", ""]
        for f in group:
            where = defuse(f.get("file") or "", 300)
            if where and isinstance(f.get("line"), int):
                where += f":{f['line']}"
            lines.append(f"- **{defuse(f['title'], 300)}** `f:{finding_id(f['kind'], f.get('file'), f['title'])}`"
                         + (f" (`{where.replace('`', '')}`)" if where else ""))
            if f.get("detail"):
                lines.append("  " + defuse(f["detail"]).replace("\n", "\n  "))
            about = f"Kind: {defuse(f['kind'], 120)}"
            if f.get("check"):
                about += f". A check that would have caught it: {defuse(f['check'], 300)}"
            lines.append(f"  <sub>{about}</sub>")
    lines += ["", f"{FOOTER} with read-only access to this pull request's code and main's AGENTS.md, "
                  f"run by fleet's agent-review.yml ([run]({run_url})).</sub>"]
    text = "\n".join(lines)
    return text if len(text) <= MAX_COMMENT else text[:MAX_COMMENT] + "\n\n(cut short)"


LIMITS = {
    "error_max_turns": "The reviewer hit its turn limit (the max-turns input) before answering; see the run",
    "error_max_budget_usd": "The reviewer hit its spending limit (the budget-usd input) before answering; see the run",
    "error_secret_in_answer": "The reviewer's answer contained a secret; not posted",
}


def usage(output: Json) -> None:
    turns, cost = output.get("num_turns"), output.get("total_cost_usd")
    if isinstance(turns, int) and isinstance(cost, int | float):
        print(f"The reviewer took {turns} turn(s) and about ${cost:.2f}.")


def report(path: str, review: Review = FIRST) -> dict[str, str]:
    run_url = os.environ.get("RUN_URL", "")
    error = {"verdict": "error", "comment": "", "findings": ""}
    try:
        with open(path, encoding="utf-8") as f:
            output = json.load(f)
    except (OSError, ValueError):
        return {**error, "description": "The reviewer gave no readable output; see the run"}
    if not isinstance(output, dict):
        return {**error, "description": "The reviewer stopped with an error; see the run"}
    usage(output)
    if output.get("subtype") in LIMITS:
        return {**error, "description": LIMITS[output["subtype"]]}
    if output.get("is_error"):
        return {**error, "description": "The reviewer stopped with an error; see the run"}
    answer = structured(output)
    findings = findings_of(answer) if answer is not None else None
    if answer is None or findings is None:
        return {**error, "description": "The reviewer's answer wasn't in the expected form; see the run"}
    comment = render(findings, str(answer.get("summary") or ""), run_url, os.environ.get("REVIEWED_SHA", ""), review,
                     os.environ.get("RISKY_REASON", "") if review is not FIRST else "", os.environ.get("RECORD_HASH", ""),
                     os.environ.get("RUN_ID", "") if review is not FIRST else "")
    raw = json.dumps(output)
    if any(key and key in raw for key in (os.environ.get(name, "") for name in SECRETS)):
        return {**error, "description": LIMITS[WITHHELD]}
    rows = base64.b64encode(json.dumps(findings_rows(findings), ensure_ascii=False).encode()).decode()
    blocking = sum(f["severity"] == "blocking" for f in findings)
    if blocking:
        return {"verdict": "blocking", "description": f"{blocking} blocking finding(s); see the comment", "comment": comment, "findings": rows}
    return {"verdict": "pass", "description": f"No blocking findings ({len(findings)} advisory)", "comment": comment, "findings": rows}


class Entry(NamedTuple):
    state: str
    description: str
    run: int
    copy: bool


class Run(NamedTuple):
    workflow: object
    event: object
    head: object
    branch: object
    repository: object
    pulls: tuple[object, ...]
    completed: bool


ON_BEHALF = "An earlier run's blocking verdict stands: "


def run_of(url: str, prefix: str) -> tuple[int, bool] | None:
    if not prefix or not url.startswith(prefix):
        return None
    rest = url[len(prefix):]
    copy = rest.endswith(RESTORED)
    rest = rest[:-len(RESTORED)] if copy else rest
    return (int(rest), copy) if rest.isdigit() and rest.isascii() else None


def entries(statuses: Sequence[Sequence[str]], prefix: str, review: Review = SECOND) -> list[Entry]:
    out: list[Entry] = []
    for status in statuses:
        if len(status) < 2 or status[0] != review.context:
            continue
        found = run_of(status[4], prefix) if len(status) >= 5 and len(status) >= 3 and status[2] == BOT else None
        out.append(Entry(status[1], status[3] if len(status) >= 4 else "", found[0] if found else 0, bool(found and found[1])))
    return out


def run_prefix(run_url: str) -> str:
    return run_url.rsplit("/", 1)[0] + "/" if RUN_LINK.search(run_url) else ""


def read_runs(lines: Iterable[Sequence[object]]) -> dict[int, Run]:
    runs: dict[int, Run] = {}
    for line in lines:
        if len(line) < 8 or not isinstance(line[0], int) or not isinstance(line[6], list):
            continue
        runs[line[0]] = Run(line[1], line[2], line[3], line[4], line[5], tuple(line[6]), line[7] == "completed")
    return runs


def tied(run: Run, pr: int, sha: str, branch: str) -> bool:
    if run.head != sha:
        return False
    return pr in run.pulls if run.pulls else bool(branch) and run.branch == branch


def trusted(runs: dict[int, Run], mine: int, pr: int, sha: str, branch: str = "") -> dict[int, bool]:
    out = {mine: False}
    own = runs.get(mine)
    if own is None:
        return out
    same = (own.workflow, own.event, own.repository)
    for number, run in runs.items():
        if number != mine and (run.workflow, run.event, run.repository) == same and tied(run, pr, sha, branch):
            out[number] = run.completed
    return out


def effective(found: Sequence[Entry], runs: dict[int, bool]) -> dict[int, Entry]:
    out: dict[int, Entry] = {}
    for entry in found:
        if entry.run in runs and not entry.copy and entry.run not in out:
            out[entry.run] = entry
    return {run: e for run, e in out.items() if not (e.state == "pending" and runs[run])}


def decide(runs: dict[int, Entry]) -> Entry | None:
    if not runs:
        return None
    latest = runs[max(runs)]
    if latest.state == "success" and latest.description != NOT_RISKY:
        cleared = max((run for run, e in runs.items() if run < latest.run and e.state == "success" and e.description == NOT_RISKY), default=0)
        blocking = [run for run, e in runs.items() if cleared < run < latest.run and e.state == "failure"]
        if blocking:
            return runs[max(blocking)]
    return latest


def publish(found: Sequence[Entry], runs: dict[int, bool], mine: int, state: str, description: str) -> tuple[str, str] | None:
    current = effective(found, runs)
    current[mine] = Entry(state, description, mine, False)
    decided = decide(current)
    if state == "failure" or decided is None or decided.run == mine:
        return state, description
    if max(current) == mine:
        return decided.state, (ON_BEHALF + decided.description.removeprefix(ON_BEHALF))[:140]
    return None


def to_restore(found: Sequence[Entry], runs: dict[int, bool]) -> Entry | None:
    decided = decide(effective(found, runs))
    if decided is None or not found:
        return None
    newest = found[0]
    return None if (newest.state, newest.description, newest.run) == (decided.state, decided.description, decided.run) else decided


def writes_comment(post: tuple[str, str] | None, state: str, description: str, decided: Entry | None, mine: int) -> bool:
    if post != (state, description):
        return False
    return decided is None or decided.run == mine or decided.state == "pending"


def comment_state(body: str, review: Review = SECOND) -> tuple[str, int]:
    lines = body.splitlines()
    for index in range(1, min(len(lines), HIDDEN_LINES)):
        if state := review.state.match(lines[index]):
            return state.group(1), int(state.group(4) or 0)
    return "", 0


def comment_run(body: str, review: Review = SECOND) -> int:
    return comment_state(body, review)[1]


def own_comment(comments: Sequence[Sequence[object]], review: Review = SECOND) -> tuple[object, str] | None:
    for comment in comments:
        if len(comment) >= 2 and isinstance(comment[1], str) and comment[1].startswith(review.marker):
            return comment[0], comment[1]
    return None


def comment_action(comments: Sequence[Sequence[object]], runs: dict[int, Run], mine: int, pr: int, sha: str, decides: bool,
                   review: Review = SECOND, branch: str = "") -> str:
    found = own_comment(comments, review)
    if found is None:
        return "post"
    written_sha, written = comment_state(found[1], review)
    later = written > mine and written in trusted(runs, mine, pr, written_sha, branch)
    if later and (written_sha != sha or not decides):
        return f"keep {found[0]} {written}"
    return f"patch {found[0]}"


def candidates(statuses: Sequence[Sequence[str]], comments: Sequence[Sequence[object]], mine: int, prefix: str,
               review: Review = SECOND) -> list[int]:
    out = [mine]
    for entry in entries(statuses, prefix, review):
        if entry.run and entry.run not in out:
            out.append(entry.run)
    found = own_comment(comments, review)
    written = comment_state(found[1], review)[1] if found else 0
    if written and written not in out:
        out.append(written)
    return out


def json_lines(lines: Iterable[str]) -> list[list[Any]]:
    return [v for v in (json.loads(line) for line in lines if line.strip()) if isinstance(v, list)]


def append_outputs(lines: list[str]) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def write_outputs(values: dict[str, str]) -> None:
    append_outputs([f"verdict={values['verdict']}", f"description={values['description'][:140]}",
                    "comment=" + base64.b64encode(values["comment"].encode()).decode(), "findings=" + values.get("findings", "")])
    print(f"{values['verdict']}: {values['description']}")


def order(argv: list[str], env: dict[str, str]) -> int:
    mine = int(env["RUN_ID"])
    prefix = run_prefix(env.get("RUN_URL", ""))
    statuses = json_lines(sys.stdin)
    if argv[0] == "runs" and len(argv) == 2:
        with open(argv[1], encoding="utf-8") as f:
            print(*candidates(statuses, json_lines(f), mine, prefix), sep="\n")
        return 0
    if len(argv) < 3:
        print(USAGE, file=sys.stderr)
        return 2
    sha, pr = argv[1], int(env["PR"])
    with open(argv[2], encoding="utf-8") as f:
        runs = read_runs(json_lines(f))
    branch = env.get("HEAD_REF", "")
    found, verified = entries(statuses, prefix), trusted(runs, mine, pr, sha, branch)
    if argv[0] == "settle":
        restore = to_restore(found, verified)
        if restore:
            print(restore.state, f"{prefix}{restore.run}{RESTORED}", " ".join(restore.description.split()), sep="\n")
        return 0
    if argv[0] == "publish" and len(argv) == 5:
        state, description = argv[4], " ".join(env.get("DESCRIPTION", "").split())
        post = publish(found, verified, mine, state, description)
        print(f"status={post[0] if post else ''}")
        print(f"description={post[1] if post else ''}")
        decided = decide(effective(found, verified) | {mine: Entry(state, description, mine, False)})
        print(f"decided_by={decided.run if decided else mine}")
        with open(argv[3], encoding="utf-8") as f:
            action = comment_action(json_lines(f), runs, mine, pr, sha, post == (state, description), branch=branch)
        print(f"comment={action if writes_comment(post, state, description, decided, mine) else 'none'}")
        return 0
    print(USAGE, file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    review = FIRST
    if argv[:1] == ["second"]:
        review, argv = SECOND, argv[1:]
    if argv[:1] == ["detect"]:
        messages = [json.loads(line) for line in sys.stdin if line.strip()]
        print("true" if is_agent(messages, os.environ.get("BODY", "")) else "false")
        return 0
    if len(argv) == 2 and argv[0] == "wanted":
        event = event_from_env(dict(os.environ))
        action = str(event["action"] or argv[1])
        statuses = [s for s in (json.loads(line) for line in sys.stdin if line.strip()) if isinstance(s, list)]
        yes, reason = wanted(action, statuses, review, event)
        print(f"wanted={'true' if yes else 'false'}")
        print(f"reason={reason}")
        print(f"reviewed={'true' if reviewed_here(statuses, review, event['run_url']) else 'false'}")
        print(f"pending={'true' if pending_first(action, statuses, review, event) else 'false'}")
        return 0
    if argv == ["event-class"]:
        env = {k: v for k, v in os.environ.items() if k != "FAKE_EVENT"}
        value = event_class(event_from_env(env))
        append_outputs([f"class={value}"])
        print(f"class={value}")
        return 0
    if len(argv) == 3 and argv[0] == "previous":
        with open(argv[1], encoding="utf-8") as f:
            bodies = [b for b in (json.loads(line) for line in f if line.strip()) if isinstance(b, str)]
        sha, verdict, text = previous(bodies, review)
        if sha:
            Path(argv[2]).write_text(text, encoding="utf-8")
            record = previous_record(bodies, review)
            print(sha, verdict, *([record] if record else []))
        return 0
    if argv == ["plan"]:
        values = plan(dict(os.environ), review)
        append_outputs([f"{k}={v}" for k, v in values.items()])
        print(", ".join(f"{k}: {v}" for k, v in values.items()))
        return 0
    if len(argv) in (2, 3) and argv[0] == "risky":
        event = event_from_env(dict(os.environ))
        paths = event["files"] if event["files"] is not None else changed_paths(Path(argv[1]).read_text(encoding="utf-8"))
        always = split_list(os.environ["ALWAYS_PATHS"], "\n") if "ALWAYS_PATHS" in os.environ else ALWAYS_RISKY
        yes, reason = risky(event["labels"], paths, event["trigger_labels"], split_list(os.environ.get("RISKY_PATHS", ""), "\n"), always)
        append_outputs([f"risky={'true' if yes else 'false'}", f"reason={reason}"])
        if len(argv) == 3:
            Path(argv[2]).write_text(reason + "\n", encoding="utf-8")
        print(f"risky={'true' if yes else 'false'}: {reason}")
        return 0
    if review is SECOND and argv[:1] in (["runs"], ["publish"], ["settle"]):
        return order(argv, dict(os.environ))
    if len(argv) == 2 and argv[0] == "report":
        write_outputs(report(argv[1], review))
        return 0
    print(USAGE, file=sys.stderr)
    return 2


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("args", nargs=argparse.REMAINDER,
                        help="[second] detect | wanted ACTION | event-class | previous COMMENTS OUT | plan | risky FILES | report OUT | second runs | "
                             "second publish | second settle")


def run(args: argparse.Namespace) -> int:
    arguments: list[str] = args.args
    return main(arguments)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
