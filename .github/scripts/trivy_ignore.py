import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import sys
from typing import Any

MAX_DAYS = 180
MAX_BYTES = 65536
SECTION = "vulnerabilities"
OTHER_SECTIONS = ("misconfigurations", "secrets", "licenses")
KEYS = ("id", "paths", "purls", "statement", "expired_at")
LISTS = ("paths", "purls")
FILE_PATH = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*\.ya?ml")
BRANCH = re.compile(r"[A-Za-z0-9._/-]+")
COMMIT = re.compile(r"[0-9a-f]{40}")
FINDING_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,63}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
KEY_LINE = re.compile(r"([a-z_]+):(?:[ ]+(.*))?")
PLAIN_START = set("-?:,[]{}#&*!|>'\"%@`")
GLOB = set("*?[]{}\\")
Entry = dict[str, Any]


class Invalid(Exception):
    pass


def escaped(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def gh(path: str) -> tuple[int, str, str]:
    done = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout, done.stderr


def answer(path: str, what: str) -> Any:
    code, out, error = gh(path)
    if code != 0:
        raise Invalid(f"couldn't read {what} ({error.strip()[:200]})")
    try:
        return json.loads(out)
    except ValueError as problem:
        raise Invalid(f"couldn't read {what} (not JSON)") from problem


def fetch(repo: str, path: str) -> tuple[str | None, str]:
    repository = answer(f"repos/{repo}", "the repository")
    branch = repository.get("default_branch") if isinstance(repository, dict) else None
    if not isinstance(branch, str) or not BRANCH.fullmatch(branch) or ".." in branch:
        raise Invalid("couldn't read the repository's default branch")
    tip = answer(f"repos/{repo}/branches/{branch}", f"the default branch ({branch})")
    commit = (tip.get("commit") or {}).get("sha") if isinstance(tip, dict) else None
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        raise Invalid(f"couldn't read the commit of the default branch ({branch})")
    origin = f"{path} at {branch} ({commit})"
    code, out, error = gh(f"repos/{repo}/contents/{path}?ref={commit}")
    if code != 0:
        if "HTTP 404" in error:
            return None, origin
        raise Invalid(f"couldn't read {origin} ({error.strip()[:200]})")
    try:
        found = json.loads(out)
    except ValueError as problem:
        raise Invalid(f"couldn't read {origin} (not JSON)") from problem
    if not isinstance(found, dict) or found.get("type") != "file" or found.get("encoding") != "base64":
        raise Invalid(f"{origin} isn't a file of at most {MAX_BYTES} bytes")
    try:
        raw = base64.b64decode(str(found.get("content") or ""), validate=False)
        if len(raw) > MAX_BYTES:
            raise Invalid(f"{origin} is larger than {MAX_BYTES} bytes")
        return raw.decode("utf-8"), origin
    except ValueError as problem:
        raise Invalid(f"{origin} isn't UTF-8 text") from problem


def scalar(text: str, number: int) -> str:
    text = text.strip()
    if not text:
        raise Invalid(f"line {number}: a value is missing")
    if text[0] == '"':
        end = 1
        while end < len(text) and text[end] != '"':
            end += 2 if text[end] == "\\" else 1
        if end >= len(text):
            raise Invalid(f"line {number}: a double-quoted value that doesn't end on its line")
        try:
            value = json.loads(text[: end + 1])
        except ValueError as problem:
            raise Invalid(f"line {number}: a double-quoted value with an escape that isn't JSON's") from problem
        rest = text[end + 1:]
    elif text[0] == "'":
        end = 1
        while end < len(text):
            if text[end] == "'":
                if text[end + 1: end + 2] != "'":
                    break
                end += 1
            end += 1
        if end >= len(text):
            raise Invalid(f"line {number}: a single-quoted value that doesn't end on its line")
        value = text[1:end].replace("''", "'")
        rest = text[end + 1:]
    else:
        value = re.split(r"\s+#", text, maxsplit=1)[0].rstrip()
        rest = ""
        if value[0] in PLAIN_START or ": " in value or value.endswith(":"):
            raise Invalid(f"line {number}: put this value in double quotes")
    if rest.strip() and not re.fullmatch(r"\s+#.*", rest):
        raise Invalid(f"line {number}: text after the closing quote")
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise Invalid(f"line {number}: a value that is empty or holds a character that can't be printed")
    return value


def lines_of(text: str) -> list[tuple[int, int, str]]:
    found = []
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.rstrip("\r").rstrip()
        body = line.lstrip(" ")
        if not body or body.startswith("#"):
            continue
        if body[0] == "\t" or "\t" in line[: len(line) - len(body)]:
            raise Invalid(f"line {number}: a tab in the indentation")
        found.append((number, len(line) - len(body), body))
    return found


def parse(text: str) -> list[Entry]:
    lines = lines_of(text)
    if not lines:
        return []
    number, indent, body = lines[0]
    head = re.fullmatch(r"([a-z_]+):\s*(\[\s*\])?\s*(#.*)?", body)
    if indent != 0 or not head:
        raise Invalid(f"line {number}: the file starts with `{SECTION}:`")
    if head.group(1) in OTHER_SECTIONS:
        raise Invalid(f"line {number}: only `{SECTION}:` is accepted; `{head.group(1)}:` isn't")
    if head.group(1) != SECTION:
        raise Invalid(f"line {number}: the file starts with `{SECTION}:`")
    if head.group(2):
        if len(lines) > 1:
            raise Invalid(f"line {lines[1][0]}: text after an empty `{SECTION}: []`")
        return []
    entries: list[Entry] = []
    entry_indent = lines[1][1] if len(lines) > 1 else 0
    current: Entry | None = None
    items: list[str] | None = None
    items_indent = -1
    for number, indent, body in lines[1:]:
        dash = body.startswith("- ") and not body.startswith("-  ")
        if indent == 0 and not dash:
            name = body.split(":", 1)[0]
            if name in OTHER_SECTIONS:
                raise Invalid(f"line {number}: only `{SECTION}:` is accepted; `{name}:` isn't")
            raise Invalid(f"line {number}: only one `{SECTION}:` list is accepted")
        if items is not None and dash and indent > entry_indent:
            if indent < entry_indent + 2 or items_indent not in (-1, indent):
                raise Invalid(f"line {number}: unexpected indentation")
            items_indent = indent
            items.append(scalar(body[2:], number))
            continue
        if dash and indent == entry_indent:
            current = {"line": number}
            entries.append(current)
            body = body[2:]
        elif current is None or dash or indent != entry_indent + 2:
            raise Invalid(f"line {number}: unexpected indentation")
        items = None
        items_indent = -1
        pair = KEY_LINE.fullmatch(body)
        if not pair or pair.group(1) not in KEYS:
            raise Invalid(f"line {number}: expected one of {', '.join(KEYS)}")
        key, value = pair.group(1), pair.group(2)
        if key in current:
            raise Invalid(f"line {number}: `{key}` is given twice")
        if key in LISTS:
            if value is not None and not value.startswith("#"):
                raise Invalid(f"line {number}: `{key}` is a list, one `- value` per line")
            items = []
            current[key] = items
        else:
            current[key] = scalar(value or "", number)
    return entries


def checked(entries: list[Entry], today: datetime.date) -> tuple[list[Entry], list[Entry]]:
    active, expired = [], []
    for entry in entries:
        at = f"the entry at line {entry['line']}"
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not FINDING_ID.fullmatch(identifier):
            raise Invalid(f"{at} needs an `id` such as CVE-2026-0001")
        at = f"{identifier} (line {entry['line']})"
        paths = entry.get("paths") or []
        if not paths:
            raise Invalid(f"{at} needs `paths`: the files in the image it is accepted for")
        for path in paths:
            if GLOB & set(path):
                raise Invalid(f"{at}: a path is an exact path, without wildcards or backslashes")
        for purl in entry.get("purls", []):
            if not purl.startswith("pkg:") or " " in purl:
                raise Invalid(f"{at}: a purl starts with pkg: and has no space")
        if "purls" in entry and not entry["purls"]:
            raise Invalid(f"{at}: `purls` is empty")
        if "statement" not in entry:
            raise Invalid(f"{at} needs a `statement`: why the finding is accepted")
        date = entry.get("expired_at", "")
        if not DATE.fullmatch(date):
            raise Invalid(f"{at} needs `expired_at: YYYY-MM-DD`")
        try:
            expires = datetime.date.fromisoformat(date)
        except ValueError as problem:
            raise Invalid(f"{at}: `expired_at` isn't a date") from problem
        if (expires - today).days > MAX_DAYS:
            raise Invalid(f"{at}: `expired_at` is more than {MAX_DAYS} days away")
        (active if expires > today else expired).append(entry)
    return active, expired


def rendered(entries: list[Entry]) -> str:
    if not entries:
        return f"{SECTION}: []\n"
    out = [f"{SECTION}:"]
    for entry in entries:
        out.append(f"  - id: {json.dumps(entry['id'], ensure_ascii=False)}")
        for key in LISTS:
            if entry.get(key):
                out.append(f"    {key}:")
                out.extend(f"      - {json.dumps(item, ensure_ascii=False)}" for item in entry[key])
        out.append(f"    statement: {json.dumps(entry['statement'], ensure_ascii=False)}")
        out.append(f"    expired_at: {entry['expired_at']}")
    return "\n".join(out) + "\n"


def described(entry: Entry) -> str:
    return escaped(f"{entry['id']} in {', '.join(entry['paths'])} until {entry['expired_at']}: {entry['statement']}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="trivy_ignore.py")
    parser.add_argument("--repo")
    parser.add_argument("--path")
    parser.add_argument("--file")
    parser.add_argument("--out", required=True)
    parser.add_argument("--today")
    arguments = parser.parse_args(argv)
    try:
        today = datetime.date.fromisoformat(arguments.today) if arguments.today else datetime.datetime.now(datetime.UTC).date()
        if arguments.file:
            with open(arguments.file, encoding="utf-8") as file:
                text: str | None = file.read()
            origin = arguments.file
        else:
            if not arguments.repo or not arguments.path:
                raise Invalid("--repo and --path, or --file, are required")
            if not FILE_PATH.fullmatch(arguments.path) or ".." in arguments.path.split("/") or "." in arguments.path.split("/"):
                raise Invalid("the ignore file's path is a .yaml or .yml file inside the repository, such as .trivyignore.yaml")
            text, origin = fetch(arguments.repo, arguments.path)
        origin = escaped(origin)
        if text is None:
            print(f"::notice::No {origin}: the image scan accepts no finding.")
            active: list[Entry] = []
        else:
            active, expired = checked(parse(text), today)
            for entry in expired:
                print(f"::warning::The acceptance of {escaped(entry['id'])} expired on {entry['expired_at']} ({origin}): the image scan reports it again.")
            print(f"{len(active)} accepted finding{'' if len(active) == 1 else 's'} from {origin}:")
            for entry in active:
                print(f"  - {described(entry)}")
        with open(arguments.out, "w", encoding="utf-8") as file:
            file.write(rendered(active))
    except (Invalid, OSError, UnicodeError, ValueError) as problem:
        print(f"::error::The accepted findings can't be used, so the job fails: {escaped(str(problem))}")
        return 1
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as file:
            file.write(f"file={arguments.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
