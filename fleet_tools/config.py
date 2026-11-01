import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PYPROJECT = "pyproject.toml"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    root: Path
    table: dict[str, Any]

    def has(self, key: str) -> bool:
        return key in self.table

    def get(self, key: str, default: Any = None) -> Any:
        return self.table.get(key, default)

    def string(self, key: str, default: str | None = None) -> str | None:
        value = self.table.get(key, default)
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"[tool.fleet] {key} must be a string")
        return value

    def strings(self, key: str) -> list[str]:
        value = self.table.get(key, [])
        if isinstance(value, str):
            return [value]
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return list(value)
        raise ConfigError(f"[tool.fleet] {key} must be a string or a list of strings")

    def section(self, key: str) -> dict[str, Any]:
        value = self.table.get(key, {})
        if not isinstance(value, dict):
            raise ConfigError(f"[tool.fleet.{key}] must be a table")
        return value

    def tables(self, key: str) -> list[dict[str, Any]]:
        value = self.table.get(key, [])
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return list(value)
        raise ConfigError(f"[[tool.fleet.{key}]] must be an array of tables")

    def path(self, key: str) -> Path | None:
        value = self.string(key)
        return None if value is None else self.root / value

    def paths(self, key: str) -> list[Path]:
        return [self.root / value for value in self.strings(key)]


def find_root(start: Path) -> Path:
    start = start.resolve()
    for directory in (start, *start.parents):
        if (directory / PYPROJECT).is_file():
            return directory
    raise ConfigError(f"no {PYPROJECT} in {start} or any directory above it")


def load(root: Path) -> Config:
    pyproject = root / PYPROJECT
    try:
        with pyproject.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError:
        raise ConfigError(f"{pyproject} doesn't exist") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{pyproject} isn't valid TOML: {error}") from None
    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        raise ConfigError(f"{pyproject}: [tool] must be a table")
    table = tool.get("fleet", {})
    if not isinstance(table, dict):
        raise ConfigError(f"{pyproject}: [tool.fleet] must be a table")
    return Config(root=root, table=table)
