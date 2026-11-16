import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fleet_tools import cli, self_check

SECURITY = cli.ROOT / ".github" / "workflows" / "security.yml"
SELF_TEST = cli.ROOT / ".github" / "workflows" / "self-test.yml"
SCRIPT = cli.ROOT / ".github" / "scripts" / "trivy_ignore.py"
FAKES = cli.ROOT / "tests" / "fakes" / "security"
INPUT = "trivy-image-ignore-file"
SET = "${{ inputs.trivy-image-ignore-file != '' }}"
REPO = "example/app"
COMMIT = "c" * 40
TODAY = "2026-11-16"
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


def entry(identifier: str = "CVE-2026-0001", path: str = "usr/local/bin/example", statement: str = "Not linked in.",
          expires: str = "2027-01-31", extra: str = "") -> str:
    return (f"  - id: {identifier}\n    paths:\n      - {path}\n    statement: {statement}\n    expired_at: {expires}\n{extra}")


def listed(*entries: str) -> str:
    return "vulnerabilities:\n" + "".join(entries or (entry(),))


class WorkflowTest(unittest.TestCase):
    def test_the_input_is_a_string_that_defaults_to_none(self) -> None:
        option = workflow()["on"]["workflow_call"]["inputs"][INPUT]
        self.assertEqual((option["type"], option["default"]), ("string", ""))

    def test_the_image_job_reads_the_list_from_the_default_branch_before_its_scan(self) -> None:
        job = steps("image")
        read = index_of(job, "trivy_ignore.py")
        step = job[read]
        self.assertEqual(step["if"], SET)
        self.assertEqual(step["id"], "accepted")
        self.assertEqual(step["env"], {"GH_TOKEN": "${{ github.token }}", "REPO": "${{ github.repository }}",
                                       "IGNORE_FILE": "${{ inputs.trivy-image-ignore-file }}",
                                       "ACCEPTED": "${{ runner.temp }}/trivy-image-accepted.yaml"})
        self.assertEqual(step["run"], 'python3 -I .fleet/.github/scripts/trivy_ignore.py --repo="$REPO" --path="$IGNORE_FILE" --out="$ACCEPTED"')
        self.assertGreater(read, index_of(job, "Build the image"))
        self.assertLess(read, index_of(job, "rm -rf .fleet"))
        self.assertLess(read, index_of(job, "Scan the image"))

    def test_the_scan_takes_only_the_file_the_script_wrote(self) -> None:
        scan = steps("image")[index_of(steps("image"), "Scan the image")]
        self.assertEqual(scan["with"]["trivyignores"], "${{ steps.accepted.outputs.file }}")
        self.assertEqual(scan["with"]["ignore-unfixed"], "true")
        self.assertEqual(scan["with"]["severity"], "CRITICAL,HIGH")

    def test_with_the_input_the_checkouts_own_trivy_settings_go_before_the_scan(self) -> None:
        job = steps("image")
        removal = index_of(job, "rm -f trivy.yaml .trivyignore")
        self.assertEqual(job[removal]["if"], SET)
        self.assertEqual(job[removal]["run"], "rm -f trivy.yaml .trivyignore")
        self.assertGreater(removal, index_of(job, "Build the image"))
        self.assertLess(removal, index_of(job, "Scan the image"))

    def test_the_file_scans_job_checks_the_checkouts_copy_without_using_it(self) -> None:
        job = steps("trivy")
        check = job[index_of(job, "Check the accepted image findings in this checkout")]
        self.assertEqual(check["if"], SET)
        self.assertEqual(check["env"], {"IGNORE_FILE": "${{ inputs.trivy-image-ignore-file }}"})
        text = SECURITY.read_text(encoding="utf-8")
        self.assertIn('env -u GITHUB_OUTPUT python3 -I .fleet/.github/scripts/trivy_ignore.py --file="$IGNORE_FILE" --out=/dev/null\n', text)
        self.assertIn('if ! [ -e "$IGNORE_FILE" ] && ! [ -L "$IGNORE_FILE" ]; then echo "No file', text)
        self.assertNotIn("id", check)
        self.assertLess(index_of(job, "Check the accepted image findings in this checkout"), index_of(job, "rm -rf .fleet"))
        self.assertNotIn("trivyignores", str(job))
        self.assertNotIn("steps.accepted", str(job))

    def test_the_self_test_runs_that_check_on_a_fixture_list_the_script_accepts(self) -> None:
        named = workflow(SELF_TEST)["jobs"]["security"]["with"][INPUT]
        self.assertEqual(named, "tests/fixtures/security/accepted.yaml")
        self.assertNotIn("image-paths", workflow(SELF_TEST)["jobs"]["security"]["with"])
        script = Script(self)
        result = script.run(f"--file={cli.ROOT / named}")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(script.written(), "vulnerabilities: []\n")

    def test_nothing_else_reads_the_input(self) -> None:
        text = SECURITY.read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertEqual(code.count(f"inputs.{INPUT}"), 5)
        self.assertEqual(code.count("trivyignores"), 1)
        self.assertEqual(code.count("steps.accepted.outputs.file"), 1)

    def test_the_script_never_takes_a_ref_from_the_event(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for name in ("GITHUB_SHA", "GITHUB_REF", "GITHUB_HEAD_REF", "GITHUB_BASE_REF", "GITHUB_EVENT"):
            self.assertNotIn(name, source)
        self.assertNotIn("--ref", source)

    def test_the_self_test_hands_the_scripts_list_to_trivy_the_way_the_image_job_does(self) -> None:
        job = workflow(SELF_TEST)["jobs"]["accepted-finding"]
        pins = workflow()["env"]
        for name in ("TRIVY_VERSION", "TRIVY_SHA256_X64", "TRIVY_SHA256_ARM64"):
            self.assertEqual(job["env"][name], pins[name], name)
        shipped = steps("image")[index_of(steps("image"), "Scan the image")]
        scans = [step for step in job["steps"] if "trivyignores" in step.get("with", {})]
        self.assertEqual([step["with"]["trivyignores"] for step in scans],
                         [f"${{{{ runner.temp }}}}/accepted/{name}.accepted.yaml" for name in ("none", "accepted", "elsewhere", "expired")])
        for step in scans:
            self.assertEqual(step["uses"], shipped["uses"])
            for same in ("skip-setup-trivy", "version", "severity", "ignore-unfixed"):
                self.assertEqual(step["with"][same], shipped["with"][same], same)
            self.assertTrue(step["with"]["trivyignores"].endswith(".yaml"))
        self.assertTrue(shipped["with"]["trivyignores"] == "${{ steps.accepted.outputs.file }}")
        self.assertTrue(steps("image")[index_of(steps("image"), "trivy_ignore.py")]["env"]["ACCEPTED"].endswith(".yaml"))
        text = SELF_TEST.read_text(encoding="utf-8")
        self.assertIn('python3 -I "$scripts/trivy_ignore.py" --file="$list.yaml" --out="$list.accepted.yaml"', text)
        for check in ("reported none ||", "if reported accepted;", "reported elsewhere ||", "reported expired ||"):
            self.assertIn(check, text)

    def test_the_docs_list_the_input_and_say_who_approves_an_entry(self) -> None:
        adopting = (cli.ROOT / "docs" / "adopting.md").read_text(encoding="utf-8")
        self.assertRegex(adopting, r"\n\| `trivy-image-ignore-file` \| none \| ")
        section = adopting.split("### Accepted image findings\n", 1)[1].split("\n### ", 1)[0]
        for words in ("default branch", "merging the pull request that adds it", "`expired_at`", "`statement`", "at most 180 days",
                      "No wildcards", "`trivy.yaml` and `.trivyignore`", "checks the checkout's own copy"):
            self.assertIn(words, section)


class Script:
    def __init__(self, test: unittest.TestCase) -> None:
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.answers = self.root / "answers.json"
        self.log = self.root / "log.jsonl"
        self.out = self.root / "accepted.yaml"
        self.output = self.root / "github_output"

    def run(self, *arguments: str, answers: Node | None = None) -> subprocess.CompletedProcess[str]:
        self.answers.write_text(json.dumps(answers or {}), encoding="utf-8")
        self.output.write_text("", encoding="utf-8")
        env = {"PATH": f"{FAKES}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_ANSWERS": str(self.answers),
               "FAKE_GH_LOG": str(self.log), "GH_TOKEN": "fake", "GITHUB_OUTPUT": str(self.output)}
        return subprocess.run([sys.executable, "-I", str(SCRIPT), f"--out={self.out}", f"--today={TODAY}", *arguments],
                              env=env, capture_output=True, text=True, check=False)

    def local(self, text: str) -> subprocess.CompletedProcess[str]:
        source = self.root / "source.yaml"
        source.write_text(text, encoding="utf-8")
        return self.run(f"--file={source}")

    def remote(self, content: Any, path: str = ".trivyignore.yaml", branch: str = "main") -> subprocess.CompletedProcess[str]:
        answers: Node = {f"GET repos/{REPO}": {"default_branch": branch},
                         f"GET repos/{REPO}/branches/{branch}": {"commit": {"sha": COMMIT}}}
        if isinstance(content, str):
            encoded = base64.encodebytes(content.encode()).decode()
            content = {"type": "file", "encoding": "base64", "content": encoded}
        if content is not None:
            answers[f"GET repos/{REPO}/contents/{path}?ref={COMMIT}"] = content
        return self.run(f"--repo={REPO}", f"--path={path}", answers=answers)

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return [json.loads(line)["call"] for line in self.log.read_text(encoding="utf-8").splitlines()]

    def written(self) -> str:
        return self.out.read_text(encoding="utf-8")


class FetchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.script = Script(self)

    def test_the_list_comes_from_the_default_branchs_commit(self) -> None:
        result = self.script.remote(listed())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.script.calls(), [f"GET repos/{REPO}", f"GET repos/{REPO}/branches/main",
                                               f"GET repos/{REPO}/contents/.trivyignore.yaml?ref={COMMIT}"])
        self.assertIn(f"1 accepted finding from .trivyignore.yaml at main ({COMMIT}):", result.stdout)
        self.assertIn("  - CVE-2026-0001 in usr/local/bin/example until 2027-01-31: Not linked in.", result.stdout)
        self.assertEqual(self.script.output.read_text(encoding="utf-8"), f"file={self.script.out}\n")

    def test_a_default_branch_of_another_name_is_followed(self) -> None:
        result = self.script.remote(listed(), branch="release/next")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"GET repos/{REPO}/branches/release/next", self.script.calls())

    def test_a_file_that_is_not_on_the_default_branch_accepts_nothing(self) -> None:
        result = self.script.remote(None)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("::notice::No .trivyignore.yaml at main", result.stdout)
        self.assertEqual(self.script.written(), "vulnerabilities: []\n")
        self.assertEqual(self.script.output.read_text(encoding="utf-8"), f"file={self.script.out}\n")

    def test_any_other_failure_fails_and_writes_no_list(self) -> None:
        for content in ({"__status__": 500}, {"__status__": 403}, [{"type": "file"}], {"type": "dir"},
                        {"type": "file", "encoding": "none", "content": ""}, {"type": "symlink", "target": "x"},
                        {"type": "file", "encoding": "base64", "content": base64.b64encode(b"\xff\xfe").decode()},
                        {"type": "file", "encoding": "base64", "content": base64.b64encode(b"#" * 70000).decode()}):
            script = Script(self)
            result = script.remote(content)
            self.assertEqual(result.returncode, 1, content)
            self.assertIn("::error::The accepted findings can't be used, so the job fails: ", result.stdout, content)
            self.assertFalse(script.out.exists(), content)
            self.assertEqual(script.output.read_text(encoding="utf-8"), "", content)

    def test_a_repository_or_branch_that_cannot_be_read_fails(self) -> None:
        cases: list[Node] = [
            {}, {f"GET repos/{REPO}": {"default_branch": "main"}}, {f"GET repos/{REPO}": {"default_branch": "a b"}},
            {f"GET repos/{REPO}": {"default_branch": "../x"}}, {f"GET repos/{REPO}": {}},
            {f"GET repos/{REPO}": {"default_branch": "main"}, f"GET repos/{REPO}/branches/main": {"commit": {"sha": "main"}}}]
        for answers in cases:
            script = Script(self)
            result = script.run(f"--repo={REPO}", "--path=.trivyignore.yaml", answers=answers)
            self.assertEqual(result.returncode, 1, answers)
            self.assertFalse(script.out.exists(), answers)
            self.assertNotIn("contents", " ".join(script.calls()), answers)

    def test_the_path_is_a_yaml_file_inside_the_repository(self) -> None:
        for path in ("../.trivyignore.yaml", "/etc/passwd.yaml", ".trivyignore", "a/../b.yaml", "a//b.yaml", "./a.yaml",
                     "a.yaml?ref=evil", "a b.yaml", "a.yaml\n", "-x.yaml#", ""):
            script = Script(self)
            result = script.run(f"--repo={REPO}", f"--path={path}")
            self.assertEqual(result.returncode, 1, path)
            self.assertEqual(script.calls(), [], path)
            self.assertFalse(script.out.exists(), path)
        self.assertEqual(self.script.remote(listed(), path="security/accepted.yml").returncode, 0)


class EntriesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.script = Script(self)

    def refused(self, text: str, reason: str) -> None:
        script = Script(self)
        result = script.local(text)
        self.assertEqual(result.returncode, 1, text)
        self.assertIn(reason, result.stdout, text)
        self.assertFalse(script.out.exists(), text)

    def test_trivy_gets_a_copy_of_what_was_understood(self) -> None:
        text = ("# accepted\n\nvulnerabilities:   # the image\n"
                "  - id: CVE-2026-0001   # x\n    paths:\n    - usr/local/bin/example\n    - 'usr/bin/it''s'\n"
                "    purls:\n      - \"pkg:golang/golang.org/x/net\"\n"
                "    statement: \"Not linked: see #12 \\\"here\\\"\"\n    expired_at: '2027-01-31'\n"
                "  - expired_at: 2026-11-17\n    statement: yes\n    id: GHSA-aaaa-bbbb-cccc\n    paths:\n      - app:scan (debian 13.1)\n")
        result = self.script.local(text)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.script.written(), (
            'vulnerabilities:\n'
            '  - id: "CVE-2026-0001"\n    paths:\n      - "usr/local/bin/example"\n      - "usr/bin/it\'s"\n'
            '    purls:\n      - "pkg:golang/golang.org/x/net"\n'
            '    statement: "Not linked: see #12 \\"here\\""\n    expired_at: 2027-01-31\n'
            '  - id: "GHSA-aaaa-bbbb-cccc"\n    paths:\n      - "app:scan (debian 13.1)"\n'
            '    statement: "yes"\n    expired_at: 2026-11-17\n'))
        self.assertIn("2 accepted findings from ", result.stdout)

    def test_an_empty_list_accepts_nothing(self) -> None:
        for text in ("", "# nothing\n", "vulnerabilities:\n", "vulnerabilities: []\n", "vulnerabilities: [ ]  # none\n"):
            script = Script(self)
            self.assertEqual(script.local(text).returncode, 0, text)
            self.assertEqual(script.written(), "vulnerabilities: []\n", text)

    def test_an_expired_entry_is_dropped_with_a_warning(self) -> None:
        result = self.script.local(listed(entry("CVE-2026-0002", expires=TODAY), entry("CVE-2026-0003", expires="2020-01-01"), entry()))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("::warning::The acceptance of CVE-2026-0002 expired on 2026-11-16", result.stdout)
        self.assertIn("::warning::The acceptance of CVE-2026-0003 expired on 2020-01-01", result.stdout)
        self.assertNotIn("CVE-2026-0002", self.script.written())
        self.assertNotIn("CVE-2026-0003", self.script.written())
        self.assertIn('"CVE-2026-0001"', self.script.written())
        only = Script(self)
        self.assertEqual(only.local(listed(entry(expires=TODAY))).returncode, 0)
        self.assertEqual(only.written(), "vulnerabilities: []\n")

    def test_an_expiry_is_required_and_at_most_180_days_away(self) -> None:
        self.assertEqual(self.script.local(listed(entry(expires="2027-05-15"))).returncode, 0)
        self.refused(listed(entry(expires="2027-05-16")), "`expired_at` is more than 180 days away")
        self.refused(listed(entry(expires="9999-12-31")), "more than 180 days away")
        self.refused(listed(entry(expires="2027-02-30")), "`expired_at` isn't a date")
        self.refused(listed(entry(expires="2027-1-5")), "needs `expired_at: YYYY-MM-DD`")
        self.refused(listed(entry(expires="2027-01-31T00:00:00Z")), "needs `expired_at: YYYY-MM-DD`")
        self.refused("vulnerabilities:\n  - id: CVE-2026-0001\n    paths:\n      - a\n    statement: why\n", "needs `expired_at: YYYY-MM-DD`")

    def test_a_statement_an_id_and_exact_paths_are_required(self) -> None:
        self.refused("vulnerabilities:\n  - id: CVE-2026-0001\n    paths:\n      - a\n    expired_at: 2027-01-31\n", "needs a `statement`")
        self.refused(listed(entry(statement='""')), "a value that is empty")
        self.refused(listed(entry(statement="'  '")), "a value that is empty")
        self.refused("vulnerabilities:\n  - paths:\n      - a\n    statement: why\n    expired_at: 2027-01-31\n", "needs an `id`")
        self.refused(listed(entry(identifier="'CVE 1'")), "needs an `id`")
        self.refused("vulnerabilities:\n  - id: CVE-2026-0001\n    statement: why\n    expired_at: 2027-01-31\n", "needs `paths`")
        self.refused("vulnerabilities:\n  - id: CVE-2026-0001\n    paths:\n    statement: why\n    expired_at: 2027-01-31\n", "needs `paths`")
        for path in ('"**"', "usr/**/syncthing", "'usr/local/bin/*'", "usr/bin/sync?hing", "'usr/[a-z]in/x'", "'{a,b}'", "'a\\b'"):
            self.refused(listed(entry(path=path)), "an exact path, without wildcards")
        self.refused(listed(entry(extra="    purls:\n")), "`purls` is empty")

    def test_a_purl_is_one_trivy_can_parse(self) -> None:
        for purl in ("pkg:golang/golang.org/x/net", "pkg:golang/golang.org/x/net@v0.60.0", "pkg:deb/debian/libssl3@3.0.11-1~deb12u2",
                     "pkg:pypi/requests@2.19.0", "pkg:npm/lodash", "pkg:maven/org.apache.logging.log4j/log4j-core@2.14.1",
                     "pkg:rpm/redhat/openssl@1.1.1k-7.el8_6", "pkg:gem/rack"):
            script = Script(self)
            result = script.local(listed(entry(extra=f'    purls:\n      - "{purl}"\n')))
            self.assertEqual(result.returncode, 0, (purl, result.stdout))
            self.assertIn(f'      - "{purl}"\n', script.written(), purl)
        for purl in ("pkg:", "pkg:golang", "pkg:golang/", "pkg:/net", "pkg://golang/net", "pkg:golang//net", "pkg:golang/net@",
                     "pkg:golang/@v1", "pkg:npm/@babel/core", "pkg:Golang/net", "pkg:1type/name", "pkg:go_lang/net", "golang.org/x/net",
                     "PKG:golang/net", "pkg:golang/net?arch=amd64", "pkg:golang/net#sub/path", "pkg:golang/x%2Fnet", "pkg:golang/x%zz",
                     "pkg:golang/a b", "pkg:golang/net@1@2", "pkg:golang/net;x", "pkg:deb/debian/x@1:2.0", "pkg:cpan/name",
                     "pkg:julia/Flux", "pkg:otp/ns/name", "pkg:swift/name", "pkg:vscode-extension/name", "pkg:golang/net/"):
            self.refused(listed(entry(extra=f'    purls:\n      - "{purl}"\n')), "a purl is pkg:<type>/<name>")
    def test_only_vulnerabilities_are_accepted(self) -> None:
        for section in ("misconfigurations", "secrets", "licenses"):
            self.refused(f"{section}:\n  - id: AVD-DS-0001\n", f"only `vulnerabilities:` is accepted; `{section}:` isn't")
            self.refused(listed() + f"{section}:\n  - id: AVD-DS-0001\n", f"only `vulnerabilities:` is accepted; `{section}:` isn't")
        self.refused(listed() + "vulnerabilities:\n" + entry("CVE-2026-0009"), "only one `vulnerabilities:` list")
        self.refused("- id: CVE-2026-0001\n", "the file starts with `vulnerabilities:`")
        self.refused("CVE-2026-0001\n", "the file starts with `vulnerabilities:`")
        self.refused("vulnerabilities: []\n" + entry(), "text after an empty")

    def test_yaml_outside_the_plain_block_form_is_refused(self) -> None:
        cases = {
            "vulnerabilities: [{id: CVE-2026-0001}]\n": "the file starts with",
            "vulnerabilities: &all\n" + entry(): "the file starts with",
            "---\n" + listed(): "the file starts with",
            listed(entry(extra="    severity: LOW\n")): "expected one of id, paths, purls, statement, expired_at",
            listed(entry(extra="    id: CVE-2026-0002\n")): "`id` is given twice",
            listed(entry(statement="|\n      many\n      lines")): "put this value in double quotes",
            listed(entry(statement="*anchor")): "put this value in double quotes",
            listed(entry(statement="why: because")): "put this value in double quotes",
            listed(entry(statement="[a, b]")): "put this value in double quotes",
            listed(entry(statement='"unfinished')): "doesn't end on its line",
            listed(entry(statement="'unfinished")): "doesn't end on its line",
            listed(entry(statement='"a" b')): "text after the closing quote",
            listed(entry(statement='"bad \\x escape"')): "an escape that isn't JSON's",
            listed(entry(statement='"bell \\u0007"')): "a character that can't be printed",
            listed(entry(path="[a]")): "put this value in double quotes",
            "vulnerabilities:\n  - id: CVE-2026-0001\n    paths: [a]\n    statement: why\n    expired_at: 2027-01-31\n": "`paths` is a list",
            "vulnerabilities:\n  - id: CVE-2026-0001\n      paths:\n      - a\n": "unexpected indentation",
            "vulnerabilities:\n  - id: CVE-2026-0001\n   statement: why\n": "unexpected indentation",
            "vulnerabilities:\n  -   id: CVE-2026-0001\n": "unexpected indentation",
            "vulnerabilities:\n  id: CVE-2026-0001\n": "unexpected indentation",
            "vulnerabilities:\n  - id: CVE-2026-0001\n    paths:\n      - a\n       - b\n": "unexpected indentation",
            "vulnerabilities:\n\t- id: CVE-2026-0001\n": "a tab in the indentation",
            "vulnerabilities:\n  - id:CVE-2026-0001\n": "expected one of",
            "vulnerabilities:\n  - id:\n": "a value is missing",
        }
        for text, reason in cases.items():
            self.refused(text, reason)

    def test_a_statement_cannot_start_a_workflow_command(self) -> None:
        result = self.script.local(listed(entry(statement='"100% sure ::add-mask::x"')))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([line for line in result.stdout.splitlines() if line.startswith("::")], [])
        self.assertIn("100%25 sure", result.stdout)
        refused = Script(self).local("vulnerabilities:\n  - id: \"x\\n::add-mask::y\"\n")
        self.assertEqual(len([line for line in refused.stdout.splitlines() if line.startswith("::")]), 1)

    def test_a_local_list_is_a_regular_file(self) -> None:
        target = self.script.root / "target.yaml"
        target.write_text(listed(), encoding="utf-8")
        link = self.script.root / "link.yaml"
        link.symlink_to(target)
        for path in (link, self.script.root, self.script.root / "missing.yaml"):
            script = Script(self)
            result = script.run(f"--file={path}")
            self.assertEqual(result.returncode, 1, path)
            self.assertIn("::error::The accepted findings can't be used, so the job fails: ", result.stdout, path)
            self.assertIn("isn't a regular file", result.stdout, path)
            self.assertFalse(script.out.exists(), path)
        self.assertEqual(self.script.run(f"--file={target}").returncode, 0)

    def test_a_local_list_has_the_size_limit_of_the_default_branchs(self) -> None:
        self.assertEqual(self.script.local(listed() + "#" * (65536 - len(listed()))).returncode, 0)
        self.refused(listed() + "#" * (65537 - len(listed())), "is larger than 65536 bytes")

    def test_the_files_jobs_step_fails_on_a_link_and_passes_when_there_is_no_list(self) -> None:
        text = SECURITY.read_text(encoding="utf-8")
        body = text.split("- name: Check the accepted image findings in this checkout\n", 1)[1].split("run: |\n", 1)[1]
        script = "\n".join(line[10:] for line in body.splitlines()[:2]).replace(".fleet/.github/scripts/trivy_ignore.py", str(SCRIPT))
        target = self.script.root / "target.yaml"
        target.write_text(listed(), encoding="utf-8")
        (self.script.root / "link.yaml").symlink_to(target)
        (self.script.root / "dangling.yaml").symlink_to(self.script.root / "nowhere")
        for name, code in (("target.yaml", 0), ("absent.yaml", 0), ("link.yaml", 1), ("dangling.yaml", 1)):
            result = subprocess.run(["bash", "-eo", "pipefail", "-c", script], cwd=self.script.root, capture_output=True, text=True,
                                    check=False, env={"PATH": os.environ["PATH"], "IGNORE_FILE": name})
            self.assertEqual(result.returncode, code, (name, result.stdout, result.stderr))
            if code:
                self.assertIn("isn't a regular file", result.stdout, name)
        self.assertIn("nothing to check", subprocess.run(
            ["bash", "-eo", "pipefail", "-c", script], cwd=self.script.root, capture_output=True, text=True, check=False,
            env={"PATH": os.environ["PATH"], "IGNORE_FILE": "absent.yaml"}).stdout)

    def test_today_is_utc_when_not_given(self) -> None:
        source = self.script.root / "source.yaml"
        source.write_text(listed(entry(expires="2020-01-01")), encoding="utf-8")
        result = subprocess.run([sys.executable, "-I", str(SCRIPT), f"--file={source}", f"--out={self.script.out}"],
                                env={"PATH": os.environ["PATH"]}, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.script.written(), "vulnerabilities: []\n")


if __name__ == "__main__":
    unittest.main()
