import fnmatch
import importlib.util
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from fleet_tools import agents_md, vendor
from fleet_tools.checks.context import Check, Context, relative
from fleet_tools.config import ConfigError

SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
MD_LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
TIMEOUT = 120
EXEMPT = ("node_modules/", ".fleet/", "tests/fixtures/")
FRONT_NAME = re.compile(r"^name:[ \t]*(?P<name>.*?)[ \t]*$", re.MULTILINE)


def check_skill(folder: str, text: str, exists: Callable[[str], bool] = lambda rel: True,
                skills: str = ".claude/skills") -> list[str]:
    where = f"{skills}/{folder}/SKILL.md"
    found = FRONTMATTER.match(text)
    if not found:
        return [f"{where}: no frontmatter (a --- line, `name:` and `description:`, then a --- line)"]
    fields: dict[str, str] = {}
    problems = []
    for line in found.group(1).splitlines():
        if line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep or not key.strip() or key != key.strip():
            problems.append(f"{where}: frontmatter line isn't `key: value`: {line[:60]!r}")
            continue
        fields[key.strip()] = value.strip()
    if fields.get("name") != folder or not SKILL_NAME.match(folder):
        problems.append(f"{where}: `name` should be {folder!r}, its folder's name (lowercase letters, digits and -), "
                        f"not {fields.get('name')!r}")
    description = fields.get("description", "")
    if not description:
        problems.append(f"{where}: no `description` (it is what makes an agent use the skill)")
    elif len(description) > 1024 or "<" in description or ">" in description:
        problems.append(f"{where}: `description` is at most 1024 characters, without < or > ({len(description)} now)")
    if not found.group(2).strip():
        problems.append(f"{where}: no body after the frontmatter")
    for target in MD_LINK.findall(found.group(2)):
        if "://" not in target and not target.startswith("/") and not exists(target):
            problems.append(f"{where}: links to {target}, which isn't there")
    return problems


def exists_under(base: Path) -> Callable[[str], bool]:
    def exists(rel: str) -> bool:
        return (base / rel).exists()
    return exists


def check_skills(root: Path, directory: Path) -> list[str]:
    problems = []
    name = relative(root, directory)
    for skill in sorted(directory.glob("*/SKILL.md")):
        text = read(skill)
        if text is None:
            problems.append(f"{name}/{skill.parent.name}/SKILL.md: isn't UTF-8 text")
            continue
        problems += check_skill(skill.parent.name, text, exists_under(skill.parent), name)
    return problems


def read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def name_and_body(text: str) -> tuple[str | None, str]:
    found = FRONTMATTER.match(text)
    if not found:
        return None, text
    name = FRONT_NAME.search(found.group(1))
    return (name.group("name") if name else None), found.group(2)


def normalized(body: str) -> str:
    return " ".join(body.split())


def skill_files(root: Path) -> list[str]:
    try:
        listing = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root,
                                 check=True, capture_output=True).stdout.split(b"\0")
        names = [os.fsdecode(name) for name in listing]
    except (OSError, subprocess.CalledProcessError):
        names = [path.relative_to(root).as_posix() for path in root.rglob("SKILL.md")
                 if ".git" not in path.relative_to(root).parts]
    return sorted({name for name in names if name.rsplit("/", 1)[-1] == "SKILL.md" and (root / name).is_file()})


def exempt(path: str, ignore: list[str]) -> bool:
    if any(f"/{where}" in f"/{path}" for where in EXEMPT):
        return True
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in ignore)


def fleet_skills(directory: Path) -> dict[str, str]:
    found = {}
    own = agents_md.ROOT / vendor.SKILLS
    for skill in sorted(own.glob("*/SKILL.md")) if own.is_dir() else []:
        text = read(skill)
        if text is not None:
            found[skill.parent.name] = name_and_body(text)[1]
    for skill in sorted(directory.glob(f"{vendor.SKILL_PREFIX}*/SKILL.md")):
        text = read(skill)
        if text is not None and vendor.is_vendored_skill(text):
            found.setdefault(skill.parent.name.removeprefix(vendor.SKILL_PREFIX), name_and_body(text)[1])
    return found


def check_homes(root: Path, directory: Path, ignore: list[str]) -> list[str]:
    home = relative(root, directory)
    if not directory.is_dir():
        return [f"{home}: [tool.fleet] skills names a directory that isn't there"]
    problems = []
    for path in skill_files(root):
        parts = path.split("/")
        if exempt(path, ignore) or (len(parts) >= 2 and "/".join(parts[:-2]) == home and parts[-2]):
            continue
        problems.append(f"{path}: a skill lives at {home}/<name>/SKILL.md (fleet's under {home}/"
                        f"{vendor.SKILL_PREFIX}*, the app's own beside them), nowhere else; move it there or list it "
                        "in skill-homes-ignore")
    fleet = fleet_skills(directory)
    bodies = {normalized(body): name for name, body in fleet.items() if normalized(body)}
    for folder in sorted(path for path in directory.iterdir() if path.is_dir()):
        where = f"{home}/{folder.name}/SKILL.md"
        text = read(folder / "SKILL.md")
        if folder.name.startswith(vendor.SKILL_PREFIX):
            if text is None or not vendor.is_vendored_skill(text):
                problems.append(f"{where}: `{vendor.SKILL_PREFIX}` is reserved for vendored skills, and this one "
                                "lacks the line `fleet sync` writes; rename the app's own skill")
            continue
        if text is None:
            continue
        name, body = name_and_body(text)
        if name is not None and (name in fleet or name.removeprefix(vendor.SKILL_PREFIX) in fleet):
            problems.append(f"{where}: its name {name!r} is a fleet skill's; change the fleet skill in fleet, or give "
                            "the app's own another name")
        copied = bodies.get(normalized(body))
        if copied is not None:
            problems.append(f"{where}: its body is fleet's {copied!r} skill copied; fleet sync vendors it as "
                            f"{home}/{vendor.SKILL_PREFIX}{copied}, so delete the copy")
    return problems


def check_generated(name: str, committed: str | None, generated: str | None, command: str, fix: str = "",
                    why: str = "") -> list[str]:
    how = f"run {fix}" if fix else f"write what `{command}` prints to it"
    if generated is None:
        return [f"{name}: couldn't generate it to compare (`{command}`: {why})"]
    if committed is None:
        return [f"{name}: missing; {how} and commit it"]
    if committed != generated:
        return [f"{name} isn't what `{command}` generates: {how} and commit the result "
                "(edit what it is generated from, never the file)"]
    return []


def generate(root: Path, command: str) -> tuple[str | None, str]:
    try:
        done = subprocess.run(shlex.split(command), cwd=root, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, str(error)
    if done.returncode != 0:
        return None, (done.stderr or done.stdout).strip()[-200:] or f"exit code {done.returncode}"
    return done.stdout, ""


def check_generated_entries(root: Path, entries: list[dict[str, object]]) -> list[str]:
    problems = []
    for entry in entries:
        path, command, fix = entry.get("path"), entry.get("command"), entry.get("fix", "")
        if not isinstance(path, str) or not isinstance(command, str) or not isinstance(fix, str):
            raise ConfigError("[[tool.fleet.generated]] needs `path` and `command` strings (and `fix`, if given, a string)")
        generated, why = generate(root, command)
        target = root / path
        committed = target.read_text(encoding="utf-8") if target.is_file() else None
        problems += check_generated(path, committed, generated, command, fix, why)
    return problems


def check_feature_map(root: Path, module_path: str) -> list[str]:
    path = root / module_path
    if not path.is_file():
        return [f"{module_path}: [tool.fleet] feature-maps names a generator that isn't there"]
    name = f"fleet_feature_map_{abs(hash(path))}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return [f"{module_path}: can't be loaded as a Python module"]
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        text, problems = module.generate()
        out = Path(module.OUT)
        committed = out.read_text(encoding="utf-8") if out.exists() else None
        found: list[str] = [*problems, *module.check(committed, text)]
    finally:
        sys.modules.pop(name, None)
    return found


def applies(context: Context) -> bool:
    conf = context.config
    return any(conf.has(key) for key in ("skills", "generated", "feature-maps"))


def run(context: Context) -> list[str]:
    conf = context.config
    problems = []
    skills = conf.path("skills")
    if skills is not None:
        problems += check_skills(context.root, skills)
    problems += check_generated_entries(context.root, conf.tables("generated"))
    for module_path in conf.strings("feature-maps"):
        problems += check_feature_map(context.root, module_path)
    return problems


def homes_apply(context: Context) -> bool:
    return context.config.has("skills")


def homes(context: Context) -> list[str]:
    skills = context.config.path("skills")
    if skills is None:
        return []
    return check_homes(context.root, skills, context.config.strings("skill-homes-ignore"))


HOMES = Check("skill-homes", "each skill in its one home", homes_apply, homes)
CHECK = Check("skills", "skills well formed, generated references and feature maps current", applies, run)
