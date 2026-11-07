import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fleet_tools import cli, self_check

DOCS = cli.ROOT / ".github" / "workflows" / "docs.yml"
SELF_TEST = cli.ROOT / ".github" / "workflows" / "self-test.yml"
FIXTURE = cli.ROOT / "tests" / "fixtures" / "docs"
ON_MAIN = "github.ref == 'refs/heads/main' && github.event_name != 'pull_request'"
Node = dict[str, Any]


def parsed(path: Path) -> Node:
    workflow = self_check.parse(path.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def jobs() -> Node:
    found = parsed(DOCS)["jobs"]
    assert isinstance(found, dict)
    return found


def steps(job: str) -> list[Node]:
    found = jobs()[job]["steps"]
    assert isinstance(found, list)
    return found


def step(job: str, name: str) -> Node:
    hits = [each for each in steps(job) if each.get("name") == name]
    assert len(hits) == 1, (name, hits)
    return hits[0]


def uses(job: str, action: str) -> Node:
    hits = [each for each in steps(job) if str(each.get("uses", "")).startswith(action + "@")]
    assert len(hits) == 1, (action, hits)
    return hits[0]


def run_block(name: str) -> str:
    text = DOCS.read_text(encoding="utf-8")
    found = re.search(rf"- name: {re.escape(name)}\n.*?run: \|\n(.*?)(?=^\n|^      - |^  \w|\Z)", text, re.MULTILINE | re.DOTALL)
    assert found, name
    return "\n".join(line[10:] for line in found.group(1).splitlines())


def site_check(site: str, cwd: str) -> subprocess.CompletedProcess[str]:
    script = run_block("Check the site was built")
    return subprocess.run(["bash", "-eo", "pipefail", "-c", script], env={"SITE": site, "PATH": "/usr/bin:/bin"},
                          cwd=cwd, capture_output=True, text=True, check=False)


class PermissionsTest(unittest.TestCase):
    def test_the_workflow_grants_nothing_and_each_job_only_what_it_needs(self) -> None:
        self.assertEqual(parsed(DOCS)["permissions"], "{}")
        self.assertEqual(set(jobs()), {"build", "deploy"})
        self.assertEqual(jobs()["build"]["permissions"], {"contents": "read"})
        self.assertEqual(jobs()["deploy"]["permissions"], {"pages": "write", "id-token": "write"})

    def test_the_deploy_job_runs_no_code_of_the_apps(self) -> None:
        self.assertEqual([each["uses"].split("@")[0] for each in steps("deploy")], ["actions/deploy-pages"])
        self.assertNotIn("actions/checkout", DOCS.read_text(encoding="utf-8").split("\n  deploy:\n")[1])

    def test_the_deploy_job_uses_the_pages_environment_with_its_address(self) -> None:
        environment = jobs()["deploy"]["environment"]
        self.assertEqual(environment["name"], "github-pages")
        self.assertEqual(environment["url"], "${{ steps.deploy.outputs.page_url }}")
        self.assertEqual(uses("deploy", "actions/deploy-pages")["id"], "deploy")

    def test_fleets_own_self_test_grants_what_the_called_jobs_ask_for(self) -> None:
        job = parsed(SELF_TEST)["jobs"]["docs"]
        self.assertEqual(job["permissions"], {"contents": "read", "pages": "write", "id-token": "write"})
        self.assertEqual(job["uses"], "./.github/workflows/docs.yml")


class MainOnlyTest(unittest.TestCase):
    def test_only_a_push_or_a_manual_run_on_main_uploads_and_deploys(self) -> None:
        self.assertEqual(uses("build", "actions/upload-pages-artifact")["if"], ON_MAIN)
        self.assertEqual(jobs()["deploy"]["if"], ON_MAIN)
        self.assertEqual(jobs()["deploy"]["needs"], "build")

    def test_the_self_test_runs_only_on_pull_requests_so_it_never_deploys(self) -> None:
        self.assertEqual(list(parsed(SELF_TEST)["on"]), ["pull_request"])

    def test_what_is_checked_is_what_is_published(self) -> None:
        self.assertEqual(uses("build", "actions/upload-pages-artifact")["with"]["path"], "${{ inputs.site-path }}")
        self.assertEqual(step("build", "Check the site was built")["env"]["SITE"], "${{ inputs.site-path }}")


class InputsTest(unittest.TestCase):
    def test_the_defaults_are_runways_build_as_it_was(self) -> None:
        inputs = parsed(DOCS)["on"]["workflow_call"]["inputs"]
        defaults = {name: spec.get("default") for name, spec in inputs.items()}
        self.assertEqual(defaults, {
            "make-target": "docs-build", "working-directory": ".", "site-path": "docs/dist", "node-version-file": ".nvmrc",
            "npm-lockfiles": "docs/package-lock.json", "browser": "", "browser-dir": "frontend", "timeout-minutes": "10",
            "runs-on": '"ubuntu-latest"', "fleet-ref": ""})
        self.assertFalse(any(spec.get("required") for spec in inputs.values()))

    def test_no_input_reaches_a_script_through_an_expression(self) -> None:
        scripts = re.findall(r"^        run: (.*?)(?=^      - |^\n|\Z)", DOCS.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)
        self.assertEqual(len(scripts), 3)
        for script in scripts:
            self.assertNotIn("${{", script)

    def test_the_browser_is_installed_only_when_asked_after_npm_ci(self) -> None:
        install = step("build", "Install the browser")
        self.assertEqual(install["if"], "inputs.browser != ''")
        self.assertEqual(install["working-directory"], "${{ inputs.browser-dir }}")
        script = run_block("Install the browser")
        self.assertLess(script.index("npm ci"), script.index('npx playwright install --with-deps "$BROWSER"'))
        names = [each.get("name") for each in steps("build")]
        self.assertLess(names.index("Install the browser"), names.index("make ${{ inputs.make-target }}"))

    def test_every_action_is_pinned_by_sha_with_its_version(self) -> None:
        for line in DOCS.read_text(encoding="utf-8").splitlines():
            if re.match(r"\s*(- )?uses:", line):
                self.assertRegex(line, r"uses: [\w/-]+@[0-9a-f]{40}   # v\d+\.\d+\.\d+$")

    def test_it_runs_none_of_fleets_files_so_it_checks_fleet_out_nowhere(self) -> None:
        self.assertNotIn("AnthonyPluth/fleet", DOCS.read_text(encoding="utf-8"))


class FixtureTest(unittest.TestCase):
    def test_the_fixture_target_builds_a_site_the_check_accepts(self) -> None:
        if shutil.which("make") is None or shutil.which("npm") is None:
            self.skipTest("make and npm are needed")
        job = parsed(SELF_TEST)["jobs"]["docs"]["with"]
        with tempfile.TemporaryDirectory() as root:
            tree = Path(root) / "tests" / "fixtures" / "docs"
            shutil.copytree(FIXTURE, tree)
            self.assertNotEqual(site_check(job["site-path"], root).returncode, 0)
            subprocess.run(["make", "docs-build"], cwd=Path(root) / job["working-directory"], check=True, capture_output=True)
            result = site_check(job["site-path"], root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_target_that_builds_no_page_fails_the_check(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "docs" / "dist").mkdir(parents=True)
            result = site_check("docs/dist", root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("::error::make built no site at docs/dist", result.stdout)

    def test_the_self_test_points_each_path_input_into_the_fixture(self) -> None:
        job = parsed(SELF_TEST)["jobs"]["docs"]["with"]
        self.assertEqual(job["fleet-ref"], "${{ github.sha }}")
        for name in ("working-directory", "site-path", "node-version-file", "npm-lockfiles"):
            self.assertTrue(job[name].startswith("tests/fixtures/docs"), name)
        self.assertTrue((cli.ROOT / job["npm-lockfiles"]).is_file())
        self.assertTrue((cli.ROOT / job["node-version-file"]).is_file())


if __name__ == "__main__":
    unittest.main()
