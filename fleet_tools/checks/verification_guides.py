import subprocess

from fleet_tools import verification_guides as guides
from fleet_tools.checks.context import Advisory, Check, Context, git

MAX_LISTED = 20
SKILL = "the fleet-verification-guide skill"


def escape(text: str) -> str:
    return guides.clean(text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"))


def changed(context: Context) -> list[str]:
    commits = context.commits
    if commits is None:
        return []
    out = git(context.root, "diff", "--name-only", "--no-renames", "--relative", "-z", commits.base, commits.head)
    return [path for path in out.split("\0") if path]


def under(path: str, relative: str) -> bool:
    return path == relative or path.startswith(relative.rstrip("/") + "/")


def stale(found: list[guides.Guide], files: list[str]) -> list[str]:
    return [escape(f"{guide.source}, line {glob.line}: the glob `{glob.text}` matches no tracked file, so the guide is stale; "
                   f"fix the glob or the guide ({SKILL} updates it)")
            for guide in found for glob in guide.paths if not any(glob.matches(path) for path in files)]


def uncovered(found: list[guides.Guide], paths: list[str], relative: str) -> list[str]:
    _, missing = guides.areas(found, [path for path in paths if not under(path, relative)])
    if not missing:
        return []
    listed = ", ".join(missing[:MAX_LISTED]) + (f" and {len(missing) - MAX_LISTED} more" if len(missing) > MAX_LISTED else "")
    return [Advisory(escape(f"no verification guide covers {len(missing)} changed path(s): {listed} ({SKILL} writes "
                            "or extends one)"))]


def checkout(context: Context) -> bool:
    try:
        return git(context.root, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except (subprocess.CalledProcessError, OSError):
        return False


def applies(context: Context) -> bool:
    return context.config.has(guides.KEY) and guides.directory(context.config) is not None and checkout(context)


def run(context: Context) -> list[str]:
    relative = guides.directory(context.config)
    if relative is None:
        return []
    found, errors = guides.load(context.root, relative)
    problems = [escape(str(error)) for error in errors]
    problems += stale(found, guides.tracked(context.root))
    return problems + uncovered(found, changed(context), relative)


CHECK = Check("verification-guides", "the verification guides", applies, run)
