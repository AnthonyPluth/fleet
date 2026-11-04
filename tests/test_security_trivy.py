import hashlib
import io
import os
import re
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fleet_tools import cli, self_check

SECURITY = cli.ROOT / ".github" / "workflows" / "security.yml"
SCRIPT = cli.ROOT / ".github" / "scripts" / "security-trivy-install.sh"
FAKES = cli.ROOT / "tests" / "fakes" / "security"
RELEASES = "https://github.com/aquasecurity/trivy/releases/download"
TRIVY_ACTION = "aquasecurity/trivy-action@"
CACHE = "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9"
CACHE_RESTORE = "actions/cache/restore@55cc8345863c7cc4c66a329aec7e433d2d1c52a9"
Node = dict[str, Any]
RETIRED_HOST = ".".join(("get", "trivy", "dev"))


def code_naming(name: str, text: str) -> list[str]:
    return [line for line in text.splitlines() if name in line and not line.lstrip().startswith("#")]


def hosts(text: str) -> set[str]:
    return {urlsplit(url).hostname or "" for url in re.findall(r"[a-z][a-z0-9+.-]*://[^\s'\"()]+", text)}


def workflow() -> Node:
    parsed = self_check.parse(SECURITY.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def steps(job: str) -> list[Node]:
    found = workflow()["jobs"][job]["steps"]
    assert isinstance(found, list)
    return found


def index_of(job_steps: list[Node], match: str) -> int:
    hits = [i for i, step in enumerate(job_steps) if match in str(step)]
    assert len(hits) == 1, (match, hits)
    return hits[0]


def install_checkout(job_steps: list[Node]) -> int:
    hits = [i for i, step in enumerate(job_steps) if "repository': 'AnthonyPluth/fleet" in str(step) and "fail-on-findings" not in str(step)]
    assert len(hits) == 1, hits
    return hits[0]


def tarball(version: str) -> bytes:
    binary = f"#!/bin/sh\necho 'Version: {version}'\n".encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in (("trivy", binary), ("LICENSE", b"license\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class Install:
    def __init__(self, test: unittest.TestCase) -> None:
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.serve = self.root / "serve"
        self.serve.mkdir()
        self.cache = self.root / "cache"
        self.bin = self.root / "bin"
        self.log = self.root / "curl.log"
        self.github_path = self.root / "github_path"
        self.github_output = self.root / "github_output"
        self.sums: dict[str, str] = {}
        for platform, arch in (("Linux-64bit", "X64"), ("Linux-ARM64", "ARM64")):
            data = tarball(f"0.70.0 {platform}")
            (self.serve / f"trivy_0.70.0_{platform}.tar.gz").write_bytes(data)
            self.sums[arch] = hashlib.sha256(data).hexdigest()

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        self.github_path.write_text("", encoding="utf-8")
        self.github_output.write_text("", encoding="utf-8")
        full = {
            "PATH": f"{FAKES}:/usr/bin:/bin",
            "FAKE_CURL_LOG": str(self.log),
            "FAKE_CURL_SERVE": str(self.serve),
            "TRIVY_VERSION": "v0.70.0",
            "TRIVY_SHA256_X64": self.sums["X64"],
            "TRIVY_SHA256_ARM64": self.sums["ARM64"],
            "TRIVY_CACHE": str(self.cache),
            "TRIVY_BIN": str(self.bin),
            "RUNNER_OS": "Linux",
            "RUNNER_ARCH": "X64",
            "GITHUB_PATH": str(self.github_path),
            "GITHUB_OUTPUT": str(self.github_output),
            **env,
        }
        return subprocess.run(["bash", str(SCRIPT)], env=full, capture_output=True, text=True, check=False)

    def output(self) -> str:
        return self.github_output.read_text(encoding="utf-8")

    def downloads(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line.split()[-1] for line in self.log.read_text(encoding="utf-8").splitlines()]


class InstallScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.install = Install(self)

    def test_it_downloads_the_release_tarball_from_github_checks_it_and_puts_trivy_on_the_path(self) -> None:
        result = self.install.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.install.downloads(), [f"{RELEASES}/v0.70.0/trivy_0.70.0_Linux-64bit.tar.gz"])
        self.assertEqual(self.install.github_path.read_text(encoding="utf-8"), f"{self.install.bin}\n")
        self.assertIn("Version: 0.70.0 Linux-64bit", result.stdout)
        self.assertTrue(os.access(self.install.bin / "trivy", os.X_OK))
        self.assertFalse((self.install.bin / "LICENSE").exists())
        self.assertTrue((self.install.cache / "trivy_0.70.0_Linux-64bit.tar.gz").exists())
        self.assertEqual(self.install.output(), "installed=true\n")

    def test_it_never_asks_get_trivy_dev(self) -> None:
        self.install.run()
        self.assertEqual(hosts(self.install.log.read_text(encoding="utf-8")), {"github.com"})
        self.assertEqual(hosts(SCRIPT.read_text(encoding="utf-8")), {"github.com"})
        self.assertEqual(code_naming(RETIRED_HOST, SCRIPT.read_text(encoding="utf-8")), [])

    def test_curl_is_told_https_only_and_to_fail_on_http_errors(self) -> None:
        self.install.run()
        line = self.install.log.read_text(encoding="utf-8")
        for flag in ("--fail", "--proto =https", "--location"):
            self.assertIn(flag, line)

    def test_an_arm64_runner_gets_the_arm64_tarball_and_its_own_checksum(self) -> None:
        result = self.install.run(RUNNER_ARCH="ARM64")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.install.downloads(), [f"{RELEASES}/v0.70.0/trivy_0.70.0_Linux-ARM64.tar.gz"])
        self.assertIn("Linux-ARM64", result.stdout)

    def test_a_tarball_that_does_not_match_its_checksum_is_not_installed_or_cached(self) -> None:
        result = self.install.run(TRIVY_SHA256_X64="0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("doesn't match its pinned SHA-256", result.stderr)
        self.assertEqual(self.install.github_path.read_text(encoding="utf-8"), "")
        self.assertFalse((self.install.bin / "trivy").exists())
        self.assertEqual(list(self.install.cache.iterdir()), [])
        self.assertEqual(self.install.output(), "")

    def test_the_x64_checksum_does_not_pass_an_arm64_tarball(self) -> None:
        result = self.install.run(RUNNER_ARCH="ARM64", TRIVY_SHA256_ARM64=self.install.sums["X64"])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.install.bin / "trivy").exists())

    def test_a_cached_tarball_that_matches_is_used_without_a_download(self) -> None:
        self.assertEqual(self.install.run().returncode, 0)
        self.install.log.unlink()
        result = self.install.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.install.downloads(), [])
        self.assertIn("Using the cached", result.stdout)

    def test_a_cached_tarball_that_does_not_match_is_downloaded_again(self) -> None:
        self.install.cache.mkdir()
        (self.install.cache / "trivy_0.70.0_Linux-64bit.tar.gz").write_bytes(tarball("tampered"))
        result = self.install.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("::warning::", result.stdout)
        self.assertEqual(len(self.install.downloads()), 1)
        self.assertIn("Version: 0.70.0 Linux-64bit", result.stdout)

    def test_a_download_that_fails_fails_the_step(self) -> None:
        result = self.install.run(FAKE_CURL_EXIT="6")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.install.github_path.read_text(encoding="utf-8"), "")
        self.assertEqual(self.install.output(), "")

    def test_a_runner_without_a_pinned_checksum_installs_nothing_and_leaves_it_to_trivy_action(self) -> None:
        for runner_os, arch in (("macOS", "ARM64"), ("macOS", "X64"), ("Linux", "X86"), ("Linux", "ARM"), ("Windows", "X64")):
            with self.subTest(runner_os=runner_os, arch=arch):
                result = self.install.run(RUNNER_OS=runner_os, RUNNER_ARCH=arch)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("::warning::No pinned Trivy", result.stdout)
                self.assertEqual(self.install.output(), "installed=false\n")
                self.assertEqual(self.install.github_path.read_text(encoding="utf-8"), "")
        self.assertEqual(self.install.downloads(), [])
        self.assertFalse(self.install.bin.exists())

    def test_a_pinned_platform_never_falls_back(self) -> None:
        for env in ({"TRIVY_SHA256_X64": "0" * 64}, {"FAKE_CURL_EXIT": "6"}, {"TRIVY_SHA256_X64": ""},
                    {"RUNNER_ARCH": "ARM64", "TRIVY_SHA256_ARM64": "0" * 64}):
            with self.subTest(env=env):
                self.assertNotEqual(self.install.run(**env).returncode, 0)
                self.assertNotIn("installed=false", self.install.output())

    def test_a_missing_checksum_or_a_malformed_version_fails_before_any_download(self) -> None:
        for env in ({"TRIVY_SHA256_X64": ""}, {"TRIVY_SHA256_X64": "abc"}, {"TRIVY_VERSION": "0.70.0"},
                    {"TRIVY_VERSION": "v0.70.0/../../evil"}):
            with self.subTest(env=env):
                self.assertNotEqual(self.install.run(**env).returncode, 0)
        self.assertEqual(self.install.downloads(), [])


class TrivyJobsTest(unittest.TestCase):
    def test_the_version_and_checksums_are_pinned_in_the_workflow(self) -> None:
        env = workflow()["env"]
        self.assertRegex(env["TRIVY_VERSION"], r"^v\d+\.\d+\.\d+$")
        for key in ("TRIVY_SHA256_X64", "TRIVY_SHA256_ARM64"):
            self.assertRegex(env[key], r"^[0-9a-f]{64}$")
        self.assertNotEqual(env["TRIVY_SHA256_X64"], env["TRIVY_SHA256_ARM64"])

    def test_nothing_in_the_workflow_installs_trivy_from_get_trivy_dev(self) -> None:
        text = SECURITY.read_text(encoding="utf-8")
        self.assertNotIn(RETIRED_HOST, hosts(text))
        self.assertEqual(code_naming(RETIRED_HOST, text), [])
        self.assertFalse("setup-trivy@" in text)

    def test_both_scans_skip_trivy_actions_own_install_and_install_ours_first(self) -> None:
        for job in ("trivy", "image"):
            with self.subTest(job=job):
                job_steps = steps(job)
                scan = index_of(job_steps, TRIVY_ACTION)
                install = index_of(job_steps, "security-trivy-install.sh")
                fleet = install_checkout(job_steps)
                removed = index_of(job_steps, "rm -rf .fleet")
                self.assertEqual(job_steps[install]["id"], "trivy")
                self.assertEqual(job_steps[scan]["with"]["skip-setup-trivy"], "${{ steps.trivy.outputs.installed == 'true' }}")
                self.assertEqual(job_steps[scan]["with"]["version"], "${{ env.TRIVY_VERSION }}")
                self.assertLess(fleet, install)
                self.assertLess(install, removed)
                self.assertLess(removed, scan)
                self.assertEqual(job_steps[install]["env"]["TRIVY_BIN"], "${{ runner.temp }}/trivy-bin")

    def test_the_tarball_cache_is_keyed_on_version_platform_and_checksum_and_lives_outside_the_workspace(self) -> None:
        for job, action in (("trivy", CACHE), ("image", CACHE_RESTORE)):
            with self.subTest(job=job):
                job_steps = steps(job)
                cache = index_of(job_steps, action)
                install = index_of(job_steps, "security-trivy-install.sh")
                self.assertLess(cache, install)
                key = job_steps[cache]["with"]["key"]
                for part in ("env.TRIVY_VERSION", "runner.os", "runner.arch", "env.TRIVY_SHA256_X64", "env.TRIVY_SHA256_ARM64"):
                    self.assertIn(part, key)
                self.assertEqual(job_steps[cache]["with"]["path"], "${{ runner.temp }}/trivy-tarball")
                self.assertEqual(job_steps[install]["env"]["TRIVY_CACHE"], job_steps[cache]["with"]["path"])

    def test_the_two_jobs_install_trivy_the_same_way_and_only_the_file_scan_saves_the_cache(self) -> None:
        def install_steps(job: str) -> list[Node]:
            job_steps = steps(job)
            start = install_checkout(job_steps)
            return job_steps[start:index_of(job_steps, "rm -rf .fleet") + 1]
        files, image = install_steps("trivy"), install_steps("image")
        self.assertEqual(len(files), len(image))
        for left, right in zip(files, image, strict=True):
            if left.get("uses") == CACHE:
                self.assertEqual(right["uses"], CACHE_RESTORE)
                self.assertEqual(left["with"], right["with"])
            else:
                self.assertEqual(left, right)

    def test_the_image_job_checks_out_fleet_after_the_build_so_it_stays_out_of_the_build_context(self) -> None:
        job_steps = steps("image")
        self.assertLess(index_of(job_steps, "docker/build-push-action@"),
                        install_checkout(job_steps))

    def test_what_is_scanned_is_unchanged(self) -> None:
        files = steps("trivy")[index_of(steps("trivy"), TRIVY_ACTION)]["with"]
        self.assertEqual(files["scan-type"], "fs")
        self.assertEqual(files["scanners"], "vuln,secret,misconfig")
        self.assertEqual(files["severity"], "CRITICAL,HIGH")
        self.assertNotIn("skip-dirs", files)
        self.assertNotIn("scan-ref", files)
        image = steps("image")[index_of(steps("image"), TRIVY_ACTION)]["with"]
        self.assertEqual(image["image-ref"], "app:scan")
        self.assertEqual(image["severity"], "CRITICAL,HIGH")
        self.assertEqual(image["ignore-unfixed"], "true")
        for scan in (files, image):
            self.assertEqual(scan["exit-code"], "${{ inputs.sarif-upload && '0' || '1' }}")

    def test_the_script_is_executable_and_takes_no_expression(self) -> None:
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        self.assertTrue(os.access(FAKES / "curl", os.X_OK))
        for job in ("trivy", "image"):
            job_steps = steps(job)
            self.assertNotIn("${{", job_steps[index_of(job_steps, "security-trivy-install.sh")]["run"])
        self.assertIsNone(re.search(r"\$\{\{", SCRIPT.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
