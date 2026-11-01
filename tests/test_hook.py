import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path

from consumers import RUNWAY, WAYPOINT, copy, temporary, write

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "plugins/fleet/hooks/session-start.sh"
VENDORED = ".claude/hooks/fleet-session-start.sh"
TOOLS = ("bash", "cat", "cut", "rm", "mkdir", "touch", "cp", "chmod", "basename", "pwd", "dirname")
UV, POETRY = "uv==0.12.21", "poetry==2.5.1"
NPM_FLAGS = "--no-audit --no-fund"
INSTALL = "--no-save --no-audit --no-fund"

STUBS = {
    "uv": """
log uv "$@"
if [ "$1 $2" = "python find" ]; then
  if [ -f "$STATE/python-$3" ]; then echo "$STATE/pythons/python$3"; else exit 2; fi
elif [ "$1 $2" = "python install" ]; then
  touch "$STATE/python-$3"
  mkdir -p "$STATE/pythons"
  cp "$STUBS/fake-python" "$STATE/pythons/python$3"
fi
""",
    "poetry": """
log poetry "$@"
if [ "$1 $2" = "env use" ] && [ ! -d "$STATE/venv" ]; then
  mkdir -p "$STATE/venv/bin"
  printf '#!/bin/bash\\necho 3.11\\n' > "$STATE/venv/bin/python"
  chmod +x "$STATE/venv/bin/python"
elif [ "$1 $2" = "env info" ]; then
  echo "$STATE/venv"
fi
""",
    "fake-python": """
log "$(basename "$0")" "$@"
if [ "$1 $2" = "-m venv" ]; then
  mkdir -p "$3/bin"
  version=$(basename "$0")
  printf '#!/bin/bash\\necho %s\\n' "${version#python}" > "$3/bin/python"
  chmod +x "$3/bin/python"
fi
""",
    "python3": """
log python3 "$@"
if [ "$1 $2 $3" = "-m pip install" ]; then
  tool=${6%%==*}
  mkdir -p "$HOME/.local/bin"
  cp "$STUBS/$tool" "$HOME/.local/bin/$tool"
fi
""",
    "npm": """
log "npm@$(basename "$PWD")" "$@"
if [ "$1" = "ci" ]; then
  mkdir -p node_modules/esrap/types
  touch node_modules/esrap/types/public.d.ts "$STATE/node-ok"
fi
""",
    "node": """
log node "$@"
test -f "$STATE/node-ok"
""",
    "dpkg": """
log dpkg "$@"
test -f "$STATE/apt-$2"
""",
    "apt-get": """
log apt-get "$@"
if [ -f "$STATE/apt-fails" ]; then exit 100; fi
for package in "$@"; do touch "$STATE/apt-$package"; done
""",
    "sudo": """
log sudo "$@"
if [ -f "$STATE/sudo-fails" ]; then exit 1; fi
shift
for package in "$@"; do touch "$STATE/apt-$package"; done
""",
}
HEADER = '#!/bin/bash\nlog() { echo "$*" >> "$LOG"; }\n'


class Container:
    def __init__(self, test: unittest.TestCase, consumer: Path, present: tuple[str, ...]) -> None:
        base = temporary(test)
        self.consumer, self.home, self.state = consumer, base / "home", base / "state"
        self.stubs, self.path, self.log = base / "stubs", base / "path", base / "calls.log"
        for directory in (self.home, self.state, self.stubs, self.path):
            directory.mkdir()
        for name, body in STUBS.items():
            stub = self.stubs / name
            stub.write_text(HEADER + body.lstrip("\n"), encoding="utf-8")
            stub.chmod(0o755)
        for name in present:
            os.symlink(self.stubs / name, self.path / name)
        for tool in TOOLS:
            found = shutil.which(tool)
            assert found, tool
            os.symlink(found, self.path / tool)

    def run(self, remote: bool = True) -> tuple[subprocess.CompletedProcess[str], list[str], float]:
        if self.log.exists():
            self.log.unlink()
        env = {"PATH": str(self.path), "HOME": str(self.home), "STATE": str(self.state), "STUBS": str(self.stubs),
               "LOG": str(self.log), "CLAUDE_PROJECT_DIR": str(self.consumer)}
        if remote:
            env["CLAUDE_CODE_REMOTE"] = "true"
        started = time.monotonic()
        done = subprocess.run([str(self.consumer / VENDORED)], env=env, capture_output=True, text=True, check=False,
                              timeout=60)
        elapsed = time.monotonic() - started
        calls = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        return done, [call.replace(str(self.state), "$STATE") for call in calls], elapsed


def python_steps(fresh: bool) -> list[str]:
    if not fresh:
        return ["uv python find 3.14", "poetry env use --quiet $STATE/pythons/python3.14", "poetry env info --path",
                "poetry install --no-root --no-interaction"]
    return [
        f"python3 -m pip install --quiet --user {UV}",
        "uv python find 3.14",
        "uv python install 3.14",
        "uv python find 3.14",
        f"python3 -m pip install --quiet --user {POETRY}",
        "poetry env use --quiet $STATE/pythons/python3.14",
        "poetry env info --path",
        "python3.14 -m venv $STATE/venv",
        "poetry install --no-root --no-interaction",
    ]


PRESENT = ("python3", "npm", "node", "dpkg", "apt-get", "sudo")


class Apps(unittest.TestCase):
    def test_waypoints_setup_then_a_quick_second_run(self) -> None:
        container = Container(self, copy(self, WAYPOINT, repository=False), PRESENT)
        done, calls, _ = container.run()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(calls, [*python_steps(fresh=True), "dpkg -s libfaketime",
                                 "apt-get install -y --no-install-recommends libfaketime",
                                 f"npm@frontend install {INSTALL}"])
        done, calls, elapsed = container.run()
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(calls, [*python_steps(fresh=False), "dpkg -s libfaketime", f"npm@frontend install {INSTALL}"])
        self.assertLess(elapsed, 5)

    def test_runways_setup_reinstalls_an_incomplete_node_modules_once(self) -> None:
        container = Container(self, copy(self, RUNWAY, repository=False), PRESENT)
        done, calls, _ = container.run()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(calls, [*python_steps(fresh=True), "dpkg -s libfaketime",
                                 "apt-get install -y --no-install-recommends libfaketime",
                                 f"npm@frontend install {INSTALL}", f"npm@frontend ci {NPM_FLAGS}",
                                 "node --input-type=module -e await import(process.argv[1]) eslint-plugin-svelte"])
        self.assertIn("frontend/node_modules is incomplete; reinstalling from the lock file", done.stderr)
        done, calls, elapsed = container.run()
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(calls, [*python_steps(fresh=False), "dpkg -s libfaketime", f"npm@frontend install {INSTALL}",
                                 "node --input-type=module -e await import(process.argv[1]) eslint-plugin-svelte"])
        self.assertLess(elapsed, 5)

    def test_tools_already_in_the_container_are_used(self) -> None:
        container = Container(self, copy(self, WAYPOINT, repository=False), (*PRESENT, "uv", "poetry"))
        _, calls, _ = container.run()
        self.assertNotIn(f"python3 -m pip install --quiet --user {UV}", calls)
        self.assertNotIn(f"python3 -m pip install --quiet --user {POETRY}", calls)


class Config(unittest.TestCase):
    def container(self, conf: str | None, local: str | None = None) -> Container:
        root = copy(self, WAYPOINT, repository=False)
        if conf is None:
            (root / ".claude/hooks/fleet.conf").unlink()
        else:
            write(root, ".claude/hooks/fleet.conf", conf)
        if local is not None:
            write(root, ".claude/hooks/session-start.local.sh", local)
        return Container(self, root, (*PRESENT, "uv", "poetry"))

    def test_a_local_session_does_nothing(self) -> None:
        done, calls, _ = self.container('PYTHON_VERSION=3.14\n').run(remote=False)
        self.assertEqual((done.returncode, calls), (0, []))

    def test_no_config_does_nothing(self) -> None:
        done, calls, _ = self.container(None).run()
        self.assertEqual((done.returncode, calls), (0, []))

    def test_the_local_script_is_sourced_last_with_the_config_set(self) -> None:
        done, calls, _ = self.container('APT_PACKAGES="libfaketime"\n',
                                        'log() { echo "$*" >> "$LOG"; }\nlog local "$APT_PACKAGES"\n').run()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(calls[-1], "local libfaketime")

    def test_apt_falls_back_to_sudo_and_a_failure_only_warns(self) -> None:
        container = self.container('APT_PACKAGES="libfaketime exiftool"\n')
        (container.state / "apt-fails").touch()
        _, calls, _ = container.run()
        self.assertEqual(calls[-1], "sudo apt-get install -y --no-install-recommends libfaketime exiftool")
        (container.state / "apt-libfaketime").unlink()
        (container.state / "sudo-fails").touch()
        done, _, _ = container.run()
        self.assertEqual(done.returncode, 0)
        self.assertIn("session-start: libfaketime isn't installed", done.stderr)

    def test_node_check_tells_packages_from_files(self) -> None:
        container = self.container('FRONTEND_DIRS="frontend"\nNODE_CHECK="plain @scope/pkg @scope/pkg/types.d.ts"\n')
        (container.state / "node-ok").touch()
        done, calls, _ = container.run()
        self.assertEqual([call.split()[-1] for call in calls if call.startswith("node ")], ["plain", "@scope/pkg"] * 2)
        self.assertIn("frontend/node_modules is still incomplete", done.stderr)


class Source(unittest.TestCase):
    def test_the_vendored_hook_is_the_plugins(self) -> None:
        text = HOOK.read_text(encoding="utf-8")
        for consumer in (WAYPOINT, RUNWAY):
            vendored = (consumer / VENDORED).read_text(encoding="utf-8").splitlines(keepends=True)
            self.assertEqual(vendored[0] + "".join(vendored[2:]), text)

    def test_uv_is_pinned_alike_in_the_hook_and_the_shim(self) -> None:
        self.assertIn(f'UV_VERSION="{UV.split("==")[1]}"', HOOK.read_text(encoding="utf-8"))
        self.assertIn(f'"{UV}"', (ROOT / "shim/fleet").read_text(encoding="utf-8"))
