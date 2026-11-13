import itertools
import json
import os
import re
import shutil
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
FIXTURES = cli.ROOT / "tests" / "fixtures" / "docker"
FIXTURE = FIXTURES / "Dockerfile"
CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
FLEET_CHECKOUT_REF = "${{ inputs.fleet-ref || job.workflow_sha }}"
SECOND_INPUTS = ("second-image", "second-dockerfile", "second-smoke-command", "second-smoke-seccomp", "second-smoke-shm-size")
COMPOSE_INPUTS = ("compose-file", "compose-env")
HAS_SECOND = "inputs.second-image != ''"
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


def positions(job: str, match: str) -> list[int]:
    return [index for index, step in enumerate(steps(job)) if match in str(step)]


def position(job: str, match: str) -> int:
    hits = positions(job, match)
    assert len(hits) == 1, (job, match, hits)
    return hits[0]


def named(job: str, name: str) -> Node:
    hits = [step for step in steps(job) if step.get("name") == name]
    assert len(hits) == 1, (job, name, len(hits))
    return hits[0]


def index_of(job: str, name: str) -> int:
    return steps(job).index(named(job, name))


def job_text(job: str) -> str:
    text = DOCKER.read_text(encoding="utf-8")
    found = re.search(rf"^  {job}:\n(.*?)(?=^  \S|\Z)", text, re.MULTILINE | re.DOTALL)
    assert found
    return found.group(1)


def step_text(job: str, name: str) -> list[str]:
    found = re.search(r"- name: " + re.escape(name) + r"\n(.*?)(?=^      - |\Z)", job_text(job), re.MULTILINE | re.DOTALL)
    assert found, name
    return [line for line in itertools.takewhile(lambda line: not line.strip() or line.startswith(" " * 8),
                                                 found.group(1).splitlines()) if line.strip()]


def script(job: str, name: str) -> str:
    found = re.search(r"- name: " + re.escape(name) + r"\n(.*?)(?=^      - |\Z)", job_text(job), re.MULTILINE | re.DOTALL)
    assert found, name
    body = found.group(1).split("run: |\n", 1)[1]
    lines = itertools.takewhile(lambda line: not line.strip() or line.startswith(" " * 10), body.splitlines())
    return "\n".join(line[10:] for line in lines) + "\n"


def bash(text: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as root:
        return subprocess.run(["bash", "-eo", "pipefail", "-c", text], env={"PATH": "/usr/bin:/bin", **env}, cwd=root,
                              capture_output=True, text=True, check=False)


def with_fake_docker(text: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as bin_dir:
        fake = Path(bin_dir) / "docker"
        fake.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@"\necho "FLEET_COMPOSE=${FLEET_COMPOSE-unset}"\n', encoding="utf-8")
        fake.chmod(0o755)
        return bash(text, {"PATH": f"{bin_dir}:/usr/bin:/bin", **env})


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
                                       "smoke-env", "cleanup", "build-timeout-minutes", "runs-on", "fleet-ref",
                                       *SECOND_INPUTS, *COMPOSE_INPUTS})
        outputs = parsed(DOCKER)["on"]["workflow_call"]["outputs"]
        self.assertEqual(set(outputs), {"version", "new", "newest", "skip"})
        for name in ("version", "new", "newest", "skip"):
            self.assertEqual(outputs[name]["value"], f"${{{{ jobs.build.outputs.{name} }}}}")
            self.assertEqual(jobs()["build"]["outputs"][name], f"${{{{ steps.next.outputs.{name} }}}}")

    def test_the_new_inputs_are_optional_and_off_by_default(self) -> None:
        inputs = parsed(DOCKER)["on"]["workflow_call"]["inputs"]
        for name in (*SECOND_INPUTS, *COMPOSE_INPUTS):
            with self.subTest(name=name):
                self.assertEqual((inputs[name]["type"], inputs[name]["default"]), ("string", ""))
                self.assertNotIn("required", inputs[name])

    def test_the_first_images_cache_is_where_it_was_and_the_second_has_its_own(self) -> None:
        for job in ("build", "publish"):
            for step in steps(job):
                cache = {key: value for key, value in step.get("with", {}).items() if key.startswith("cache-")}
                if not cache:
                    continue
                with self.subTest(job=job, step=step["name"]):
                    if "second-dockerfile" in step["with"]["file"]:
                        self.assertEqual(cache["cache-from"], "type=gha,scope=${{ inputs.second-image }}")
                        if "cache-to" in cache:
                            self.assertEqual(cache["cache-to"], "type=gha,mode=max,scope=${{ inputs.second-image }}")
                    else:
                        self.assertEqual(step["with"]["file"], "${{ inputs.dockerfile }}")
                        self.assertEqual(cache["cache-from"], "type=gha")
                        self.assertEqual(cache.get("cache-to", "type=gha,mode=max"), "type=gha,mode=max")


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
        self.assertNotIn("if", steps("cleanup")[0])

    def test_the_cleanup_tidies_the_second_images_package_the_same_way(self) -> None:
        first, second = steps("cleanup")
        self.assertEqual(second["if"], HAS_SECOND)
        self.assertEqual(second["uses"], first["uses"])
        self.assertEqual(second["with"], {**first["with"], "package": "${{ inputs.second-image }}"})

    def test_the_release_comes_after_every_push_and_only_for_a_new_version(self) -> None:
        release = position("publish", "Tag and publish the release")
        pushes = positions("publish", "'push': 'true'")
        self.assertEqual(len(pushes), 2)
        self.assertGreater(release, max(pushes))
        self.assertEqual(release, len(steps("publish")) - 1)
        self.assertEqual(steps("publish")[release]["if"], "inputs.new == 'true'")

    def test_the_publish_checks_its_inputs_first(self) -> None:
        self.assertEqual(steps("publish")[0]["name"], "Check the inputs")
        self.assertEqual(steps("build")[0]["name"], "Check the inputs")

    def test_the_build_never_pushes_and_builds_for_arm_only_with_a_version(self) -> None:
        for step in steps("build"):
            self.assertNotEqual(step.get("with", {}).get("push"), "true")
        first, second = (steps("build")[index] for index in positions("build", "linux/amd64,linux/arm64"))
        self.assertEqual(first["if"], "steps.next.outputs.version != '' && steps.next.outputs.skip != 'true'")
        self.assertEqual(second["if"], f"{first['if']} && {HAS_SECOND}")
        for multi in (first, second):
            self.assertEqual(multi["with"]["push"], "false")
            self.assertTrue(multi["with"]["cache-to"].startswith("type=gha,mode=max"))

    def test_every_step_for_the_second_image_runs_only_with_one(self) -> None:
        for job in jobs():
            for step in steps(job):
                if step.get("name") in ("Check the inputs", "Image names (registry names must be lowercase)"):
                    continue
                if "second" in str(step.get("with", {})) + str(step.get("env", {})):
                    with self.subTest(job=job, step=step["name"]):
                        self.assertIn(HAS_SECOND, step.get("if", ""))
        self.assertEqual(len([step for job in jobs() for step in steps(job) if HAS_SECOND in step.get("if", "")]), 5)

    def test_the_second_images_steps_match_the_firsts_line_for_line(self) -> None:
        swap = {"file: ${{ inputs.dockerfile }}": "file: ${{ inputs.second-dockerfile }}",
                "tags: ${{ inputs.image }}:smoke": "tags: ${{ inputs.second-image }}:smoke",
                "tags: ${{ steps.tags.outputs.list }}": "tags: ${{ steps.tags.outputs.second }}",
                "cache-from: type=gha": "cache-from: type=gha,scope=${{ inputs.second-image }}",
                "cache-to: type=gha,mode=max": "cache-to: type=gha,mode=max,scope=${{ inputs.second-image }}"}
        for job, first, second in (("build", "Build for this runner", "Build the second image for this runner"),
                                   ("build", "Build for Intel/AMD and ARM (not pushed)",
                                    "Build the second image for Intel/AMD and ARM (not pushed)"),
                                   ("publish", "Push (from the build's cache)", "Push the second image (from the build's cache)")):
            with self.subTest(step=second):
                expected = [swap.get(line.strip(), line.strip()) for line in step_text(job, first) if "if:" not in line]
                self.assertEqual([line.strip() for line in step_text(job, second) if "if:" not in line], expected)
                self.assertEqual(named(job, second).get("if"), f"{named(job, first)['if']} && {HAS_SECOND}"
                                 if "if" in named(job, first) else HAS_SECOND)
                self.assertGreater(index_of(job, second), index_of(job, first))

    def test_the_checks_skip_when_nothing_more_runs(self) -> None:
        self.assertEqual(named("build", "Check the second image")["if"],
                         f"steps.next.outputs.skip != 'true' && {HAS_SECOND} && inputs.second-smoke-command != ''")
        self.assertEqual(named("build", "Check the Compose file")["if"],
                         "steps.next.outputs.skip != 'true' && inputs.compose-file != ''")
        for name in ("Check the second image", "Check the Compose file"):
            self.assertLess(index_of("build", name), index_of("build", "Build for Intel/AMD and ARM (not pushed)"))
        self.assertGreater(index_of("build", "Check the second image"), index_of("build", "Build the second image for this runner"))


class Version(unittest.TestCase):
    def test_next_version_runs_from_fleets_checkout_at_the_pin_never_on_a_pull_request(self) -> None:
        build = steps("build")
        fleet = build[position("build", "AnthonyPluth/fleet")]
        self.assertEqual(fleet["with"]["ref"], FLEET_CHECKOUT_REF)
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

    def second(self, **env: str) -> subprocess.CompletedProcess[str]:
        values = {"SECOND_IMAGE": "darkroom-browser", "SECOND_DOCKERFILE": "Dockerfile.browser",
                  "SECOND_SMOKE_COMMAND": "bash\n/app/smoke.sh\n", "SECOND_SMOKE_SECCOMP": "deploy/browser/seccomp.json",
                  "SECOND_SMOKE_SHM_SIZE": "1g", "COMPOSE_FILE_INPUT": "docker-compose.yml",
                  "COMPOSE_ENV_INPUT": "PHOTOS_ROOT=/tmp/photos\n", **env}
        return self.check(IMAGE="darkroom", **values)

    def test_the_build_takes_a_second_image_and_a_compose_file(self) -> None:
        self.assertEqual(self.second().returncode, 0, self.second().stdout)
        for env in ({"SECOND_SMOKE_COMMAND": "", "SECOND_SMOKE_SECCOMP": "", "SECOND_SMOKE_SHM_SIZE": ""},
                    {"SECOND_SMOKE_SECCOMP": "", "SECOND_SMOKE_SHM_SIZE": "64m"}, {"SECOND_SMOKE_SHM_SIZE": "512k"},
                    {"COMPOSE_FILE_INPUT": "", "COMPOSE_ENV_INPUT": ""}, {"COMPOSE_ENV_INPUT": ""},
                    {"COMPOSE_FILE_INPUT": "deploy/compose.yaml"}, {"SECOND_SMOKE_COMMAND": "sh\n-c\nexit 0"}):
            with self.subTest(env=env):
                self.assertEqual(self.second(**env).returncode, 0, self.second(**env).stdout)

    def test_the_build_refuses_a_bad_second_image_or_compose_input(self) -> None:
        for env in ({"SECOND_IMAGE": "darkroom"}, {"SECOND_IMAGE": "Darkroom-Browser"}, {"SECOND_IMAGE": "a/b"},
                    {"SECOND_DOCKERFILE": ""},
                    {"SECOND_IMAGE": "", "SECOND_SMOKE_COMMAND": "", "SECOND_SMOKE_SECCOMP": "", "SECOND_SMOKE_SHM_SIZE": ""},
                    {"SECOND_IMAGE": "", "SECOND_DOCKERFILE": "", "SECOND_SMOKE_SECCOMP": "", "SECOND_SMOKE_SHM_SIZE": ""},
                    {"SECOND_SMOKE_COMMAND": "\n\n"}, {"SECOND_SMOKE_COMMAND": "", "SECOND_SMOKE_SHM_SIZE": ""},
                    {"SECOND_SMOKE_COMMAND": "", "SECOND_SMOKE_SECCOMP": ""},
                    {"SECOND_SMOKE_SECCOMP": "unconfined"}, {"SECOND_SMOKE_SECCOMP": "/etc/seccomp.json"},
                    {"SECOND_SMOKE_SECCOMP": "deploy/../seccomp.json"}, {"SECOND_SMOKE_SECCOMP": "deploy/browser/seccomp.yaml"},
                    {"SECOND_SMOKE_SECCOMP": "a b.json"}, {"SECOND_SMOKE_SHM_SIZE": "1G"}, {"SECOND_SMOKE_SHM_SIZE": "0"},
                    {"SECOND_SMOKE_SHM_SIZE": "1g --privileged"}, {"COMPOSE_FILE_INPUT": "/etc/compose.yml"},
                    {"COMPOSE_FILE_INPUT": "../compose.yml"}, {"COMPOSE_FILE_INPUT": "-f"},
                    {"COMPOSE_FILE_INPUT": "", "COMPOSE_ENV_INPUT": "A=1"}, {"COMPOSE_ENV_INPUT": "A=1\n-v /:/host"},
                    {"COMPOSE_ENV_INPUT": "=1"}):
            with self.subTest(env=env):
                result = self.second(**env)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("::error::", result.stdout)

    def test_without_the_new_inputs_the_build_checks_as_before(self) -> None:
        self.assertEqual(self.check(SECOND_IMAGE="", SECOND_DOCKERFILE="", COMPOSE_FILE_INPUT="").returncode, 0)

    def publish_check(self, **env: str) -> subprocess.CompletedProcess[str]:
        values = {"IMAGE": "waypoint", "VERSION": "v1.2.3", "NEW": "true", "NEWEST": "false", **env}
        return bash(script("publish", "Check the inputs"), values)

    def test_the_publish_refuses_anything_but_the_builds_outputs(self) -> None:
        self.assertEqual(self.publish_check().returncode, 0)
        for env in ({"VERSION": ""}, {"VERSION": "1.2.3"}, {"VERSION": "v1.2"}, {"VERSION": "v1.2.3; rm -rf /"},
                    {"NEW": ""}, {"NEWEST": "yes"}, {"IMAGE": "Waypoint"}):
            with self.subTest(env=env):
                self.assertEqual(self.publish_check(**env).returncode, 1)

    def test_the_publish_refuses_a_bad_second_image(self) -> None:
        good = {"SECOND_IMAGE": "waypoint-worker", "SECOND_DOCKERFILE": "Dockerfile.worker"}
        self.assertEqual(self.publish_check(**good).returncode, 0)
        for env in ({"SECOND_IMAGE": "waypoint"}, {"SECOND_IMAGE": "Worker"}, {"SECOND_IMAGE": "a:b"}, {"SECOND_DOCKERFILE": ""}):
            with self.subTest(env=env):
                result = self.publish_check(**{**good, **env})
                self.assertEqual(result.returncode, 1)
                self.assertIn("::error::", result.stdout)

    def test_the_image_names_are_lowercase_and_the_second_is_empty_without_one(self) -> None:
        text = script("publish", "Image names (registry names must be lowercase)")
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "env"
            for second, expected in (("", ""), ("darkroom-browser", "ghcr.io/owner/darkroom-browser")):
                output.write_text("", encoding="utf-8")
                result = bash(text, {"NAME": "darkroom", "SECOND_NAME": second, "GITHUB_REPOSITORY_OWNER": "Owner",
                                     "GITHUB_ENV": str(output)})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_text(encoding="utf-8").splitlines(),
                                 ["IMAGE=ghcr.io/owner/darkroom", f"SECOND_IMAGE={expected}"])

    def test_the_tags_are_as_before(self) -> None:
        text = script("publish", "Image tags (latest, 1.2.3, v1.2.3, 1.2 and 1)")
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "out"
            for newest, tags in (("true", ["x:1.2.3", "x:v1.2.3", "x:latest", "x:1.2", "x:1"]), ("false", ["x:1.2.3", "x:v1.2.3"])):
                for second in ({}, {"SECOND_IMAGE": ""}):
                    output.write_text("", encoding="utf-8")
                    result = bash(text, {"IMAGE": "x", "VERSION": "v1.2.3", "NEWEST": newest, "GITHUB_OUTPUT": str(output),
                                         **second})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(output.read_text(encoding="utf-8").splitlines(), ["list<<EOF", *tags, "EOF"])

    def test_the_second_image_gets_the_same_tags(self) -> None:
        text = script("publish", "Image tags (latest, 1.2.3, v1.2.3, 1.2 and 1)")
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "out"
            for newest, versions in (("true", ["1.2.3", "v1.2.3", "latest", "1.2", "1"]), ("false", ["1.2.3", "v1.2.3"])):
                output.write_text("", encoding="utf-8")
                result = bash(text, {"IMAGE": "x", "SECOND_IMAGE": "y", "VERSION": "v1.2.3", "NEWEST": newest,
                                     "GITHUB_OUTPUT": str(output)})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_text(encoding="utf-8").splitlines(),
                                 ["list<<EOF", *(f"x:{v}" for v in versions), "EOF",
                                  "second<<EOF", *(f"y:{v}" for v in versions), "EOF"])

    def test_the_second_images_check_runs_its_command_in_a_locked_down_container(self) -> None:
        text = script("build", "Check the second image")
        env = {"SECOND_IMAGE": "darkroom-browser", "COMMAND": "bash\n\n/app/smoke.sh\ntwo words\n--privileged\n",
               "SECCOMP": "deploy/browser/seccomp.json", "SHM_SIZE": "1g"}
        result = with_fake_docker(text, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[:-1],
                         ["run", "--rm", "--cap-drop=ALL", "--security-opt", "no-new-privileges:true", "--security-opt",
                          "seccomp=deploy/browser/seccomp.json", "--shm-size=1g", "--entrypoint=bash",
                          "darkroom-browser:smoke", "/app/smoke.sh", "two words", "--privileged"])
        result = with_fake_docker(text, {**env, "COMMAND": "/check", "SECCOMP": "", "SHM_SIZE": ""})
        self.assertEqual(result.stdout.splitlines()[:-1],
                         ["run", "--rm", "--cap-drop=ALL", "--security-opt", "no-new-privileges:true", "--entrypoint=/check",
                          "darkroom-browser:smoke"])

    def test_the_compose_check_adds_its_variables_and_names_the_file(self) -> None:
        text = script("build", "Check the Compose file")
        result = with_fake_docker(text, {"FILE": "docker-compose.yml", "VARIABLES": "FLEET_COMPOSE=two words\n\n"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         ["compose", "--file=docker-compose.yml", "config", "--quiet", "FLEET_COMPOSE=two words"])
        result = with_fake_docker(text, {"FILE": "-v", "VARIABLES": ""})
        self.assertEqual(result.stdout.splitlines(), ["compose", "--file=-v", "config", "--quiet", "FLEET_COMPOSE=unset"])

    def test_the_start_check_passes_each_environment_line_as_one_argument(self) -> None:
        text = script("build", "Check the image starts").replace("docker run", "printf '%s\\n'", 1)
        text = text.split("for _ in", 1)[0].replace("docker rm -f smoke", "true", 1)
        result = bash(text, {"IMAGE": "runway", "SMOKE_PORT": "8000", "SMOKE_ENV": "A=1\n\nB=two words\n"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["-d", "--name", "smoke", "-p", "8765:8000", "-e", "A=1", "-e",
                                                      "B=two words", "runway:smoke"])

    def test_the_start_checks_container_is_removed_before_and_always_after(self) -> None:
        check = script("build", "Check the image starts")
        self.assertLess(check.index("docker rm -f smoke"), check.index("docker run"))
        self.assertNotIn("docker rm -f smoke; exit 0", check)
        cleanup = position("build", "Remove the start check's container")
        self.assertEqual(cleanup, position("build", "Check the image starts") + 1)
        step = steps("build")[cleanup]
        self.assertEqual(step["if"], "always()")
        self.assertEqual(step["run"], "docker rm -f smoke > /dev/null 2>&1 || true")

    def test_the_release_is_titled_with_the_repositorys_name(self) -> None:
        self.assertIn('--title "${GITHUB_REPOSITORY#*/} $VERSION"', script("publish", "Tag and publish the release"))


class SelfTest(unittest.TestCase):
    def test_the_self_test_builds_the_fixture_with_its_environment(self) -> None:
        job = parsed(SELF_TEST)["jobs"]["docker"]
        self.assertEqual(job["uses"], "./.github/workflows/docker.yml")
        self.assertEqual(job["permissions"], {"contents": "write", "packages": "write"})
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

    def test_the_self_test_builds_and_checks_a_second_image_and_a_compose_file(self) -> None:
        given = parsed(SELF_TEST)["jobs"]["docker"]["with"]
        text = SELF_TEST.read_text(encoding="utf-8")
        self.assertEqual(given["second-image"], "fleet-self-test-second")
        self.assertEqual(given["second-dockerfile"], "tests/fixtures/docker/Dockerfile.second")
        self.assertEqual(given["second-smoke-seccomp"], "tests/fixtures/docker/seccomp.json")
        self.assertEqual(given["second-smoke-shm-size"], "128m")
        self.assertEqual(given["compose-file"], "tests/fixtures/docker/compose.yaml")
        self.assertIn("second-smoke-command: |\n        sh\n        /smoke.sh\n        two words\n", text)
        self.assertIn("compose-env: |\n        FLEET_COMPOSE=on\n", text)
        for name in ("second-dockerfile", "second-smoke-seccomp", "compose-file"):
            self.assertTrue((cli.ROOT / given[name]).is_file(), name)

    def test_the_second_fixture_passes_only_when_run_as_the_check_runs_it(self) -> None:
        dockerfile = (FIXTURES / "Dockerfile.second").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"(?m)^USER 65534")
        self.assertRegex(dockerfile, r'(?m)^ENTRYPOINT \["false"\]$')
        self.assertIn("COPY second-smoke.sh /smoke.sh", dockerfile)
        smoke = (FIXTURES / "second-smoke.sh").read_text(encoding="utf-8")
        for needed in ('"$1" = "two words"', "NoNewPrivs:", "CapBnd:[[:space:]]*0000000000000000", "mkdir /tmp/refused",
                       "/dev/shm", "131072"):
            self.assertIn(needed, smoke)
        profile = json.loads((FIXTURES / "seccomp.json").read_text(encoding="utf-8"))
        self.assertEqual(profile["defaultAction"], "SCMP_ACT_ALLOW")
        self.assertEqual(profile["syscalls"], [{"names": ["mkdir", "mkdirat"], "action": "SCMP_ACT_ERRNO"}])

    @unittest.skipUnless(shutil.which("docker"), "needs the docker CLI (not its daemon)")
    def test_the_compose_fixture_needs_compose_env(self) -> None:
        compose = FIXTURES / "compose.yaml"
        clean = {key: value for key, value in os.environ.items() if key != "FLEET_COMPOSE"}
        run = ["docker", "compose", f"--file={compose}", "config", "--quiet"]
        missing = subprocess.run(run, env=clean, capture_output=True, text=True, check=False)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("FLEET_COMPOSE", missing.stderr)
        given = subprocess.run(run, env={**clean, "FLEET_COMPOSE": "on"}, capture_output=True, text=True, check=False)
        self.assertEqual(given.returncode, 0, given.stderr)


def docs_section() -> str:
    text = ADOPTING.read_text(encoding="utf-8")
    found = re.search(r"^## Docker image\n(.*?)(?=^## )", text, re.MULTILINE | re.DOTALL)
    assert found
    return found.group(1)


def called_permissions() -> dict[str, str]:
    union: dict[str, str] = {}
    for job in jobs().values():
        for scope, level in job["permissions"].items():
            if level == "write" or scope not in union:
                union[scope] = level
    return union


class CallerGrants(unittest.TestCase):
    def test_a_caller_grants_every_called_jobs_permissions_or_the_run_cannot_start(self) -> None:
        self.assertEqual(called_permissions(), {"contents": "write", "packages": "write"})
        self.assertEqual(parsed(SELF_TEST)["jobs"]["docker"]["permissions"], called_permissions())


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
        self.assertEqual(build["permissions"], called_permissions())
        self.assertEqual(publish["permissions"], called_permissions())
        self.assertEqual(publish["with"]["publish"], "true")
        self.assertEqual(publish["needs"], "[test, web, build]")
        self.assertEqual(publish["if"], "needs.build.outputs.skip != 'true'")
        self.assertEqual(build["if"], "github.event_name != 'pull_request' && github.ref == 'refs/heads/main'")
        for name in ("version", "new", "newest"):
            self.assertEqual(publish["with"][name], f"${{{{ needs.build.outputs.{name} }}}}")
        self.assertNotIn("secrets", build)
        self.assertNotIn("secrets", publish)

    def test_darkrooms_caller_passes_the_second_image_on_both_calls(self) -> None:
        blocks = re.findall(r"```yaml\n(.*?)```", docs_section(), re.DOTALL)
        self.assertEqual(len(blocks), 2)
        caller: Any = self_check.parse(blocks[1].replace("@<commit>", "@" + "0" * 40))
        assert isinstance(caller, dict)
        build, publish = caller["jobs"]["build"], caller["jobs"]["publish"]
        inputs = set(parsed(DOCKER)["on"]["workflow_call"]["inputs"])
        for call in (build, publish):
            self.assertEqual(call["permissions"], called_permissions())
            self.assertEqual(call["uses"], "AnthonyPluth/fleet/.github/workflows/docker.yml@" + "0" * 40)
            self.assertLessEqual(set(call["with"]), inputs)
            self.assertNotIn("secrets", call)
            for name in ("image", "second-image", "second-dockerfile"):
                self.assertEqual(call["with"][name], build["with"][name])
        self.assertNotIn("if", build)
        self.assertEqual(build["with"]["second-image"], "darkroom-browser")
        self.assertEqual(build["with"]["compose-file"], "docker-compose.yml")
        self.assertIn("second-smoke-command: |\n        bash\n        /app/smoke.sh\n", blocks[1])
        self.assertEqual(publish["with"]["publish"], "true")
        self.assertEqual(publish["needs"], "[build]")
        self.assertEqual(publish["if"], "github.event_name != 'pull_request' && needs.build.outputs.skip != 'true'")
        for name in ("version", "new", "newest"):
            self.assertEqual(publish["with"][name], f"${{{{ needs.build.outputs.{name} }}}}")
        self.assertFalse(set(publish["with"]) & {"second-smoke-command", "second-smoke-seccomp", "compose-file"})


if __name__ == "__main__":
    unittest.main()
