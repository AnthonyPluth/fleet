import importlib.metadata
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fleet_tools import identity

GIT_ENV = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
           "GIT_COMMITTER_EMAIL": "test@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
COMMIT = "0123456789abcdef0123456789abcdef01234567"
UV_DIRECT_URL = {"url": "https://github.com/AnthonyPluth/fleet",
                 "vcs_info": {"vcs": "git", "requested_revision": COMMIT, "commit_id": COMMIT}}


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


class Base(unittest.TestCase):
    def directory(self) -> Path:
        found = tempfile.TemporaryDirectory()
        self.addCleanup(found.cleanup)
        return Path(found.name)

    def repository(self, root: Path) -> str:
        (root / "fleet_tools").mkdir(parents=True, exist_ok=True)
        (root / "fleet_tools" / "__init__.py").write_text("", encoding="utf-8")
        git(root, "init", "-q", "-b", "main")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "chore: a fleet")
        return git(root, "rev-parse", "HEAD")


class Checkout(Base):
    def test_a_checkout_is_its_head(self) -> None:
        root = self.directory()
        head = self.repository(root)
        self.assertEqual(identity.commit(root), head)
        self.assertIsNone(identity.tag(root))
        self.assertEqual(identity.describe(root), f"fleet at commit {head}")

    def test_a_dirty_checkout_is_still_its_head(self) -> None:
        root = self.directory()
        head = self.repository(root)
        (root / "fleet_tools" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
        self.assertEqual(identity.commit(root), head)

    def test_a_worktree_whose_git_is_a_file_is_its_head(self) -> None:
        root = self.directory()
        self.repository(root / "main")
        git(root / "main", "worktree", "add", "-q", "-b", "other", str(root / "other"))
        (root / "other" / "more").write_text("x\n", encoding="utf-8")
        git(root / "other", "add", "-A")
        git(root / "other", "commit", "-q", "-m", "chore: on the worktree")
        self.assertTrue((root / "other" / ".git").is_file())
        self.assertEqual(identity.commit(root / "other"), git(root / "other", "rev-parse", "HEAD"))
        self.assertNotEqual(identity.commit(root / "other"), identity.commit(root / "main"))

    def test_the_tag_on_the_commit_is_named(self) -> None:
        root = self.directory()
        head = self.repository(root)
        git(root, "tag", "not-a-release")
        self.assertIsNone(identity.tag(root))
        git(root, "tag", "v4.1.0")
        self.assertEqual(identity.tag(root), "v4.1.0")
        self.assertEqual(identity.describe(root), f"fleet v4.1.0 (commit {head})")

    def test_a_directory_inside_another_repository_is_not_that_repositorys_commit(self) -> None:
        root = self.directory()
        self.repository(root)
        site = root / ".venv" / "lib" / "site-packages"
        (site / "fleet_tools").mkdir(parents=True)
        with mock.patch.object(identity, "installed_commit", return_value=None):
            self.assertIsNone(identity.commit(site))
        self.assertIsNone(identity.tag(site))

    def test_a_repository_without_a_commit_has_none(self) -> None:
        root = self.directory()
        git(root, "init", "-q", "-b", "main")
        self.assertIsNone(identity.commit(root))
        self.assertTrue(identity.describe(root).startswith("fleet at an unknown commit"))


class Installed(Base):
    def site(self, direct_url: object | str | None, name: str = "fleet") -> tuple[Path, importlib.metadata.Distribution]:
        site = self.directory()
        (site / "fleet_tools").mkdir()
        (site / "fleet_tools" / "__init__.py").write_text("", encoding="utf-8")
        info = site / f"{name}-0.0.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.0.0\n", encoding="utf-8")
        if direct_url is not None:
            text = direct_url if isinstance(direct_url, str) else json.dumps(direct_url)
            (info / "direct_url.json").write_text(text, encoding="utf-8")
        return site, importlib.metadata.PathDistribution(info)

    def installed(self, site: Path, distribution: importlib.metadata.Distribution) -> str | None:
        with mock.patch("importlib.metadata.distribution", return_value=distribution) as found:
            commit = identity.commit(site)
        found.assert_called_once_with("fleet")
        return commit

    def test_an_install_from_git_is_the_commit_in_its_direct_url(self) -> None:
        site, distribution = self.site(UV_DIRECT_URL)
        self.assertEqual(self.installed(site, distribution), COMMIT)
        with mock.patch("importlib.metadata.distribution", return_value=distribution):
            self.assertEqual(identity.describe(site), f"fleet at commit {COMMIT}")
            self.assertIsNone(identity.tag(site))

    def test_the_resolved_commit_counts_not_the_requested_revision(self) -> None:
        site, distribution = self.site({"url": "https://github.com/AnthonyPluth/fleet",
                                        "vcs_info": {"vcs": "git", "requested_revision": "v4.0.0", "commit_id": COMMIT}})
        self.assertEqual(self.installed(site, distribution), COMMIT)

    def test_an_install_that_names_no_git_commit_has_none(self) -> None:
        for direct_url in (None, "", "{not json", {"url": "file:///src/fleet", "dir_info": {"editable": True}},
                           {"url": "https://example.com/fleet.tar.gz", "archive_info": {}},
                           {"url": "https://example.com/fleet", "vcs_info": {"vcs": "hg", "commit_id": COMMIT}},
                           {"url": "https://example.com/fleet", "vcs_info": {"vcs": "git", "commit_id": COMMIT[:39]}},
                           {"url": "https://example.com/fleet", "vcs_info": {"vcs": "git", "commit_id": COMMIT.upper()}},
                           {"url": "https://example.com/fleet", "vcs_info": {"vcs": "git"}}, [COMMIT]):
            with self.subTest(direct_url=direct_url):
                site, distribution = self.site(direct_url)
                self.assertIsNone(self.installed(site, distribution))

    def test_an_install_elsewhere_isnt_this_code(self) -> None:
        _, distribution = self.site(UV_DIRECT_URL)
        other = self.directory()
        (other / "fleet_tools").mkdir()
        with mock.patch("importlib.metadata.distribution", return_value=distribution):
            self.assertIsNone(identity.commit(other))

    def test_no_install_has_none(self) -> None:
        site, _ = self.site(UV_DIRECT_URL)
        with mock.patch("importlib.metadata.distribution", side_effect=importlib.metadata.PackageNotFoundError("fleet")):
            self.assertIsNone(identity.commit(site))
            self.assertTrue(identity.describe(site).startswith("fleet at an unknown commit"))

    def test_a_checkout_wins_over_an_install(self) -> None:
        site, distribution = self.site(UV_DIRECT_URL)
        head = self.repository(site)
        with mock.patch("importlib.metadata.distribution", return_value=distribution) as found:
            self.assertEqual(identity.commit(site), head)
        found.assert_not_called()


if __name__ == "__main__":
    unittest.main()
