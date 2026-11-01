import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fleet_tools import cli, self_check

SECURITY = cli.ROOT / ".github" / "workflows" / "security.yml"
SELF_TEST = cli.ROOT / ".github" / "workflows" / "self-test.yml"
Node = dict[str, Any]


def parsed(path: Path) -> Node:
    workflow = self_check.parse(path.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def audit_step() -> str:
    text = SECURITY.read_text(encoding="utf-8")
    found = re.search(r"- name: Audit the workflows\n(.*?)(?=^      - |\Z)", text, re.MULTILINE | re.DOTALL)
    assert found
    return found.group(1)


class ZizmorPathsTest(unittest.TestCase):
    def test_the_input_defaults_to_the_whole_tree(self) -> None:
        inputs = parsed(SECURITY)["on"]["workflow_call"]["inputs"]
        self.assertEqual(inputs["zizmor-paths"]["default"], ".")

    def test_the_step_maps_the_input_to_the_env_and_audits_those_paths(self) -> None:
        step = audit_step()
        self.assertIn("ZIZMOR_PATHS: ${{ inputs.zizmor-paths }}", step)
        self.assertEqual(step.count('"${paths[@]}"'), 2)
        self.assertNotIn("${{", step.split("run: |")[1])

    def test_fleets_own_self_test_narrows_it_to_github(self) -> None:
        job = parsed(SELF_TEST)["jobs"]["security"]
        self.assertEqual(job["with"]["zizmor-paths"], ".github")

    def split_paths(self, value: str) -> str:
        script = step_script()
        probe = script.split("if [ \"$SARIF_UPLOAD\"")[0] + 'printf "%s\\n" "${paths[@]}"\n'
        with tempfile.TemporaryDirectory() as root:
            result = subprocess.run(["bash", "-eo", "pipefail", "-c", probe], env={"ZIZMOR_PATHS": value, "PATH": "/usr/bin:/bin"},
                                    cwd=root, capture_output=True, text=True, check=True)
        return result.stdout

    def test_several_paths_split_and_an_empty_value_falls_back_to_the_whole_tree(self) -> None:
        self.assertEqual(self.split_paths(".github docs"), ".github\ndocs\n")
        self.assertEqual(self.split_paths(""), ".\n")
        self.assertEqual(self.split_paths("   "), ".\n")


def step_script() -> str:
    body = audit_step().split("run: |\n", 1)[1]
    return "\n".join(line[10:] for line in body.splitlines())


if __name__ == "__main__":
    unittest.main()
