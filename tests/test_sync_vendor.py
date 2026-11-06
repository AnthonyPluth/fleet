import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from consumers import (
    FIXTURE_COMMIT,
    OFFLINE_TAGS,
    RUNWAY,
    WAYPOINT,
    commit,
    copy,
    git,
    temporary,
    write,
)

from fleet_tools import agents_md, cli, drift, vendor
from fleet_tools.checks import skills

ROOT = Path(__file__).resolve().parent.parent
VERSION = agents_md.own_version()
PLUGIN = ROOT / "plugins/fleet"
BASE_SKILLS = {"fix-with-checks", "design-signoff", "agent-task", "review-guidance"}
REGENERATE = "regenerate it with `poetry run fleet sync --to v$(cat VERSION) --root {}`"


def setUpModule() -> None:
    OFFLINE_TAGS.start()


def tearDownModule() -> None:
    OFFLINE_TAGS.stop()


def shipped() -> dict[Path, vendor.Vendored]:
    found = vendor.shipped_at(VERSION)
    assert found is not None
    return found


def skill_names() -> set[str]:
    return {path.parent.name for path in (PLUGIN / "skills").glob("*/SKILL.md")}


def sync(root: Path) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["sync", "--to", VERSION, "--root", str(root)])
    return code, out.getvalue(), err.getvalue()


class Plugin(unittest.TestCase):
    def test_the_marketplace_lists_the_one_plugin(self) -> None:
        marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(marketplace["name"], "fleet")
        self.assertEqual([(plugin["name"], plugin["source"]) for plugin in marketplace["plugins"]],
                         [("fleet", "./plugins/fleet")])

    def test_the_plugin_version_is_fleets(self) -> None:
        manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["name"], "v" + manifest["version"]), ("fleet", VERSION))

    def test_the_hook_is_declared_as_session_start(self) -> None:
        hooks = json.loads((PLUGIN / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(list(hooks), ["SessionStart"])
        self.assertEqual([hook["command"] for entry in hooks["SessionStart"] for hook in entry["hooks"]],
                         ['"${CLAUDE_PLUGIN_ROOT}/hooks/session-start.sh"'])
        self.assertTrue(os.access(PLUGIN / "hooks/session-start.sh", os.X_OK))

    def test_fleets_own_sessions_run_the_plugins_hook(self) -> None:
        settings = json.loads((ROOT / ".claude/settings.json").read_text(encoding="utf-8"))
        self.assertEqual([hook["command"] for entry in settings["hooks"]["SessionStart"] for hook in entry["hooks"]],
                         ['"$CLAUDE_PROJECT_DIR/plugins/fleet/hooks/session-start.sh"'])
        self.assertNotIn("enabledPlugins", settings)
        self.assertIn("PYTHON_VERSION=3.11\n", (ROOT / ".claude/hooks/fleet.conf").read_text(encoding="utf-8"))

    def test_the_base_skills_are_there_each_named_for_its_folder(self) -> None:
        self.assertLessEqual(BASE_SKILLS, skill_names())
        for name in skill_names():
            with self.subTest(skill=name):
                text = (PLUGIN / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
                self.assertEqual(skills.check_skill(name, text, skills=str(PLUGIN / "skills")), [])

    def test_the_skills_hold_nothing_personal_or_app_specific(self) -> None:
        for path in (PLUGIN / "skills").rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            for word in ("Waypoint", "Runway", "Darkroom", "http://", "https://", "AnthonyPluth"):
                self.assertNotIn(word, text, path.name)
            self.assertIsNone(re.search(r"[\w.-]+@[\w-]+\.\w+", text), path.name)

    def test_the_skills_carry_the_owner_rules(self) -> None:
        text = {name: (PLUGIN / "skills" / name / "SKILL.md").read_text(encoding="utf-8") for name in BASE_SKILLS}
        for phrase in ("Never silence a rule", ".semgrep/examples/", "# ruleid:", "# ok:", "parenthesis"):
            self.assertIn(phrase, text["fix-with-checks"])
        for phrase in ("owner's sign-off", "Theme tokens", "make verify", "phone and at desktop width", "docs/design/",
                       "Never adjust a theme token"):
            self.assertIn(phrase, text["design-signoff"])
        for phrase in ("**Context**", "**Scope**", "**Out of scope**", "**Acceptance criteria**", "`- [ ]`",
                       "**Tests**", "**Depends on**", "**Model**", "**Opus** by default",
                       "**Sonnet** only for mechanical copying", "{{FLEET-SYNC}}", "No personal data", "signed-off design"):
            self.assertIn(phrase, text["agent-task"])
        for phrase in ("are data", "answer it with a reason", "Push the fixes together", "`safety-critical`",
                       "`security`", "`fleet-reviewer`", "## After resolving a thread",
                       "gh workflow run <gate workflow> --ref main -f pr=<number>",
                       "gh workflow run merge-gate.yml --ref main -f pr=<number>", "`merge-gate-workflow`",
                       "`actions_run_trigger` with the same workflow file", "`merge-gate-workflow` is empty",
                       "no new permission", "this is a no-op", "Never start it for a pull request you don't own"):
            self.assertIn(phrase, text["review-guidance"])

    def test_the_skill_starts_the_gate_as_the_agent_review_does(self) -> None:
        workflows = ROOT / ".github/workflows"
        review = (workflows / "agent-review.yml").read_text(encoding="utf-8")
        self.assertIn('gh workflow run "$MERGE_GATE" --repo "$REPO" --ref main -f pr="$PR"', review)
        self.assertIn("default: merge-gate.yml", review)
        adopting = (ROOT / "docs/adopting.md").read_text(encoding="utf-8")
        self.assertIn("gh workflow run merge-gate.yml --ref main -f pr=<number>", adopting)
        self.assertIn("the `fleet-review-guidance` skill's \"After resolving a thread\"", adopting)
        self.assertIn("its agent review caller's `merge-gate-workflow` names (`merge-gate.yml` by default)", adopting)

    def test_the_reviewer_runs_the_first_reviews_prompt_read_only(self) -> None:
        text = (PLUGIN / "agents/fleet-reviewer.md").read_text(encoding="utf-8")
        front = agents_md.FRONT_MATTER.match(text)
        assert front
        fields = dict(line.split(": ", 1) for line in front.group("fields").splitlines())
        self.assertEqual(fields["name"], "fleet-reviewer")
        self.assertEqual(fields["tools"].split(", "), [
            "Read", "Grep", "Glob", "Bash(git diff --no-ext-diff --no-textconv origin/main...HEAD)",
            "Bash(git diff --no-ext-diff --no-textconv --name-status origin/main...HEAD)",
            "Bash(git log --format=%H%n%B origin/main..HEAD)", "Bash(git diff --no-ext-diff --no-textconv --stat HEAD)"])
        self.assertNotIn("*", fields["tools"])
        prompt = (ROOT / ".github/agent-review/prompt.md").read_text(encoding="utf-8")
        self.assertIn(prompt, text, "copy .github/agent-review/prompt.md into plugins/fleet/agents/fleet-reviewer.md "
                                    "word for word, after the preface")
        self.assertIn("git diff --no-ext-diff --no-textconv origin/main...HEAD", text)
        self.assertIn(".github/agent-review/context.md", text)
        schema = json.loads((ROOT / ".github/agent-review/schema.json").read_text(encoding="utf-8"))
        finding = schema["properties"]["findings"]["items"]["properties"]
        for field in [*schema["properties"], *finding]:
            self.assertIn(f"`{field}`", text.split(prompt)[1])


class Shipped(unittest.TestCase):
    def test_every_listed_file_is_vendored(self) -> None:
        targets = set(shipped())
        expected = {Path(".claude/skills", f"fleet-{name}", "SKILL.md") for name in skill_names()}
        expected |= {Path(".claude/agents/fleet-reviewer.md"), Path(".claude/hooks/fleet-session-start.sh"),
                     Path("tools/fleet")}
        self.assertLessEqual(expected, targets)
        self.assertEqual({target for target in targets if not target.as_posix().startswith(".claude/skills/")},
                         {Path(".claude/agents/fleet-reviewer.md"), Path(".claude/hooks/fleet-session-start.sh"),
                          Path("tools/fleet")})
        self.assertEqual({vendored.target for vendored in shipped().values() if vendored.executable},
                         {Path(".claude/hooks/fleet-session-start.sh"), Path("tools/fleet")})

    def test_each_starts_with_the_vendored_line(self) -> None:
        for target, vendored in shipped().items():
            with self.subTest(target=target):
                lines = vendored.text.splitlines()
                line = lines[1] if lines[0] in ("#!/bin/bash", "---") else lines[0]
                self.assertIn(f"vendored from fleet {VERSION} ({vendored.source.as_posix()}) by `fleet sync`; edit it "
                              "in fleet, not here", line)
                self.assertTrue(vendor.NOTED.match(vendored.text))

    def test_a_vendored_skill_is_prefixed_and_well_formed(self) -> None:
        for target, vendored in shipped().items():
            if target.name == "SKILL.md":
                with self.subTest(target=target):
                    self.assertTrue(target.parent.name.startswith("fleet-"))
                    self.assertEqual(skills.check_skill(target.parent.name, vendored.text), [])
                    self.assertIn(f"\nname: {target.parent.name}\n", vendored.text)

    def test_a_skill_named_otherwise_than_its_folder_is_refused(self) -> None:
        with self.assertRaises(vendor.VendorError):
            vendor.render({Path("plugins/fleet/skills/x/SKILL.md"): "---\nname: y\ndescription: d\n---\nBody\n"}, VERSION)

    def test_a_skills_other_files_go_with_it(self) -> None:
        found = vendor.render({Path("plugins/fleet/skills/x/references/more.md"): "Text\n"}, VERSION)
        vendored = found[Path(".claude/skills/fleet-x/references/more.md")]
        self.assertTrue(vendored.text.startswith("<!-- vendored from fleet ") and vendored.text.endswith(" -->\nText\n"))


class Fixtures(unittest.TestCase):
    def test_each_fixture_consumer_carries_the_vendored_files(self) -> None:
        for consumer in (WAYPOINT, RUNWAY):
            for target, vendored in shipped().items():
                with self.subTest(consumer=consumer.name, target=target):
                    path = consumer / target
                    self.assertEqual(path.read_text(encoding="utf-8") if path.is_file() else None, vendored.text,
                                     REGENERATE.format(consumer.relative_to(ROOT)))
                    self.assertEqual(os.access(path, os.X_OK), vendored.executable)

    def test_each_fixture_consumer_has_a_fleet_conf(self) -> None:
        for consumer in (WAYPOINT, RUNWAY):
            text = (consumer / ".claude/hooks/fleet.conf").read_text(encoding="utf-8")
            self.assertIn("PYTHON_VERSION=3.14\n", text)
            self.assertIn('APT_PACKAGES="libfaketime"\n', text)


class Sync(unittest.TestCase):
    def test_sync_vendors_every_file_and_a_second_run_changes_nothing(self) -> None:
        root = temporary(self)
        code, out, err = sync(root)
        self.assertEqual((code, err), (0, ""))
        for target, vendored in shipped().items():
            self.assertIn(f"{target.as_posix()}: wrote ({vendored.source.as_posix()})\n", out)
            self.assertEqual((root / target).read_text(encoding="utf-8"), vendored.text)
            self.assertEqual(os.access(root / target, os.X_OK), vendored.executable)
        self.assertEqual(drift.drift(root)[0], [])
        code, out, _ = sync(root)
        self.assertEqual((code, out), (0, f"fleet sync: {root.name} is at fleet {VERSION} already; nothing changed.\n"))

    def test_sync_replaces_an_edited_file_and_restores_the_mode(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        write(root, ".claude/skills/fleet-agent-task/SKILL.md", "edited\n")
        (root / "tools/fleet").chmod(0o644)
        _, out, _ = sync(root)
        self.assertIn(".claude/skills/fleet-agent-task/SKILL.md: replaced (plugins/fleet/skills/agent-task/SKILL.md)\n",
                      out)
        self.assertIn("tools/fleet: made executable\n", out)
        self.assertEqual(drift.drift(root)[0], [])

    def test_sync_repairs_a_file_that_is_not_utf_8(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        target = root / ".claude/agents/fleet-reviewer.md"
        target.write_bytes(b"\xff\xfe broken \x80\n")
        _, out, err = sync(root)
        self.assertEqual(err, "")
        self.assertIn(".claude/agents/fleet-reviewer.md: replaced (plugins/fleet/agents/fleet-reviewer.md)\n", out)
        self.assertEqual(drift.drift(root)[0], [])

    def test_sync_removes_a_retired_vendored_skill_but_never_the_apps_own_file(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        retired = shipped()[Path(".claude/skills/fleet-agent-task/SKILL.md")].text
        write(root, ".claude/skills/fleet-retired/SKILL.md", retired.replace("fleet-agent-task", "fleet-retired"))
        write(root, ".claude/hooks/fleet-mine.sh", "echo mine\n")
        _, out, _ = sync(root)
        self.assertIn(".claude/skills/fleet-retired/SKILL.md: removed (fleet " + VERSION + " no longer ships it)\n", out)
        self.assertFalse((root / ".claude/skills/fleet-retired").exists())
        self.assertTrue((root / ".claude/hooks/fleet-mine.sh").exists())
        self.assertTrue((root / ".claude/skills/verify-runway/SKILL.md").exists())
        self.assertTrue((root / ".claude/hooks/fleet.conf").exists())


class Drift(unittest.TestCase):
    def test_a_one_character_edit_in_any_vendored_file_fails(self) -> None:
        for target, vendored in shipped().items():
            root = copy(self, WAYPOINT, repository=False)
            path = root / target
            text = path.read_text(encoding="utf-8")
            at = len(text) // 2
            path.write_text(text[:at] + ("x" if text[at] != "x" else "y") + text[at + 1:], encoding="utf-8")
            with self.subTest(target=target):
                self.assertEqual(drift.drift(root)[0], [
                    f"{target.as_posix()} differs from fleet's at {VERSION}; edit it in fleet "
                    f"({vendored.source.as_posix()}), not here, or run `fleet sync`"])

    def test_a_file_that_is_not_utf_8_is_drift(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        (root / "tools/fleet").write_bytes(b"#!/bin/bash\n\xff\xfe\x80\n")
        write(root, ".claude/skills/fleet-stray/SKILL.md", "x").write_bytes(b"\xff\xfe")
        self.assertEqual(drift.drift(root)[0], [
            f"tools/fleet differs from fleet's at {VERSION}; edit it in fleet (shim/fleet), not here, or run `fleet sync`",
            f".claude/skills/fleet-stray/SKILL.md isn't a file fleet {VERSION} vendors (the fleet- prefix is fleet's: "
            "rename the app's own file)"])

    def test_a_missing_or_non_executable_file_fails(self) -> None:
        root = copy(self, RUNWAY, repository=False)
        (root / ".claude/agents/fleet-reviewer.md").unlink()
        (root / ".claude/hooks/fleet-session-start.sh").chmod(0o644)
        self.assertEqual(drift.drift(root)[0], [
            f".claude/agents/fleet-reviewer.md doesn't exist (fleet sync --to {VERSION} vendors it)",
            ".claude/hooks/fleet-session-start.sh isn't executable (fleet sync makes it so)"])

    def test_a_file_under_fleets_prefix_that_fleet_does_not_ship_fails(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        write(root, ".claude/skills/fleet-mine/SKILL.md", "---\nname: fleet-mine\ndescription: d\n---\nBody\n")
        self.assertEqual(drift.drift(root)[0], [
            f".claude/skills/fleet-mine/SKILL.md isn't a file fleet {VERSION} vendors (the fleet- prefix is fleet's: "
            "rename the app's own file)"])

    def test_the_apps_own_skills_and_config_are_not_checked(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        write(root, ".claude/skills/verify-waypoint/SKILL.md", "anything\n")
        write(root, ".claude/hooks/fleet.conf", "PYTHON_VERSION=3.13\n")
        write(root, ".claude/hooks/session-start.local.sh", "true\n")
        self.assertEqual(drift.drift(root)[0], [])


class OtherVersions(unittest.TestCase):
    def fleet(self, with_plugin: bool) -> Path:
        root = temporary(self) / "fleet"
        for source in [*vendor.working_tree(ROOT)] if with_plugin else []:
            target = root / source
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / source, target)
        write(root, "README.md", "An older fleet.\n")
        git(root, "init", "-q", "-b", "main")
        commit(root, "chore: an older fleet")
        git(root, "tag", "v0.0.5")
        return root

    def test_files_at_an_older_tag_are_read_from_fleets_history(self) -> None:
        fleet = self.fleet(with_plugin=True)
        path = fleet / "plugins/fleet/skills/agent-task/SKILL.md"
        path.write_text(path.read_text(encoding="utf-8") + "Newer.\n", encoding="utf-8")
        found = vendor.shipped_at("v0.0.5", fleet)
        assert found is not None
        text = found[Path(".claude/skills/fleet-agent-task/SKILL.md")].text
        self.assertIn("vendored from fleet v0.0.5 ", text)
        self.assertNotIn("Newer.", text)

    def test_a_version_from_before_vendoring_checks_no_vendored_file(self) -> None:
        fleet = self.fleet(with_plugin=False)
        self.assertEqual(vendor.shipped_at("v0.0.5", fleet), {})
        root = copy(self, WAYPOINT, repository=False)
        with mock.patch.object(agents_md, "ROOT", fleet):
            self.assertEqual(vendor.check(root, "v0.0.5"), (0, []))

    def test_an_unknown_tag_cannot_be_compared(self) -> None:
        fleet = self.fleet(with_plugin=True)
        root = copy(self, WAYPOINT, repository=False)
        with mock.patch.object(agents_md, "ROOT", fleet):
            self.assertEqual(vendor.check(root, "v9.9.9")[1], [
                f"can't read fleet's vendored files at v9.9.9 from fleet {VERSION}; run fleet drift from fleet at v9.9.9"])


class Shim(unittest.TestCase):
    def run_shim(self, with_uvx: bool, version_file: str | None = None) -> tuple[subprocess.CompletedProcess[str], str]:
        root = copy(self, RUNWAY, repository=False)
        if version_file is not None:
            write(root, ".fleet/version", version_file)
        base = temporary(self)
        bin_dir, home, log = base / "bin", base / "home", base / "calls.log"
        bin_dir.mkdir()
        home.mkdir()
        uvx = '#!/bin/bash\necho "uvx $*" >> "$LOG"\n'
        write(base, "uvx-stub", uvx).chmod(0o755)
        if with_uvx:
            shutil.copy2(base / "uvx-stub", bin_dir / "uvx")
        python = ('#!/bin/bash\necho "python3 $*" >> "$LOG"\nmkdir -p "$HOME/.local/bin"\n'
                  f'cp "{base}/uvx-stub" "$HOME/.local/bin/uvx"\n')
        write(bin_dir, "python3", python).chmod(0o755)
        for tool in ("bash", "cat", "dirname", "mkdir", "cp"):
            found = shutil.which(tool)
            assert found
            os.symlink(found, bin_dir / tool)
        done = subprocess.run([str(root / "tools/fleet"), "drift", "--root", "."], capture_output=True, text=True,
                              env={"PATH": str(bin_dir), "HOME": str(home), "LOG": str(log)}, check=False, timeout=30)
        return done, log.read_text(encoding="utf-8") if log.is_file() else ""

    def test_the_shim_runs_the_cli_at_the_consumers_version(self) -> None:
        done, calls = self.run_shim(with_uvx=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(calls, f"uvx --from git+https://github.com/AnthonyPluth/fleet@{FIXTURE_COMMIT} fleet drift --root .\n")

    def test_the_shim_refuses_a_version_file_without_a_commit(self) -> None:
        for text in (f"{VERSION}\n", f"{VERSION}\n{FIXTURE_COMMIT[:39]}\n", f"{VERSION}\n{FIXTURE_COMMIT.upper()}\n", ""):
            with self.subTest(text=text):
                done, calls = self.run_shim(with_uvx=True, version_file=text)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertEqual(calls, "")
                self.assertIn(".fleet/version names no commit on its line 2", done.stderr)
                self.assertIn('fleet@<the commit your fleet workflow pins name>" fleet sync --to', done.stderr)

    def test_the_shim_installs_uv_when_missing(self) -> None:
        done, calls = self.run_shim(with_uvx=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(calls.splitlines(), ["python3 -m pip install --quiet --user uv==0.12.21",
                                              f"uvx --from git+https://github.com/AnthonyPluth/fleet@{FIXTURE_COMMIT} fleet "
                                              "drift --root ."])
