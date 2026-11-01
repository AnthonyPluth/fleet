import importlib.util
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from fleet_tools.checks.context import Check, Context, relative
from fleet_tools.config import ConfigError

SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)
MD_LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
TIMEOUT = 120


def check_skill(folder: str, text: str, exists: Callable[[str], bool] = lambda rel: True,
                skills: str = ".claude/skills") -> list[str]:
    where = f"{skills}/{folder}/SKILL.md"
    found = FRONTMATTER.match(text)
    if not found:
        return [f"{where}: no frontmatter (a --- line, `name:` and `description:`, then a --- line)"]
    fields: dict[str, str] = {}
    problems = []
    for line in found.group(1).splitlines():
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
        problems += check_skill(skill.parent.name, skill.read_text(encoding="utf-8"), exists_under(skill.parent), name)
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


CHECK = Check("skills", "skills well formed, generated references and feature maps current", applies, run)
