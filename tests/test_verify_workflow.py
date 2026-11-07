import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fleet_tools import cli, self_check

VERIFY = cli.ROOT / ".github" / "workflows" / "verify.yml"
SELF_TEST = cli.ROOT / ".github" / "workflows" / "self-test.yml"
FIXTURE_MAKEFILE = cli.ROOT / "tests" / "fixtures" / "verify" / "Makefile"
ADOPTING = cli.ROOT / "docs" / "adopting.md"
CHANGELOG = cli.ROOT / "CHANGELOG.md"
INSTALL = "Install the system packages"
REJECTED = ("::error::apt-packages must be Debian package names separated by single spaces (lower case letters, digits, '.', "
            "'+' and '-', each starting with a letter or digit and not ending in '-', which apt-get reads as remove)\n")
FAKE_SUDO = '#!/bin/sh\nprintf "%s\\n" "--- $*" >> "$SUDO_LOG"\nfor arg in "$@"; do printf "[%s]\\n" "$arg" >> "$SUDO_LOG"; done\n'
FAKE_APT_CACHE = ('#!/bin/sh\n[ "$1" = pkgnames ] && [ "$2" = -- ] && [ "$#" = 3 ] || exit 2\n'
                  'for name in $KNOWN; do case "$name" in "$3"*) echo "$name";; esac; done\n'
                  'i=0; while [ "$i" -lt 4000 ]; do echo "$3-padding-name-that-is-long-enough-$i"; i=$((i + 1)); done\n')
KNOWN = ("libfaketime libimage-exiftool-perl libheif1 libheif-plugin-libde265 a-b g++ g++-13 libfoo+bar libstdc++6 "
         "python3 python3-clang-20 python3.12-venv python3.12-venv-extra x hello")
UNKNOWN = ("::error::apt-packages names a package apt doesn't know; each name must be exactly a package's, since apt-get "
           "reads an unknown name with '.' or '+' as a pattern\n")
Node = dict[str, Any]


def parsed(path: Path) -> Node:
    workflow = self_check.parse(path.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def inputs() -> Node:
    found = parsed(VERIFY)["on"]["workflow_call"]["inputs"]
    assert isinstance(found, dict)
    return found


def steps() -> list[Node]:
    found = parsed(VERIFY)["jobs"]["verify"]["steps"]
    assert isinstance(found, list)
    return found


def step(name: str) -> Node:
    hits = [each for each in steps() if each.get("name") == name]
    assert len(hits) == 1, (name, hits)
    return hits[0]


def run_block(name: str) -> str:
    text = VERIFY.read_text(encoding="utf-8")
    found = re.search(rf"- name: {re.escape(name)}\n.*?run: \|\n(.*?)(?=^\n|^      - |^  \w|\Z)", text, re.MULTILINE | re.DOTALL)
    assert found, name
    return "\n".join(line[10:] for line in found.group(1).splitlines())


def install(packages: str, faketime: str = "true") -> tuple[subprocess.CompletedProcess[str], str]:
    with tempfile.TemporaryDirectory() as root:
        bin_dir = Path(root) / "bin"
        bin_dir.mkdir()
        sudo = bin_dir / "sudo"
        sudo.write_text(FAKE_SUDO, encoding="utf-8")
        sudo.chmod(0o755)
        apt_cache = bin_dir / "apt-cache"
        apt_cache.write_text(FAKE_APT_CACHE, encoding="utf-8")
        apt_cache.chmod(0o755)
        log = Path(root) / "sudo.log"
        env = {"NEEDS_FAKETIME": faketime, "APT_PACKAGES": packages, "SUDO_LOG": str(log), "KNOWN": KNOWN, "PATH": f"{bin_dir}:/usr/bin:/bin"}
        result = subprocess.run(["bash", "-eo", "pipefail", "-c", run_block(INSTALL)], env=env, cwd=root,
                                capture_output=True, text=True, check=False)
        return result, log.read_text(encoding="utf-8") if log.exists() else ""


def calls(log: str) -> list[list[str]]:
    found: list[list[str]] = []
    for line in log.splitlines():
        if line.startswith("--- "):
            found.append([])
        else:
            found[-1].append(line[1:-1])
    return found


class InputTest(unittest.TestCase):
    def test_apt_packages_is_optional_and_empty_by_default(self) -> None:
        spec = inputs()["apt-packages"]
        self.assertEqual(spec["type"], "string")
        self.assertEqual(spec["default"], "")
        self.assertNotIn("required", spec)

    def test_the_other_defaults_are_unchanged(self) -> None:
        defaults = {name: spec.get("default") for name, spec in inputs().items() if name != "make-target"}
        self.assertEqual(defaults, {
            "working-directory": ".", "needs-faketime": "true", "apt-packages": "", "timeout-minutes": "15",
            "runs-on": '"ubuntu-latest"', "python-version": "3.14", "poetry-version": "2.5.1", "poetry-install-args": "",
            "node-dir": "frontend", "node-version-file": ".nvmrc", "browser": "chromium", "fleet-ref": ""})
        self.assertEqual(inputs()["make-target"]["required"], "true")

    def test_the_value_reaches_the_script_only_through_the_environment(self) -> None:
        install_step = step(INSTALL)
        self.assertEqual(install_step["env"], {"NEEDS_FAKETIME": "${{ inputs.needs-faketime }}",
                                               "APT_PACKAGES": "${{ inputs.apt-packages }}"})
        self.assertEqual(install_step["if"], "inputs.needs-faketime || inputs.apt-packages != ''")
        script = run_block(INSTALL)
        self.assertNotIn("${{", script)
        self.assertNotIn("eval", script)
        self.assertIn('sudo apt-get install -yq "${packages[@]}"', script)

    def test_no_input_reaches_any_script_through_an_expression(self) -> None:
        scripts = re.findall(r"^        run: (.*?)(?=^      - |^\n|\Z)", VERIFY.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)
        self.assertEqual(len(scripts), 7)
        for script in scripts:
            self.assertNotIn("${{", script)

    def test_there_is_one_apt_step_and_it_runs_before_the_target(self) -> None:
        text = VERIFY.read_text(encoding="utf-8")
        self.assertEqual(text.count("sudo apt-get update"), 1)
        self.assertEqual(text.count("sudo apt-get install"), 1)
        names = [each.get("name") for each in steps()]
        self.assertLess(names.index(INSTALL), names.index("make ${{ inputs.make-target }}"))


class InstallTest(unittest.TestCase):
    def test_the_default_installs_libfaketime_exactly_as_before(self) -> None:
        result, log = install("")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls(log), [["apt-get", "update", "-q"], ["apt-get", "install", "-yq", "libfaketime"]])

    def test_the_packages_join_libfaketime_in_the_same_install_as_separate_arguments(self) -> None:
        result, log = install("libimage-exiftool-perl libheif1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls(log), [["apt-get", "update", "-q"],
                                      ["apt-get", "install", "-yq", "libfaketime", "libimage-exiftool-perl", "libheif1"]])

    def test_without_faketime_only_the_packages_are_installed(self) -> None:
        result, log = install("libstdc++6 python3.12-venv", faketime="false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls(log)[1], ["apt-get", "install", "-yq", "libstdc++6", "python3.12-venv"])

    def test_names_with_inner_hyphens_dots_and_plus_signs_are_accepted(self) -> None:
        result, log = install("a-b g++ libfoo+bar libstdc++6 python3.12-venv x")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls(log)[1], ["apt-get", "install", "-yq", "libfaketime", "a-b", "g++", "libfoo+bar", "libstdc++6",
                                         "python3.12-venv", "x"])

    def test_a_real_name_listed_before_more_than_a_pipe_buffer_of_others_is_accepted(self) -> None:
        listed = subprocess.run(["sh", "-c", FAKE_APT_CACHE, "apt-cache", "pkgnames", "--", "hello"], env={"KNOWN": KNOWN},
                                capture_output=True, text=True, check=True).stdout
        self.assertTrue(listed.startswith("hello\n"))
        self.assertGreater(len(listed), 128 * 1024)
        result, log = install("hello libheif1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls(log)[1], ["apt-get", "install", "-yq", "libfaketime", "hello", "libheif1"])

    def test_a_name_that_is_not_exactly_a_package_fails_after_the_update_and_before_the_install(self) -> None:
        for value in ("python3.", "libheif1 python3.", "g+", "python3.12", "libheif", "libstdc++", "nosuch", "hello x.y"):
            with self.subTest(value=value):
                result, log = install(value)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls(log), [["apt-get", "update", "-q"]])
                self.assertEqual(result.stdout, UNKNOWN)

    def test_a_value_that_is_not_package_names_fails_before_apt_get_and_is_not_echoed(self) -> None:
        for value in ("exiftool; touch pwned", "$(touch pwned)", "`touch pwned`", "a|b", "a && b", "a>b", "a\nb", "a\tb",
                      "-o APT::Get::AllowUnauthenticated=true", "--allow-unauthenticated", "a  b", " a", "a ", "Exiftool",
                      "../x", "a/b", "a*", "a?", "a'b", 'a"b', "a\\b", "a:amd64", "a=1.0", "::error::x", "a${IFS}b",
                      "hello-", "bash-", "a-b-", "libheif1 bash-", "-", "a -"):
            with self.subTest(value=value):
                result, log = install(value)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(log, "")
                self.assertEqual(result.stdout, REJECTED)


class SelfTestTest(unittest.TestCase):
    def test_the_self_test_installs_two_packages_and_its_target_checks_them(self) -> None:
        job = parsed(SELF_TEST)["jobs"]["verify"]
        self.assertEqual(job["uses"], "./.github/workflows/verify.yml")
        self.assertEqual(job["with"]["fleet-ref"], "${{ github.sha }}")
        packages = job["with"]["apt-packages"].split(" ")
        self.assertEqual(len(packages), 2)
        self.assertIn(f"dpkg -s libfaketime {' '.join(packages)}", FIXTURE_MAKEFILE.read_text(encoding="utf-8"))


class DocsTest(unittest.TestCase):
    def section(self) -> str:
        text = ADOPTING.read_text(encoding="utf-8")
        return text.split("## Verify (control smoke, verify flows)\n")[1].split("\n## ")[0]

    def test_every_input_is_in_the_table(self) -> None:
        rows = re.findall(r"^\| `([a-z-]+)` \|", self.section(), re.MULTILINE)
        self.assertEqual(rows, list(inputs()))
        self.assertRegex(self.section(), r"\| `apt-packages` \| none \| .*Debian package names")

    def test_darkrooms_caller_passes_its_packages(self) -> None:
        section = self.section()
        self.assertIn("apt-packages: libimage-exiftool-perl libheif1", section)
        self.assertIn("node-version-file: frontend/.nvmrc", section)

    def test_the_changelog_lists_it_under_added(self) -> None:
        text = CHANGELOG.read_text(encoding="utf-8")
        added = [section.split("\n### Added\n")[1].split("\n### ")[0] for section in text.split("\n## [")[1:] if "\n### Added\n" in section]
        entries = [line for part in added for line in part.splitlines() if line.startswith("- The verify workflow takes other Debian packages")]
        self.assertEqual(len(entries), 1)
        self.assertIn("`apt-packages`", entries[0])
        self.assertEqual(text.count("- The verify workflow takes other Debian packages"), 1)

if __name__ == "__main__":
    unittest.main()
