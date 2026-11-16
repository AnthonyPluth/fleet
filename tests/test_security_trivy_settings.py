import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fleet_tools import cli, self_check

SECURITY = cli.ROOT / ".github" / "workflows" / "security.yml"
SELF_TEST = cli.ROOT / ".github" / "workflows" / "self-test.yml"
SCRIPT = cli.ROOT / ".github" / "scripts" / "security-trivy-settings.sh"
STEP = "Keep the checkout's own Trivy settings out of the scan"
NAMES = ("trivy.yaml", ".trivyignore", "trivy-secret.yaml")
SECRET_CONFIG = ".git"
SETTINGS = "${{ runner.temp }}/trivy-settings"
SCAN_ENV = {"TRIVY_CONFIG": f"{SETTINGS}/trivy.yaml", "TRIVY_IGNOREFILE": f"{SETTINGS}/trivyignore",
            "TRIVY_SECRET_CONFIG": f"{SETTINGS}/{SECRET_CONFIG}"}
SCANS = {"trivy": "Scan the repository", "image": "Scan the image"}
CHECKOUT = {"trivy": "keep", "image": "remove"}
CACHE = "${{ runner.temp }}/trivy-cache"
ACTION = "aquasecurity/trivy-action@"
Node = dict[str, Any]


def workflow(path: Path = SECURITY) -> Node:
    parsed = self_check.parse(path.read_text(encoding="utf-8"))
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


class WorkflowTest(unittest.TestCase):
    def test_both_trivy_jobs_run_the_step_on_every_event_whatever_the_inputs(self) -> None:
        for job in SCANS:
            step = steps(job)[index_of(steps(job), STEP)]
            self.assertEqual(step, {"name": STEP, "env": {"TRIVY_SETTINGS": SETTINGS, "CHECKOUT_SETTINGS": CHECKOUT[job]},
                                    "run": ".fleet/.github/scripts/security-trivy-settings.sh"}, job)

    def test_only_the_job_that_scans_no_checkout_file_removes_the_checkouts_files(self) -> None:
        self.assertEqual(CHECKOUT, {"trivy": "keep", "image": "remove"})
        files = steps("trivy")[index_of(steps("trivy"), SCANS["trivy"])]["with"]
        self.assertEqual(files["scan-type"], "fs")
        self.assertNotIn("scan-ref", files)
        self.assertIn("secret", files["scanners"].split(","))
        image = steps("image")[index_of(steps("image"), SCANS["image"])]["with"]
        self.assertEqual(image["image-ref"], "app:scan")
        self.assertNotIn("scan-type", image)
        self.assertNotIn("scan-ref", image)

    def test_trivys_database_cache_is_outside_the_workspace_in_every_scan(self) -> None:
        for path in (SECURITY, SELF_TEST):
            scans = [step for job in workflow(path)["jobs"].values() for step in job.get("steps", [])
                     if step.get("uses", "").startswith(ACTION)]
            self.assertGreaterEqual(len(scans), 2, path.name)
            for step in scans:
                self.assertEqual(step["with"]["cache-dir"], CACHE, step["name"])

    def test_the_step_runs_while_fleets_scripts_are_there_and_before_the_scan(self) -> None:
        for job, scan in SCANS.items():
            job_steps = steps(job)
            step = index_of(job_steps, STEP)
            self.assertGreater(step, index_of(job_steps, "security-trivy-install.sh"), job)
            self.assertLess(step, index_of(job_steps, "rm -rf .fleet"), job)
            self.assertLess(step, index_of(job_steps, scan), job)

    def test_the_image_is_built_before_the_checkouts_files_go(self) -> None:
        job_steps = steps("image")
        self.assertGreater(index_of(job_steps, STEP), index_of(job_steps, "Build the image"))
        after = job_steps[index_of(job_steps, STEP) + 1:]
        self.assertEqual([step for step in after if "docker/" in step.get("uses", "")], [])

    def test_each_scan_names_the_empty_settings_outside_the_workspace(self) -> None:
        for job, scan in SCANS.items():
            step = steps(job)[index_of(steps(job), scan)]
            self.assertEqual(step["env"], SCAN_ENV, job)
            self.assertTrue(step["uses"].startswith("aquasecurity/trivy-action@"), job)
            self.assertNotIn("trivy-config", step["with"], job)
            self.assertNotIn("if", step, job)

    def test_no_step_between_the_settings_and_the_scan_runs_the_checkouts_code(self) -> None:
        for job, scan in SCANS.items():
            job_steps = steps(job)
            between = job_steps[index_of(job_steps, STEP) + 1:index_of(job_steps, scan)]
            self.assertEqual([step["run"] for step in between], ["rm -rf .fleet"], job)

    def test_nothing_else_in_the_workflow_sets_trivys_settings(self) -> None:
        text = SECURITY.read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        for name in SCAN_ENV:
            self.assertEqual(code.count(name), 2, name)
        self.assertEqual(code.count("security-trivy-settings.sh"), 2)
        self.assertNotIn("rm -f trivy.yaml", code)
        self.assertNotIn("inputs.trivy-config", code)

    def test_the_header_says_what_the_scans_no_longer_read(self) -> None:
        header = " ".join(SECURITY.read_text(encoding="utf-8").split("\non:\n", 1)[0].replace("#", " ").split())
        self.assertIn("Neither Trivy job reads Trivy's settings from the caller's checkout", header)
        self.assertIn("scans them like any other file, so a secret committed in one is still reported", header)
        for name in NAMES:
            self.assertIn(f"./{name}", header)


class SelfTestTest(unittest.TestCase):
    def job(self) -> Node:
        found = workflow(SELF_TEST)["jobs"]["checkout-trivy-settings"]
        assert isinstance(found, dict)
        return found

    def test_it_scans_with_the_pinned_trivy_and_action(self) -> None:
        job = self.job()
        pins = workflow()["env"]
        for name in ("TRIVY_VERSION", "TRIVY_SHA256_X64", "TRIVY_SHA256_ARM64"):
            self.assertEqual(job["env"][name], pins[name], name)
        shipped = steps("trivy")[index_of(steps("trivy"), SCANS["trivy"])]
        scans = [step for step in job["steps"] if step.get("uses", "").startswith("aquasecurity/trivy-action@")]
        self.assertEqual(len(scans), 3)
        for step in scans:
            self.assertEqual(step["uses"], shipped["uses"])
            for same in ("skip-setup-trivy", "version", "cache-dir", "scan-type", "severity"):
                self.assertEqual(step["with"][same], shipped["with"][same], same)
            self.assertNotIn("trivyignores", step["with"])
            self.assertNotIn("trivy-config", step["with"])

    def test_the_first_scan_is_unprotected_and_the_others_take_the_shipped_environment(self) -> None:
        scans = [step for step in self.job()["steps"] if step.get("uses", "").startswith(ACTION)]
        self.assertNotIn("env", scans[0])
        self.assertEqual([step["env"] for step in scans[1:]], [SCAN_ENV, SCAN_ENV])
        self.assertEqual([step["with"]["output"].rsplit("/", 1)[1] for step in scans],
                         ["unprotected.json", "protected.json", "workspace.json"])
        self.assertEqual([step["with"]["scanners"] for step in scans], ["vuln,secret", "vuln,secret", "secret"])

    def test_the_last_scan_is_of_the_workspace_as_the_file_scan_scans_it(self) -> None:
        scans = [step for step in self.job()["steps"] if step.get("uses", "").startswith(ACTION)]
        self.assertEqual([step["with"].get("scan-ref") for step in scans],
                         ["${{ runner.temp }}/planted/app", "${{ runner.temp }}/planted/app", None])

    def test_it_runs_the_shipped_script_as_each_job_does(self) -> None:
        job_steps = self.job()["steps"]
        keep = index_of(job_steps, STEP)
        self.assertEqual(job_steps[keep]["env"], {"TRIVY_SETTINGS": SETTINGS, "CHECKOUT_SETTINGS": "keep"})
        self.assertGreater(keep, index_of(job_steps, "unprotected.json"))
        self.assertLess(keep, index_of(job_steps, "/protected.json"))
        remove = index_of(job_steps, "The image job's step removes the three")
        self.assertEqual(job_steps[remove]["env"], {"TRIVY_SETTINGS": SETTINGS, "CHECKOUT_SETTINGS": "remove"})
        self.assertGreater(remove, index_of(job_steps, "workspace.json'"))
        between = job_steps[keep + 1:remove]
        self.assertEqual([step.get("uses", "")[:len(ACTION)] for step in between], [ACTION, ACTION])

    def test_each_planted_file_drops_one_finding_and_every_scan_is_checked(self) -> None:
        text = SELF_TEST.read_text(encoding="utf-8").split("  checkout-trivy-settings:\n", 1)[1].split("\n  deep-scan-due:\n", 1)[0]
        for plant in ("CVE-2018-18074\\n' \"$password\" > \"$WORK/settings/.trivyignore\"",
                      "disable-rules:\\n  - github-pat\\n' \"$password\" > \"$WORK/settings/trivy-secret.yaml\"",
                      "printf 'scan:\\n  skip-files:\\n    - \"**/cloud.py\"\\nregistry:\\n",
                      ".github/scripts/security-trivy-settings.sh > ",
                      "for id in CVE-2018-18074 github-pat aws-access-key-id; do",
                      'if reported unprotected "$id"; then', 'reported protected "$id" ||'):
            self.assertIn(plant, text)
        self.assertEqual(text.count('cp "$WORK/settings/.trivyignore" "$WORK/settings/trivy-secret.yaml" "$WORK/settings/trivy.yaml" .'), 1)
        self.assertNotRegex(text, r"ghp_[0-9A-Za-z]{36}|AKIA[0-9A-Z]{16}|glpat-[0-9A-Za-z_-]{20}")

    def test_a_secret_inside_each_of_the_checkouts_settings_files_must_be_reported(self) -> None:
        text = SELF_TEST.read_text(encoding="utf-8").split("  checkout-trivy-settings:\n", 1)[1].split("\n  deep-scan-due:\n", 1)[0]
        self.assertIn("password=\"glpat-$(printf '%020d' 0 | tr 0 b)\"\n", text)
        for plant in ("printf '# %s\\nCVE-2018-18074\\n' \"$password\" > \"$WORK/settings/.trivyignore\"",
                      "printf '# %s\\ndisable-rules:\\n  - github-pat\\n' \"$password\" > \"$WORK/settings/trivy-secret.yaml\"",
                      "password: \"%s\"\\n' \"$password\" > \"$WORK/settings/trivy.yaml\""):
            self.assertIn(plant, text)
        self.assertIn('cmp -s "$name" "$RUNNER_TEMP/planted/settings/$name" ||', text)
        self.assertIn("          for name in trivy.yaml .trivyignore trivy-secret.yaml; do\n"
                      "            jq -e --arg name \"$name\" '[.Results[]? | select(.Target == $name) | .Secrets[]?.RuleID] | "
                      "index(\"gitlab-pat\") != null' workspace.json > /dev/null || {", text)

    def test_the_empty_secret_config_has_a_name_no_file_of_a_checkout_can_have(self) -> None:
        self.assertEqual(SECRET_CONFIG, ".git")
        for path in (SECURITY, SELF_TEST):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("trivy-settings/trivy-secret.yaml", text, path.name)
            self.assertIn(f"TRIVY_SECRET_CONFIG: ${{{{ runner.temp }}}}/trivy-settings/{SECRET_CONFIG}\n", text, path.name)


class AcceptedFindingTest(unittest.TestCase):
    def test_every_scan_with_a_list_runs_with_the_image_scans_environment(self) -> None:
        job_steps = workflow(SELF_TEST)["jobs"]["accepted-finding"]["steps"]
        shipped = steps("image")[index_of(steps("image"), SCANS["image"])]
        scans = [i for i, step in enumerate(job_steps) if step.get("uses", "").startswith(ACTION)]
        self.assertEqual(len(scans), 6)
        for i in scans:
            self.assertEqual(job_steps[i]["env"], shipped["env"], job_steps[i]["name"])
            self.assertEqual(job_steps[i]["env"], SCAN_ENV)
            self.assertTrue(job_steps[i]["with"]["trivyignores"].endswith(".accepted.yaml"))
        self.assertIn("trivyignores", shipped["with"])
        script = index_of(job_steps, STEP)
        self.assertEqual(job_steps[script], {"name": STEP, "env": {"TRIVY_SETTINGS": SETTINGS, "CHECKOUT_SETTINGS": "remove"},
                                             "run": ".github/scripts/security-trivy-settings.sh"})
        self.assertLess(script, scans[0])


class ScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.settings = self.root / "temp" / "trivy-settings"

    def run_script(self, settings: str | None = None, cwd: Path | None = None, checkout: str | None = "remove") -> subprocess.CompletedProcess[str]:
        env = {"PATH": os.environ["PATH"], "TRIVY_SETTINGS": str(self.settings) if settings is None else settings}
        if checkout is not None:
            env["CHECKOUT_SETTINGS"] = checkout
        return subprocess.run(["bash", str(SCRIPT)], cwd=cwd or self.checkout, env=env, capture_output=True, text=True, check=False)

    def warnings(self, result: subprocess.CompletedProcess[str]) -> list[str]:
        return [line for line in result.stdout.splitlines() if line.startswith("::")]

    def test_a_checkout_with_none_of_the_files_gets_the_empty_settings_and_no_warning(self) -> None:
        (self.checkout / "app.py").write_text("x = 1\n", encoding="utf-8")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.warnings(result), [])
        self.assertEqual(sorted(path.name for path in self.checkout.iterdir()), ["app.py"])
        self.assertEqual({path.name: path.read_text(encoding="utf-8") for path in self.settings.iterdir()},
                         {"trivy.yaml": "{}\n", "trivyignore": "", SECRET_CONFIG: "{}\n"})
        self.assertIn(f"Trivy reads its settings from {self.settings} (empty), not from the checkout.", result.stdout)

    def test_kept_each_of_the_three_stays_as_it_was_with_a_warning_that_names_it(self) -> None:
        text = "registry:\n  username: scanner\n"
        for name in NAMES:
            (self.checkout / name).write_text(text, encoding="utf-8")
        outside = self.root / "outside.yaml"
        outside.write_text("x\n", encoding="utf-8")
        (self.checkout / "sub").mkdir()
        (self.checkout / "sub" / "trivy.yaml").symlink_to(outside)
        result = self.run_script(checkout="keep")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({path.name: path.read_text(encoding="utf-8") for path in self.checkout.iterdir() if path.is_file()},
                         dict.fromkeys(NAMES, text))
        found = self.warnings(result)
        self.assertEqual(len(found), 3)
        for name, line in zip(NAMES, found, strict=True):
            self.assertTrue(line.startswith(f"::warning title=Trivy settings not read::This checkout has {name}. "), line)
            self.assertIn("the file scan reads it like any other file, for secrets too", line)
            self.assertIn("delete it", line)
        self.assertEqual(sorted(path.name for path in self.settings.iterdir()), [SECRET_CONFIG, "trivy.yaml", "trivyignore"])
        self.assertEqual((self.settings / "trivy.yaml").read_text(encoding="utf-8"), "{}\n")

    def test_kept_a_link_or_a_directory_of_that_name_stays_too(self) -> None:
        outside = self.root / "outside.yaml"
        outside.write_text("x\n", encoding="utf-8")
        (self.checkout / "trivy.yaml").symlink_to(outside)
        (self.checkout / ".trivyignore").symlink_to(self.root / "nowhere")
        (self.checkout / "trivy-secret.yaml").mkdir()
        result = self.run_script(checkout="keep")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.warnings(result)), 3)
        self.assertTrue((self.checkout / "trivy.yaml").is_symlink())
        self.assertTrue((self.checkout / ".trivyignore").is_symlink())
        self.assertTrue((self.checkout / "trivy-secret.yaml").is_dir())

    def test_what_happens_to_the_checkouts_files_has_to_be_said(self) -> None:
        (self.checkout / ".trivyignore").write_text("CVE-2018-18074\n", encoding="utf-8")
        for checkout in (None, "", "delete", "Keep", "keep "):
            result = self.run_script(checkout=checkout)
            self.assertEqual(result.returncode, 1, checkout)
            self.assertIn("::error::CHECKOUT_SETTINGS should be keep or remove", result.stderr, checkout)
            self.assertTrue((self.checkout / ".trivyignore").is_file(), checkout)
            self.assertFalse(self.settings.exists(), checkout)

    def test_removed_each_of_the_three_goes_with_a_warning_that_names_it(self) -> None:
        for name in NAMES:
            (self.checkout / name).write_text("CVE-2018-18074\n", encoding="utf-8")
        (self.checkout / ".trivyignore.yaml").write_text("vulnerabilities: []\n", encoding="utf-8")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(path.name for path in self.checkout.iterdir()), [".trivyignore.yaml"])
        found = self.warnings(result)
        self.assertEqual(len(found), 3)
        for name, line in zip(NAMES, found, strict=True):
            self.assertTrue(line.startswith(f"::warning title=Trivy settings not read::This checkout has {name}. "), line)
            self.assertIn("trivy-image-ignore-file", line)
            self.assertIn("delete it", line)

    def test_only_the_file_that_is_there_is_named(self) -> None:
        (self.checkout / ".trivyignore").write_text("CVE-2018-18074\n", encoding="utf-8")
        found = self.warnings(self.run_script())
        self.assertEqual(len(found), 1)
        self.assertIn("This checkout has .trivyignore. ", found[0])

    def test_a_link_or_a_directory_of_that_name_goes_and_a_links_target_stays(self) -> None:
        outside = self.root / "outside.yaml"
        outside.write_text("scan:\n  skip-files:\n    - '**'\n", encoding="utf-8")
        (self.checkout / "trivy.yaml").symlink_to(outside)
        (self.checkout / ".trivyignore").symlink_to(self.root / "nowhere")
        (self.checkout / "trivy-secret.yaml").mkdir()
        (self.checkout / "trivy-secret.yaml" / "inner").write_text("x\n", encoding="utf-8")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.checkout.iterdir()), [])
        self.assertEqual(len(self.warnings(result)), 3)
        self.assertTrue(outside.is_file())

    def test_files_in_a_subdirectory_are_not_the_working_directorys(self) -> None:
        (self.checkout / "sub").mkdir()
        (self.checkout / "sub" / ".trivyignore").write_text("CVE-2018-18074\n", encoding="utf-8")
        result = self.run_script()
        self.assertEqual(self.warnings(result), [])
        self.assertTrue((self.checkout / "sub" / ".trivyignore").is_file())

    def test_settings_left_by_an_earlier_run_are_replaced(self) -> None:
        self.settings.mkdir(parents=True)
        (self.settings / "trivyignore").write_text("CVE-2018-18074\n", encoding="utf-8")
        (self.settings / "extra").write_text("x\n", encoding="utf-8")
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(sorted(path.name for path in self.settings.iterdir()), [SECRET_CONFIG, "trivy.yaml", "trivyignore"])
        self.assertEqual((self.settings / "trivyignore").read_text(encoding="utf-8"), "")

    def test_the_settings_directory_is_absolute_and_outside_the_checkout(self) -> None:
        (self.checkout / ".trivyignore").write_text("CVE-2018-18074\n", encoding="utf-8")
        link = self.root / "link"
        link.symlink_to(self.checkout)
        for settings, reason in (("", "should be an absolute directory"), ("trivy-settings", "should be an absolute directory"),
                                 (str(self.checkout / "settings"), "inside the working directory"),
                                 (str(self.checkout), "inside the working directory"),
                                 (str(link / "settings"), "inside the working directory")):
            result = self.run_script(settings)
            self.assertEqual(result.returncode, 1, settings)
            self.assertIn("::error::TRIVY_SETTINGS ", result.stderr, settings)
            self.assertIn(reason, result.stderr, settings)
            self.assertTrue((self.checkout / ".trivyignore").is_file(), settings)
            self.assertFalse((self.checkout / "settings").exists(), settings)


class DocsTest(unittest.TestCase):
    def test_adopting_says_which_files_are_not_read_and_what_to_do_instead(self) -> None:
        adopting = (cli.ROOT / "docs" / "adopting.md").read_text(encoding="utf-8")
        section = adopting.split("### Trivy's settings files in the checkout\n", 1)[1].split("\n### ", 1)[0]
        for words in ("`trivy.yaml`", "`.trivyignore`", "`trivy-secret.yaml`", "on every event", "`TRIVY_CONFIG`",
                      "`TRIVY_IGNOREFILE`", "`TRIVY_SECRET_CONFIG`", "`trivy-image-ignore-file`", "skip", "warning",
                      "like any other file", "a secret committed in one is reported", "`cache-dir`"):
            self.assertIn(words, section, words)
        self.assertNotIn("every built-in secret rule runs on every file", adopting)


if __name__ == "__main__":
    unittest.main()
