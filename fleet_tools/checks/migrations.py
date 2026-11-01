import ast
import re
from dataclasses import dataclass
from pathlib import Path

from fleet_tools.checks.context import Check, Context, relative

REVISION = re.compile(r"^\d{4}$")
DEFAULT_TESTS = "tests/test_migrations.py"
DEFAULT_TESTED_FROM = "0001"


@dataclass(frozen=True)
class Migration:
    file: str
    revision: object
    down: tuple[object, ...]


def _assigned(tree: ast.Module, name: str) -> object:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        value = node.value
        if any(isinstance(target, ast.Name) and target.id == name for target in targets) and value is not None:
            return ast.literal_eval(value)
    return ...


def migrations(directory: Path) -> list[Migration]:
    found = []
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("__"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        revision, down = _assigned(tree, "revision"), _assigned(tree, "down_revision")
        if down is None or down is ...:
            downs: tuple[object, ...] = ()
        elif isinstance(down, list | tuple):
            downs = tuple(down)
        else:
            downs = (down,)
        found.append(Migration(path.name, revision, downs))
    return found


def check_migrations(found: list[Migration], tests_text: str, tested_from: str = DEFAULT_TESTED_FROM,
                     tests_name: str = DEFAULT_TESTS) -> list[str]:
    problems = []
    revisions = [m.revision for m in found]
    for m in found:
        rev = m.revision
        if not isinstance(rev, str) or not REVISION.match(rev):
            problems.append(f"{m.file}: revision should be four digits, like '0041' (it is {rev!r})")
        elif not m.file.startswith(rev + "_"):
            problems.append(f"{m.file}: the file name should start with its revision, {rev}_")
        if revisions.count(rev) > 1 and m is next(x for x in found if x.revision == rev):
            files = ", ".join(x.file for x in found if x.revision == rev)
            problems.append(f"revision {rev!r} is used more than once ({files}): renumber yours after main's newest")
        for d in m.down:
            if d not in revisions:
                problems.append(f"{m.file}: down_revision {d!r} is no migration here")
    followed = {d for m in found for d in m.down}
    heads = sorted({str(m.revision) for m in found if m.revision not in followed})
    if len(heads) > 1:
        problems.append(f"more than one Alembic head ({', '.join(heads)}): another branch added a migration "
                        "with the same parent; merge main and make yours follow its newest")
    if not heads and found:
        problems.append("no Alembic head: the migrations go round in a loop")
    tested = set(re.findall(r"^\s*def test_(\d{4})_", tests_text, re.MULTILINE))
    for m in found:
        rev = m.revision
        if isinstance(rev, str) and REVISION.match(rev) and rev >= tested_from and rev not in tested:
            problems.append(f"{m.file}: no test of its own in {tests_name} (a method named test_{rev}_...); "
                            "downgrade to the revision before it, write the data it changes, upgrade and check the result")
    return problems


def applies(context: Context) -> bool:
    return context.config.has("migrations")


def run(context: Context) -> list[str]:
    conf = context.config
    directory = conf.path("migrations")
    assert directory is not None
    if not directory.is_dir():
        return [f"{relative(context.root, directory)}: [tool.fleet] migrations names a directory that isn't there"]
    tests = conf.path("migration-tests") or context.root / DEFAULT_TESTS
    tested_from = conf.string("tested-from", DEFAULT_TESTED_FROM) or DEFAULT_TESTED_FROM
    tests_text = tests.read_text(encoding="utf-8") if tests.is_file() else ""
    return check_migrations(migrations(directory), tests_text, tested_from, relative(context.root, tests))


CHECK = Check("migrations", "one migration head, migrations tested", applies, run)
