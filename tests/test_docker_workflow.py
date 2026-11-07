import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fleet_tools import cli, self_check
from fleet_tools.checks import workflows as wf

DOCKER = cli.ROOT / ".github" / "workflows" / "docker.yml"
SELF_TEST = cli.ROOT / ".github" / "workflows" / "self-test.yml"
ADOPTING = cli.ROOT / "docs" / "adopting.md"
FIXTURE = cli.ROOT / "tests" / "fixtures" / "docker" / "Dockerfile"
CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
FLEET_REF = "${{ inputs.fleet-ref || env.FLEET_REF }}"
Node = dict[str, Any]


def parsed(path: Path) -> Node:
    workflow = self_check.parse(path.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def jobs() -> Node:
    found = parsed(DOCKER)["jobs"]
    assert isinstance(found, dict)
    return found


def steps(job: str) -> list[Node]:
    found = jobs()[job]["steps"]
    assert isinstance(found, list)
    return found


def position(job: str, match: str) -> int:
    hits = [index for index, step in enumerate(steps(job)) if match in str(step)]
    assert len(hits) == 1, (job, match, hits)
    return hits[0]


def job_text(job: str) -> str:
    text = DOCKER.read_text(encoding="utf-8")
    found = re.search(rf"^  {job}:\n(.*?)(?=^  \S|\Z)", text, re.MULTILINE | re.DOTALL)
    assert found
    return found.group(1)


def script(job: str, name: str) -> str:
    found = re.search(r"- name: " + re.escape(name) + r"\n(.*?)(?=^      - |\Z)", job_text(job), re.MULTILINE | re.DOTALL)
    assert found, name
    body = found.group(1).split("run: |\n", 1)[1]
    return "\n".join(line[10:] for line in body.splitlines()) + "\n"


def bash(text: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as root:
        return subprocess.run(["bash", "-eo", "pipefail", "-c", text], env={"PATH": "/usr/bin:/bin", **env}, cwd=root,
                              capture_output=True, text=True, check=False)


def flat(value: str) -> str:
    return " ".join(value.split())


class Shape(unittest.TestCase):
    def test_no_permissions_at_the_top_and_the_least_per_job(self) -> None:
        workflow = parsed(DOCKER)
        self.assertEqual(workflow["permissions"], "{}")
        self.assertEqual(jobs()["build"]["permissions"], {"contents": "read"})
        self.assertEqual(jobs()["publish"]["permissions"], {"contents": "write", "packages": "write"})
        self.assertEqual(jobs()["cleanup"]["permissions"], {"packages": "write"})
        self.assertEqual(set(jobs()), {"build", "publish", "cleanup"})

    def test_the_fleet_ref_convention_and_bash(self) -> None:
        workflow = parsed(DOCKER)
        self.assertEqual(workflow["env"]["FLEET_REF"], self_check.expected_ref(cli.version()))
        self.assertEqual(workflow["defaults"], {"run": {"shell": "bash"}})
        self.assertEqual(self_check.check_fleet_checkouts("docker.yml", workflow), [])

    def test_no_concurrency_group_and_no_secrets(self) -> None:
        text = DOCKER.read_text(encoding="utf-8")
        self.assertNotIn("concurrency", parsed(DOCKER))
        for job in jobs().values():
            self.assertNotIn("concurrency", job)
        self.assertNotIn("secrets", parsed(DOCKER)["on"]["workflow_call"])
        self.assertNotRegex(text, r"secrets\.[A-Za-z]")
        self.assertNotIn("inherit", text)
        self.assertNotIn("pull_request_target:", text)

    def test_every_action_is_pinned_with_its_version(self) -> None:
        problems = [problem for problem in wf.check_workflow("docker.yml", DOCKER.read_text(encoding="utf-8"))
                    if "runs-on" not in problem]
        self.assertEqual(problems, [])
        uses = re.findall(r"uses: (\S+)(.*)", DOCKER.read_text(encoding="utf-8"))
        self.assertGreater(len(uses), 8)
        for target, rest in uses:
            self.assertRegex(target, r"@[0-9a-f]{40}$")
            self.assertRegex(rest, r"#\s*v\d")

    def test_every_checkout_drops_its_credentials(self) -> None:
        for name in jobs():
            for step in steps(name):
                if str(step.get("uses", "")).startswith("actions/checkout@"):
                    self.assertEqual(step["with"]["persist-credentials"], "false", name)

    def test_no_input_or_event_value_is_interpolated_into_a_script(self) -> None:
        for name in jobs():
            for step in steps(name):
                if "run" in step:
                    found = re.search(r"- name: " + re.escape(step["name"]) + r"\n(.*?)(?=^      - |\Z)",
                                      job_text(name), re.MULTILINE | re.DOTALL)
                    assert found
                    run = found.group(1).split("run:", 1)[1]
                    self.assertNotIn("${{", run, step["name"])

    def test_the_inputs(self) -> None:
        inputs = parsed(DOCKER)["on"]["workflow_call"]["inputs"]
        self.assertEqual(inputs["image"]["required"], "true")
        self.assertEqual((inputs["publish"]["type"], inputs["publish"]["default"]), ("boolean", "false"))
        self.assertEqual((inputs["cleanup"]["type"], inputs["cleanup"]["default"]), ("boolean", "true"))
        self.assertEqual((inputs["smoke-port"]["type"], inputs["smoke-port"]["default"]), ("number", "8765"))
        self.assertEqual((inputs["build-timeout-minutes"]["type"], inputs["build-timeout-minutes"]["default"]), ("number", "45"))
        self.assertEqual(inputs["runs-on"]["default"], '"ubuntu-latest"')
        self.assertEqual(set(inputs), {"image", "publish", "version", "new", "newest", "context", "dockerfile", "smoke-port",
                                       "smoke-env", "cleanup", "build-timeout-minutes", "runs-on", "fleet-ref"})
        outputs = parsed(DOCKER)["on"]["workflow_call"]["outputs"]
        for name in ("version", "new", "newest", "skip"):
            self.assertEqual(outputs[name]["value"], f"${{{{ jobs.build.outputs.{name} }}}}")
            self.assertEqual(jobs()["build"]["outputs"][name], f"${{{{ steps.next.outputs.{name} }}}}")


class Gates(unittest.TestCase):
    def test_the_build_runs_only_without_publish(self) -> None:
        self.assertEqual(jobs()["build"]["if"], "${{ !inputs.publish }}")

    def test_the_publish_runs_only_on_main_and_never_for_a_pull_request(self) -> None:
        condition = flat(jobs()["publish"]["if"])
        self.assertEqual(condition, "inputs.publish && github.ref == 'refs/heads/main' && "
                                    "github.event_name != 'pull_request' && github.event_name != 'pull_request_target'")
        self.assertNotIn("needs", jobs()["publish"])

    def test_the_cleanup_follows_the_publish(self) -> None:
        self.assertEqual(jobs()["cleanup"]["needs"], "publish")
        self.assertEqual(jobs()["cleanup"]["if"], "inputs.cleanup")
        self.assertEqual(steps("cleanup")[0]["with"]["package"], "${{ inputs.image }}")

    def test_the_release_comes_after_the_push_and_only_for_a_new_version(self) -> None:
        release = position("publish", "Tag and publish the release")
        self.assertGreater(release, position("publish", "push': 'true'"))
        self.assertEqual(release, len(steps("publish")) - 1)
        self.assertEqual(steps("publish")[release]["if"], "inputs.new == 'true'")

    def test_the_publish_checks_its_inputs_first(self) -> None:
        self.assertEqual(steps("publish")[0]["name"], "Check the inputs")
        self.assertEqual(steps("build")[0]["name"], "Check the inputs")

    def test_the_build_never_pushes_and_builds_for_arm_only_with_a_version(self) -> None:
        for step in steps("build"):
            self.assertNotEqual(step.get("with", {}).get("push"), "true")
        multi = steps("build")[position("build", "linux/amd64,linux/arm64")]
        self.assertEqual(multi["if"], "steps.next.outputs.version != '' && steps.next.outputs.skip != 'true'")
        self.assertEqual(multi["with"]["push"], "false")
        self.assertEqual(multi["with"]["cache-to"], "type=gha,mode=max")


class Version(unittest.TestCase):
    def test_next_version_runs_from_fleets_checkout_at_the_pin_never_on_a_pull_request(self) -> None:
        build = steps("build")
        fleet = build[position("build", "AnthonyPluth/fleet")]
        self.assertEqual(fleet["with"]["ref"], FLEET_REF)
        self.assertEqual(fleet["with"]["path"], ".fleet")
        sparse = job_text("build").split("sparse-checkout: |\n", 1)[1].split("sparse-checkout-cone-mode", 1)[0].split()
        self.assertEqual(sparse, ["fleet_tools", "VERSION"])
        step = build[position("build", "next-version")]
        self.assertEqual(step["run"], 'python3 -E -s -m fleet_tools.cli next-version --root "$GITHUB_WORKSPACE"')
        self.assertEqual(step["working-directory"], ".fleet")
        self.assertEqual(step["if"], "github.event_name != 'pull_request' && github.event_name != 'pull_request_target'")
        self.assertEqual(step["id"], "next")
        self.assertGreater(position("build", "next-version"), position("build", "fetch-depth"))

    def test_fleets_tools_are_gone_before_anything_is_built(self) -> None:
        removed = position("build", "rm -rf .fleet")
        self.assertEqual(removed, position("build", "next-version") + 1)
        for index, step in enumerate(steps("build")):
            if str(step.get("uses", "")).startswith("docker/"):
                self.assertGreater(index, removed)


class Scripts(unittest.TestCase):
    def check(self, **env: str) -> subprocess.CompletedProcess[str]:
        values = {"IMAGE": "runway", "SMOKE_PORT": "8765", "SMOKE_ENV": "", **env}
        return bash(script("build", "Check the inputs"), values)

    def test_the_build_refuses_a_bad_image_port_or_environment_line(self) -> None:
        self.assertEqual(self.check().returncode, 0)
        self.assertEqual(self.check(SMOKE_ENV="RUNWAY_ALLOW_NO_AUTH=1\n\nRUNWAY_NO_SYNC=1\n").returncode, 0)
        for env in ({"IMAGE": "Runway"}, {"IMAGE": "runway:latest"}, {"IMAGE": ""}, {"IMAGE": "a/b"},
                    {"SMOKE_PORT": "0"}, {"SMOKE_PORT": "70000"}, {"SMOKE_PORT": "80 -v /:/host"},
                    {"SMOKE_ENV": "OK=1\n-v /:/host"}, {"SMOKE_ENV": "=1"}):
            with self.subTest(env=env):
                result = self.check(**env)
                self.assertEqual(result.returncode, 1)
                self.assertIn("::error::", result.stdout)

    def publish_check(self, **env: str) -> subprocess.CompletedProcess[str]:
        values = {"IMAGE": "waypoint", "VERSION": "v1.2.3", "NEW": "true", "NEWEST": "false", **env}
        return bash(script("publish", "Check the inputs"), values)

    def test_the_publish_refuses_anything_but_the_builds_outputs(self) -> None:
        self.assertEqual(self.publish_check().returncode, 0)
        for env in ({"VERSION": ""}, {"VERSION": "1.2.3"}, {"VERSION": "v1.2"}, {"VERSION": "v1.2.3; rm -rf /"},
                    {"NEW": ""}, {"NEWEST": "yes"}, {"IMAGE": "Waypoint"}):
            with self.subTest(env=env):
                self.assertEqual(self.publish_check(**env).returncode, 1)

    def test_the_tags_are_as_before(self) -> None:
        text = script("publish", "Image tags (latest, 1.2.3, v1.2.3, 1.2 and 1)")
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "out"
            for newest, tags in (("true", ["x:1.2.3", "x:v1.2.3", "x:latest", "x:1.2", "x:1"]), ("false", ["x:1.2.3", "x:v1.2.3"])):
                output.write_text("", encoding="utf-8")
                result = bash(text, {"IMAGE": "x", "VERSION": "v1.2.3", "NEWEST": newest, "GITHUB_OUTPUT": str(output)})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_text(encoding="utf-8").splitlines(), ["list<<EOF", *tags, "EOF"])

    def test_the_start_check_passes_each_environment_line_as_one_argument(self) -> None:
        text = script("build", "Check the image starts").replace("docker run", "printf '%s\\n'", 1)
        text = text.split("for _ in", 1)[0]
        result = bash(text, {"IMAGE": "runway", "SMOKE_PORT": "8000", "SMOKE_ENV": "A=1\n\nB=two words\n"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["-d", "--name", "smoke", "-p", "8765:8000", "-e", "A=1", "-e",
                                                      "B=two words", "runway:smoke"])

    def test_the_release_is_titled_with_the_repositorys_name(self) -> None:
        self.assertIn('--title "${GITHUB_REPOSITORY#*/} $VERSION"', script("publish", "Tag and publish the release"))


class SelfTest(unittest.TestCase):
    def test_the_self_test_builds_the_fixture_with_its_environment(self) -> None:
        job = parsed(SELF_TEST)["jobs"]["docker"]
        self.assertEqual(job["uses"], "./.github/workflows/docker.yml")
        self.assertEqual(job["permissions"], {"contents": "read"})
        self.assertEqual(job["with"]["fleet-ref"], "${{ github.sha }}")
        self.assertEqual(job["with"]["dockerfile"], "tests/fixtures/docker/Dockerfile")
        self.assertNotIn("publish", job["with"])
        self.assertIn("FLEET_SMOKE=on", SELF_TEST.read_text(encoding="utf-8"))
        self.assertEqual(parsed(SELF_TEST)["jobs"]["docker-outputs"]["needs"], "[docker]")

    def test_the_fixture_runs_as_a_user_and_needs_the_environment(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^USER 65534")
        self.assertIn('[ \\"$FLEET_SMOKE\\" = on ]', text)
        self.assertIn("-p 8765", text)


def docs_section() -> str:
    text = ADOPTING.read_text(encoding="utf-8")
    found = re.search(r"^## Docker image\n(.*?)(?=^## )", text, re.MULTILINE | re.DOTALL)
    assert found
    return found.group(1)


class Docs(unittest.TestCase):
    def test_the_table_lists_every_input(self) -> None:
        listed: set[str] = set()
        for row in re.findall(r"^\| (`[^|]+) \|", docs_section(), re.MULTILINE):
            listed |= set(re.findall(r"`([^`]+)`", row))
        self.assertEqual(listed, set(parsed(DOCKER)["on"]["workflow_call"]["inputs"]))

    def test_the_caller_calls_it_twice_with_the_permissions_each_needs(self) -> None:
        block = re.search(r"```yaml\n(.*?)```", docs_section(), re.DOTALL)
        assert block
        caller: Any = self_check.parse(block.group(1).replace("@<commit>", "@" + "0" * 40))
        assert isinstance(caller, dict)
        build, publish = caller["jobs"]["build"], caller["jobs"]["publish"]
        self.assertEqual(build["permissions"], {"contents": "read"})
        self.assertEqual(publish["permissions"], {"contents": "write", "packages": "write"})
        self.assertEqual(publish["with"]["publish"], "true")
        self.assertEqual(publish["needs"], "[test, web, build]")
        self.assertEqual(publish["if"], "needs.build.outputs.skip != 'true'")
        self.assertEqual(build["if"], "github.event_name != 'pull_request' && github.ref == 'refs/heads/main'")
        for name in ("version", "new", "newest"):
            self.assertEqual(publish["with"][name], f"${{{{ needs.build.outputs.{name} }}}}")
        self.assertNotIn("secrets", build)
        self.assertNotIn("secrets", publish)


if __name__ == "__main__":
    unittest.main()
