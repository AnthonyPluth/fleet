import importlib.util
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

from fleet_tools.checks.context import Check, Context

ExtraCheck = Callable[[Context], Iterable[str]]


class ExtraChecksError(Exception):
    pass


def parse(spec: str) -> tuple[str, str]:
    module, sep, attribute = spec.partition(":")
    if not sep or not module or not attribute or not all(part.isidentifier() for part in module.split(".")) \
            or not attribute.isidentifier():
        raise ExtraChecksError(f"extra-checks: {spec!r} should be `package.module:NAME`, like 'tools.app_checks:CHECKS'")
    return module, attribute


def module_file(root: Path, module: str) -> Path:
    base = root.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    raise ExtraChecksError(f"extra-checks: no module {module} in {root} (looked for {base.relative_to(root)}.py "
                           f"and {base.relative_to(root)}/__init__.py)")


def _drop_consumer_modules(root: Path, before: set[str]) -> None:
    for name in set(sys.modules) - before:
        loaded = getattr(sys.modules[name], "__file__", None)
        if loaded and Path(loaded).resolve().is_relative_to(root.resolve()):
            del sys.modules[name]


def load(root: Path, spec: str) -> list[ExtraCheck]:
    module_name, attribute = parse(spec)
    path = module_file(root, module_name)
    before = set(sys.modules)
    sys.path.insert(0, str(root))
    try:
        loader_spec = importlib.util.spec_from_file_location(module_name, path)
        if loader_spec is None or loader_spec.loader is None:
            raise ExtraChecksError(f"extra-checks: {path.relative_to(root)} can't be loaded as a Python module")
        module: ModuleType = importlib.util.module_from_spec(loader_spec)
        sys.modules[module_name] = module
        try:
            loader_spec.loader.exec_module(module)
        except Exception as error:
            raise ExtraChecksError(f"extra-checks: importing {module_name} from {root} failed: "
                                   f"{type(error).__name__}: {error}") from error
        checks: Any = getattr(module, attribute, None)
    finally:
        sys.path.remove(str(root))
        _drop_consumer_modules(root, before)
        sys.modules.pop(module_name, None)
    if checks is None:
        raise ExtraChecksError(f"extra-checks: {module_name} has no {attribute}")
    if not isinstance(checks, list | tuple) or not all(callable(check) for check in checks):
        raise ExtraChecksError(f"extra-checks: {module_name}.{attribute} should be a list of callables")
    return list(checks)


def run_one(check: ExtraCheck, context: Context) -> list[str]:
    name = getattr(check, "__name__", repr(check))
    try:
        found = check(context)
    except Exception as error:
        return [f"extra check {name} failed: {type(error).__name__}: {error}"]
    if isinstance(found, str) or found is None:
        return [f"extra check {name} should return a list of problems (strings), not {type(found).__name__}"]
    return [str(problem) for problem in found]


def applies(context: Context) -> bool:
    return context.config.has("extra-checks")


def run(context: Context) -> list[str]:
    spec = context.config.string("extra-checks")
    assert spec is not None
    try:
        checks = load(context.root, spec)
    except ExtraChecksError as error:
        return [str(error)]
    return [problem for check in checks for problem in run_one(check, context)]


CHECK = Check("extra-checks", "the app's own checks", applies, run)
