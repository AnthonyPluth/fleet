import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "security-changes.sh"
FAKES = ROOT / "tests" / "fakes" / "security"
FIXTURES = ROOT / "tests" / "fixtures" / "security"
REPO = "example/app"
ZERO = "0" * 40
BEFORE = "a" * 40
AFTER = "b" * 40


def files_api(files: list[str], renamed_from: list[str] | None = None) -> list[dict[str, str]]:
    listed = [{"filename": name} for name in files]
    for entry, previous in zip(listed, renamed_from or [], strict=False):
        entry["previous_filename"] = previous
    return listed


class Changes:
    def __init__(self, test: unittest.TestCase, inputs: dict[str, str]) -> None:
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.inputs = inputs
        self.answers = self.directory / "answers.json"
        self.log = self.directory / "log.jsonl"
        self.output = self.directory / "output.txt"

    def run(self, answers: dict[str, Any], **event: str) -> subprocess.CompletedProcess[str]:
        self.answers.write_text(json.dumps(answers), encoding="utf-8")
        self.output.write_text("", encoding="utf-8")
        env = {
            **os.environ,
            "PATH": f"{FAKES}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_ANSWERS": str(self.answers),
            "FAKE_GH_LOG": str(self.log),
            "GITHUB_OUTPUT": str(self.output),
            "GH_TOKEN": "fake",
            "REPO": REPO,
            "CODE_PATHS": self.inputs["code-paths"],
            "WEB_DIRS": self.inputs["web-dirs"],
            "IMAGE_PATHS": self.inputs["image-paths"],
            "EVENT": "",
            "PR": "",
            "BEFORE": "",
            "AFTER": "",
            **event,
        }
        return subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True, check=False)

    def outputs(self) -> dict[str, bool]:
        pairs = [line.split("=", 1) for line in self.output.read_text(encoding="utf-8").splitlines() if line]
        return {key: value == "true" for key, value in pairs}

    def pull_request(self, files: list[str], renamed_from: list[str] | None = None, number: str = "7") -> dict[str, bool]:
        answers = {f"GET repos/{REPO}/pulls/{number}/files?per_page=100": files_api(files, renamed_from)}
        result = self.run(answers, EVENT="pull_request", PR=number)
        assert result.returncode == 0, result.stderr
        return self.outputs()


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


class AppFixturesTest(unittest.TestCase):
    def check_app(self, name: str) -> None:
        data = fixture(name)
        changes = Changes(self, data["inputs"])
        for case in data["cases"]:
            with self.subTest(app=name, case=case["name"]):
                outputs = changes.pull_request(case["files"], case.get("renamed_from"))
                self.assertEqual(outputs, case["expect"])

    def test_waypoint(self) -> None:
        self.check_app("waypoint")

    def test_runway(self) -> None:
        self.check_app("runway")

    def test_darkroom(self) -> None:
        self.check_app("darkroom")

    def test_every_fixture_has_the_four_outputs(self) -> None:
        for path in sorted(FIXTURES.glob("*.json")):
            for case in fixture(path.stem)["cases"]:
                self.assertEqual(set(case["expect"]), {"code", "web_deps", "image", "workflows"}, f"{path.name}: {case['name']}")


class EventsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.changes = Changes(self, fixture("waypoint")["inputs"])

    def test_a_push_compares_with_its_before_commit(self) -> None:
        answers = {f"GET repos/{REPO}/compare/{BEFORE}...{AFTER}": {"files": files_api(["frontend/package.json"])}}
        result = self.changes.run(answers, EVENT="push", BEFORE=BEFORE, AFTER=AFTER)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.changes.outputs(), {"code": False, "web_deps": True, "image": False, "workflows": False})

    def test_a_push_without_a_before_commit_scans_everything(self) -> None:
        result = self.changes.run({}, EVENT="push", BEFORE=ZERO, AFTER=AFTER)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.changes.outputs(), {"code": True, "web_deps": True, "image": True, "workflows": True})
        self.assertFalse(self.changes.log.exists())

    def test_a_scheduled_run_scans_everything_without_asking(self) -> None:
        result = self.changes.run({}, EVENT="schedule")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.changes.outputs(), {"code": True, "web_deps": True, "image": True, "workflows": True})
        self.assertFalse(self.changes.log.exists())

    def test_a_scheduled_run_skips_the_scans_the_app_has_no_inputs_for(self) -> None:
        changes = Changes(self, fixture("darkroom")["inputs"])
        result = changes.run({}, EVENT="workflow_dispatch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(changes.outputs(), {"code": True, "web_deps": False, "image": False, "workflows": True})

    def test_a_change_too_big_to_list_scans_everything(self) -> None:
        files = [f"docs/page-{index}.md" for index in range(3000)]
        self.assertEqual(self.changes.pull_request(files), {"code": True, "web_deps": True, "image": True, "workflows": True})
        self.assertEqual(self.changes.pull_request(files[:2999]), {"code": False, "web_deps": False, "image": False, "workflows": False})

    def test_a_push_too_big_to_compare_scans_everything(self) -> None:
        files = files_api([f"docs/page-{index}.md" for index in range(300)])
        answers = {f"GET repos/{REPO}/compare/{BEFORE}...{AFTER}": {"files": files}}
        result = self.changes.run(answers, EVENT="push", BEFORE=BEFORE, AFTER=AFTER)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.changes.outputs(), {"code": True, "web_deps": True, "image": True, "workflows": True})

    def test_a_failed_listing_fails_the_job(self) -> None:
        result = self.changes.run({}, EVENT="pull_request", PR="7")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HTTP 404", result.stderr)

    def test_several_web_dirs_and_regex_characters_in_their_names(self) -> None:
        changes = Changes(self, {"code-paths": "^app/", "web-dirs": "web.app/\nextension", "image-paths": ""})
        self.assertEqual(changes.pull_request(["web.app/package.json"])["web_deps"], True)
        self.assertEqual(changes.pull_request(["extension/package-lock.json"])["web_deps"], True)
        self.assertEqual(changes.pull_request(["webXapp/package.json"])["web_deps"], False)
        self.assertEqual(changes.pull_request(["package.json"])["web_deps"], False)

    def test_code_paths_are_required(self) -> None:
        changes = Changes(self, {"code-paths": "", "web-dirs": "", "image-paths": ""})
        result = changes.run({}, EVENT="schedule")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CODE_PATHS", result.stderr)


if __name__ == "__main__":
    unittest.main()
