import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fleet_tools import cli, self_check

SECURITY = cli.ROOT / ".github" / "workflows" / "security.yml"
SELF_TEST = cli.ROOT / ".github" / "workflows" / "self-test.yml"
FINDINGS = cli.ROOT / ".github" / "scripts" / "sarif_findings.py"
FAIL_IF = "${{ inputs.sarif-upload && inputs.fail-on-findings }}"
FLEET_CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
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


def job_steps(job: str) -> list[Node]:
    found = parsed(SECURITY)["jobs"][job]["steps"]
    assert isinstance(found, list)
    return found


def position(steps: list[Node], match: str) -> int:
    hits = [index for index, step in enumerate(steps) if match in str(step)]
    assert len(hits) == 1, (match, hits)
    return hits[0]


SCANS = {
    "semgrep": ("security-semgrep.sh", "semgrep.sarif", "sarif_findings.py semgrep.sarif"),
    "trivy": ("Scan the repository", "trivy-fs.sarif", "sarif_findings.py --level error trivy-fs.sarif"),
    "image": ("Scan the image", "trivy-image.sarif", "sarif_findings.py --level error trivy-image.sarif"),
}


class FailOnFindingsWorkflowTest(unittest.TestCase):
    def test_the_input_is_an_opt_in_boolean(self) -> None:
        option = parsed(SECURITY)["on"]["workflow_call"]["inputs"]["fail-on-findings"]
        self.assertEqual((option["type"], option["default"]), ("boolean", "false"))

    def test_each_scan_fails_on_its_findings_after_the_upload_only_when_asked(self) -> None:
        for job, (scan, sarif, command) in SCANS.items():
            steps = job_steps(job)
            check = position(steps, command)
            self.assertEqual(steps[check]["if"], FAIL_IF, job)
            self.assertEqual(steps[check]["run"], f"python3 -I .fleet/.github/scripts/{command}", job)
            self.assertGreater(check, position(steps, f"'sarif_file': '{sarif}'"), job)
            self.assertGreater(check, position(steps, scan), job)
            self.assertEqual(check, len(steps) - 1, job)

    def test_trivy_fetches_fleets_scripts_again_only_after_its_scan(self) -> None:
        for job in ("trivy", "image"):
            steps = job_steps(job)
            fetches = [index for index, step in enumerate(steps) if step.get("uses", "").startswith(FLEET_CHECKOUT)
                       and step.get("with", {}).get("repository") == "AnthonyPluth/fleet"]
            self.assertEqual(len(fetches), 2, job)
            again = steps[fetches[-1]]
            self.assertEqual(again["if"], FAIL_IF, job)
            self.assertEqual(again["with"]["ref"], "${{ inputs.fleet-ref || env.FLEET_REF }}", job)
            self.assertEqual(again["with"]["persist-credentials"], "false", job)
            self.assertGreater(fetches[-1], position(steps, SCANS[job][0]), job)
            self.assertGreater(position(steps, "sarif_findings.py"), fetches[-1], job)

    def test_the_default_keeps_the_old_behaviour(self) -> None:
        for job in ("trivy", "image"):
            scan = job_steps(job)[position(job_steps(job), SCANS[job][0])]
            self.assertEqual(scan["with"]["exit-code"], "${{ inputs.sarif-upload && '0' || '1' }}", job)
            self.assertEqual(scan["with"]["severity"], "CRITICAL,HIGH", job)
        text = SECURITY.read_text(encoding="utf-8")
        self.assertEqual(text.count("inputs.fail-on-findings"), 5)
        self.assertNotIn("FAIL_ON_FINDINGS", text)

    def test_the_header_no_longer_says_the_gate_requires_code_scannings_check_runs(self) -> None:
        header = SECURITY.read_text(encoding="utf-8").split("\non:\n", 1)[0]
        self.assertNotIn("requires", header)
        self.assertIn("doesn't require", " ".join(header.replace("#", " ").split()))
        self.assertIn("fail-on-findings", header)

    def test_the_self_test_checks_a_planted_finding_in_sarif_mode(self) -> None:
        steps = parsed(SELF_TEST)["jobs"]["planted-finding"]["steps"]
        sarif = [step for step in steps if step.get("env", {}).get("SARIF_UPLOAD") == "true"]
        self.assertEqual(len(sarif), 1)
        text = SELF_TEST.read_text(encoding="utf-8")
        found = re.search(r"- name: " + re.escape(sarif[0]["name"]) + r"\n(.*?)(?=^      - |^  \S|\Z)", text, re.MULTILINE | re.DOTALL)
        assert found
        self.assertIn(".github/scripts/security-semgrep.sh\n", found.group(1))
        self.assertIn("if python3 -I .github/scripts/sarif_findings.py tests/fixtures/security/planted/semgrep.sarif; then", found.group(1))
        self.assertIn("exit 1", found.group(1))

    def test_the_docs_list_the_input(self) -> None:
        adopting = (cli.ROOT / "docs" / "adopting.md").read_text(encoding="utf-8")
        self.assertRegex(adopting, r"\n\| `fail-on-findings` \| `false` \| ")


def sarif_result(rule: str = "rule.a", level: str | None = "error", uri: str = "app.py", **extra: Any) -> Node:
    result: Node = {"ruleId": rule, "locations": [{"physicalLocation": {"artifactLocation": {"uri": uri}, "region": {"startLine": 3}}}]}
    if level is not None:
        result["level"] = level
    return {**result, **extra}


def sarif(*results: Node, rules: list[Node] | None = None) -> Node:
    return {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "t", "rules": rules or []}}, "results": list(results)}]}


class FindingsScriptTest(unittest.TestCase):
    def check(self, content: object, *levels: str, raw: str | None = None) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "scan.sarif"
            path.write_text(raw if raw is not None else json.dumps(content), encoding="utf-8")
            arguments = [argument for level in levels for argument in ("--level", level)]
            return subprocess.run([sys.executable, "-I", str(FINDINGS), *arguments, str(path)],
                                  capture_output=True, text=True, check=False)

    def test_no_results_passes(self) -> None:
        result = self.check(sarif())
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("No findings in", result.stdout)
        self.assertEqual(self.check({"runs": [{"results": None}]}).returncode, 0)

    def test_any_finding_fails_and_names_it(self) -> None:
        result = self.check(sarif(sarif_result(), sarif_result("rule.b", "note")))
        self.assertEqual(result.returncode, 1)
        self.assertIn("  - rule.a (error) at app.py:3", result.stdout)
        self.assertIn("  - rule.b (note) at app.py:3", result.stdout)
        self.assertIn("::error::2 findings in ", result.stdout)
        self.assertIn("at any level (fail-on-findings)", result.stdout)

    def test_suppressed_and_non_failing_kinds_do_not_count(self) -> None:
        results = [sarif_result(suppressions=[{"kind": "inSource"}]), sarif_result(kind="pass"),
                   sarif_result(kind="informational"), sarif_result(kind="notApplicable")]
        self.assertEqual(self.check(sarif(*results)).returncode, 0)
        self.assertEqual(self.check(sarif(sarif_result(kind="open"))).returncode, 1)

    def test_a_level_filter_counts_only_those_levels(self) -> None:
        medium = sarif(sarif_result(level="warning"), sarif_result(level="note"))
        self.assertEqual(self.check(medium, "error").returncode, 0)
        self.assertIn("at level error.", self.check(medium, "error").stdout)
        high = self.check(sarif(sarif_result(level="warning"), sarif_result("rule.high", "error")), "error")
        self.assertEqual(high.returncode, 1)
        self.assertIn("::error::1 finding in ", high.stdout)
        self.assertNotIn("(warning)", high.stdout)

    def test_a_result_without_a_level_takes_its_rules_default_or_warning(self) -> None:
        rules: list[Node] = [{"id": "rule.a", "defaultConfiguration": {"level": "error"}}, {"id": "rule.b"}]
        self.assertEqual(self.check(sarif(sarif_result("rule.a", None), rules=rules), "error").returncode, 1)
        self.assertEqual(self.check(sarif(sarif_result("rule.a", None, ruleIndex=0), rules=rules), "error").returncode, 1)
        self.assertEqual(self.check(sarif(sarif_result("rule.b", None), rules=rules), "error").returncode, 0)
        self.assertEqual(self.check(sarif(sarif_result("rule.b", None), rules=rules), "warning").returncode, 1)

    def test_a_sarif_that_cannot_be_read_fails(self) -> None:
        for raw in ("not json", "[]", '{"runs": 3}', '{"runs": [3]}', '{"runs": [{"results": [3]}]}',
                    '{"runs": [{"results": [{"level": ["x"]}]}]}'):
            result = self.check(None, raw=raw)
            self.assertEqual(result.returncode, 1, raw)
            self.assertIn("::error::Couldn't read the findings in ", result.stdout, raw)
        missing = subprocess.run([sys.executable, "-I", str(FINDINGS), "/nonexistent/scan.sarif"],
                                 capture_output=True, text=True, check=False)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("::error::Couldn't read the findings in /nonexistent/scan.sarif (FileNotFoundError)", missing.stdout)

    def test_a_scanned_files_name_cannot_start_a_workflow_command(self) -> None:
        result = self.check(sarif(sarif_result(uri="a.py\n::add-mask::x\r\n::stop-commands::y%")))
        self.assertEqual(result.returncode, 1)
        commands = [line for line in result.stdout.splitlines() if line.startswith("::")]
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0].startswith("::error::1 finding in "))
        self.assertIn("a.py%0A::add-mask::x%0D%0A::stop-commands::y%25:3", result.stdout)

    def test_many_findings_are_listed_up_to_a_limit(self) -> None:
        result = self.check(sarif(*[sarif_result(f"rule.{index}") for index in range(60)]))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(sum(1 for line in result.stdout.splitlines() if line.startswith("  - ")), 50)
        self.assertIn("  ... and 10 more", result.stdout)
        self.assertIn("::error::60 findings in ", result.stdout)


if __name__ == "__main__":
    unittest.main()
