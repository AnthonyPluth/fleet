import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from fleet_tools import agents_md

SKILLS = Path("plugins/fleet/skills")
FILES = {
    Path("plugins/fleet/agents/fleet-reviewer.md"): Path(".claude/agents/fleet-reviewer.md"),
    Path("plugins/fleet/hooks/session-start.sh"): Path(".claude/hooks/fleet-session-start.sh"),
    Path("shim/fleet"): Path("tools/fleet"),
}
EXECUTABLE = {Path(".claude/hooks/fleet-session-start.sh"), Path("tools/fleet")}
SKILL_PREFIX = "fleet-"
RESERVED = (".claude/skills/fleet-*/**/*", ".claude/agents/fleet-*", ".claude/hooks/fleet-*")
NOTE = "vendored from fleet {version} ({source}) by `fleet sync`; edit it in fleet, not here: `fleet drift` fails on any change"
NOTED = re.compile(r"\A(?:#![^\n]*\n|---\n)?(?:# |<!-- )vendored from fleet v\d+\.\d+\.\d+ ")
NAME = re.compile(r"^name: (?P<name>[a-z0-9-]+)$", re.MULTILINE)
SKILL_NOTE = re.compile("# " + re.escape(NOTE).replace(re.escape("{version}"), r"v\d+\.\d+\.\d+")
                        .replace(re.escape("{source}"), re.escape(SKILLS.as_posix()) + r"/[a-z0-9-]+/SKILL\.md"))


class VendorError(Exception):
    pass


@dataclass(frozen=True)
class Vendored:
    target: Path
    source: Path
    text: str
    executable: bool


def note(text: str, version: str, source: Path) -> str:
    line = NOTE.format(version=version, source=source.as_posix())
    if text.startswith(("#!", "---\n")):
        first, _, rest = text.partition("\n")
        return f"{first}\n# {line}\n{rest}"
    if source.suffix == ".md":
        return f"<!-- {line} -->\n{text}"
    return f"# {line}\n{text}"


def rename_skill(text: str, name: str, source: Path) -> str:
    front = agents_md.FRONT_MATTER.match(text)
    found = NAME.search(front.group(0)) if front else None
    if not found or found.group("name") != name:
        raise VendorError(f"{source.as_posix()}: its front matter's `name` must be {name!r}, its folder's name")
    return text[:found.start("name")] + SKILL_PREFIX + name + text[found.end("name"):]


def target_of(source: Path) -> Path | None:
    if source in FILES:
        return FILES[source]
    if source.parts[:len(SKILLS.parts)] == SKILLS.parts and len(source.parts) > len(SKILLS.parts) + 1:
        skill = source.parts[len(SKILLS.parts)]
        return Path(".claude/skills", SKILL_PREFIX + skill, *source.parts[len(SKILLS.parts) + 1:])
    return None


def render(sources: dict[Path, str], version: str) -> dict[Path, Vendored]:
    found = {}
    for source, text in sorted(sources.items()):
        target = target_of(source)
        if target is None:
            continue
        if source.name == "SKILL.md" and target.parent.parent == Path(".claude/skills"):
            text = rename_skill(text, source.parent.name, source)
        found[target] = Vendored(target, source, note(text, version, source), target in EXECUTABLE)
    return found


def working_tree(root: Path) -> dict[Path, str]:
    paths = [path for path in sorted((root / SKILLS).rglob("*")) if path.is_file()]
    paths += [root / source for source in FILES if (root / source).is_file()]
    return {path.relative_to(root): path.read_text(encoding="utf-8") for path in paths}


def tagged(version: str, root: Path) -> dict[Path, str] | None:
    if not (root / ".git").exists():
        return None
    try:
        listing = subprocess.run(["git", "ls-tree", "-r", "--name-only", version, "--", SKILLS.as_posix(),
                                  *(source.as_posix() for source in FILES)], cwd=root, check=True,
                                 capture_output=True, text=True).stdout.split("\n")
        return {Path(name): subprocess.run(["git", "show", f"{version}:{name}"], cwd=root, check=True,
                                           capture_output=True, text=True).stdout for name in listing if name}
    except (subprocess.CalledProcessError, OSError):
        return None


def shipped_at(version: str, root: Path | None = None) -> dict[Path, Vendored] | None:
    root = root or agents_md.ROOT
    sources = tagged(version, root)
    if sources is None and version == agents_md.own_version():
        sources = working_tree(root)
    return None if sources is None else render(sources, version)


def reserved(root: Path) -> list[Path]:
    found = {path.relative_to(root) for pattern in RESERVED for path in root.glob(pattern) if path.is_file()}
    return sorted(found)


def is_vendored(path: Path) -> bool:
    try:
        return bool(NOTED.match(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError):
        return False


def is_vendored_skill(text: str) -> bool:
    lines = text.split("\n", 2)
    return len(lines) > 2 and lines[0] == "---" and bool(SKILL_NOTE.fullmatch(lines[1]))


def sync(root: Path, version: str) -> list[str]:
    shipped = shipped_at(version)
    if shipped is None:
        raise VendorError(f"can't read fleet's vendored files at {version}")
    changes = []
    for target, vendored in shipped.items():
        path = root / target
        current = path.read_bytes() if path.is_file() else None
        if current != vendored.text.encode("utf-8"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(vendored.text.encode("utf-8"))
            changes.append(f"{target.as_posix()}: {'wrote' if current is None else 'replaced'} ({vendored.source.as_posix()})")
        if vendored.executable and not path.stat().st_mode & 0o100:
            path.chmod(path.stat().st_mode | 0o755)
            changes.append(f"{target.as_posix()}: made executable")
    for target in reserved(root):
        if target not in shipped and is_vendored(root / target):
            (root / target).unlink()
            changes.append(f"{target.as_posix()}: removed (fleet {version} no longer ships it)")
            parent = (root / target).parent
            while parent != root and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
    return changes


def check(root: Path, version: str) -> tuple[int, list[str]]:
    shipped = shipped_at(version)
    if shipped is None:
        return 0, [f"can't read fleet's vendored files at {version} from fleet {agents_md.own_version()}; run fleet "
                   f"drift from fleet at {version}"]
    if not shipped:
        return 0, []
    problems = []
    for target, vendored in shipped.items():
        path = root / target
        name = target.as_posix()
        if not path.is_file():
            problems.append(f"{name} doesn't exist (fleet sync --to {version} vendors it)")
        elif path.read_bytes() != vendored.text.encode("utf-8"):
            problems.append(f"{name} differs from fleet's at {version}; edit it in fleet ({vendored.source.as_posix()}), "
                            "not here, or run `fleet sync`")
        elif vendored.executable and not path.stat().st_mode & 0o100:
            problems.append(f"{name} isn't executable (fleet sync makes it so)")
    for target in reserved(root):
        if target not in shipped:
            why = ("fleet sync removes it" if is_vendored(root / target)
                   else "the fleet- prefix is fleet's: rename the app's own file")
            problems.append(f"{target.as_posix()} isn't a file fleet {version} vendors ({why})")
    return len(shipped), problems
