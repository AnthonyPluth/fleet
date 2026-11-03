import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from consumers import WAYPOINT, copy, temporary, write

from fleet_tools import agents_md, cli, drift, marketplace

VERSION = agents_md.own_version()
SETTINGS = ".claude/settings.json"
OTHER_FIRST = """{
  "extraKnownMarketplaces": {
    "other":   { "source": {"source": "github", "repo": "someone/else", "ref": "v1.0.0"} },
    "fleet": {
      "source": {
        "source" : "github",
        "repo": "AnthonyPluth/fleet",
        "ref"   :   "v1.0.0"
      }
    }
  },
  "env": {"ref": "v1.0.0"}
}
"""


def stale(root: Path, ref: str = "v1.0.0") -> str:
    path = root / SETTINGS
    text = path.read_text(encoding="utf-8").replace(f'"ref": "{VERSION}"', f'"ref": "{ref}"')
    path.write_text(text, encoding="utf-8")
    return text


def ref(root: Path, name: str = "fleet") -> object:
    return json.loads((root / SETTINGS).read_text(encoding="utf-8"))["extraKnownMarketplaces"][name]["source"]["ref"]


class Sync(unittest.TestCase):
    def test_the_fixture_declares_fleets_marketplace_at_its_version(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        self.assertEqual(ref(root), VERSION)
        self.assertEqual(marketplace.check(root, VERSION), [])

    def test_sync_moves_a_stale_ref_and_nothing_else(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        before = stale(root)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["sync", "--to", VERSION, "--root", str(root)]), 0)
        self.assertIn(f"{SETTINGS}: the fleet marketplace's ref 'v1.0.0' -> '{VERSION}'", out.getvalue())
        after = (root / SETTINGS).read_text(encoding="utf-8")
        self.assertEqual(after, before.replace('"ref": "v1.0.0"', f'"ref": "{VERSION}"'))
        self.assertEqual(marketplace.sync(root, VERSION), [])

    def test_only_fleets_ref_moves_whatever_the_formatting(self) -> None:
        root = temporary(self)
        write(root, SETTINGS, OTHER_FIRST)
        self.assertEqual(marketplace.sync(root, "v2.1.0"), [f"{SETTINGS}: the fleet marketplace's ref 'v1.0.0' -> 'v2.1.0'"])
        text = (root / SETTINGS).read_text(encoding="utf-8")
        self.assertEqual(text, OTHER_FIRST.replace('"ref"   :   "v1.0.0"', '"ref"   :   "v2.1.0"'))
        self.assertEqual((ref(root), ref(root, "other")), ("v2.1.0", "v1.0.0"))

    def test_settings_without_fleets_marketplace_are_left_alone(self) -> None:
        for text in ("", "not json", "[]", '{"hooks": {}}', '{"extraKnownMarketplaces": {"fleet": {"source": '
                     '{"source": "github", "repo": "AnthonyPluth/fleet"}}}}',
                     '{"extraKnownMarketplaces": {"x": {"source": {"source": "git", "url": "AnthonyPluth/fleet", "ref": "v1.0.0"}}}}'):
            with self.subTest(text=text):
                root = temporary(self)
                write(root, SETTINGS, text)
                self.assertEqual(marketplace.sync(root, VERSION), [])
                self.assertEqual(marketplace.check(root, VERSION), [])
                self.assertEqual((root / SETTINGS).read_text(encoding="utf-8"), text)
        self.assertEqual(marketplace.sync(temporary(self), VERSION), [])

    def test_a_ref_that_cant_be_moved_fails_the_sync_and_writes_nothing(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        stale(root)
        text = (root / SETTINGS).read_text(encoding="utf-8").replace('"ref": "v1.0.0"', '"ref": 2')
        write(root, SETTINGS, text)
        write(root, ".fleet/version", "v1.0.0\n" + "0" * 40 + "\n")
        before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["sync", "--to", VERSION, "--root", str(root)]), 2)
        self.assertEqual(err.getvalue(), f"fleet sync: {SETTINGS}: can't move fleet's marketplace `fleet` from ref 2 to "
                                         f'{VERSION}; set its ref to "{VERSION}" by hand\n')
        self.assertNotIn("is at fleet", out.getvalue())
        self.assertEqual({path: path.read_bytes() for path in root.rglob("*") if path.is_file()}, before)
        with self.assertRaises(marketplace.MarketplaceError):
            marketplace.sync(root, VERSION)


class Drift(unittest.TestCase):
    def test_drift_flags_a_stale_ref_until_sync_moves_it(self) -> None:
        root = copy(self, WAYPOINT, repository=False)
        stale(root, "main")
        problems, _ = drift.drift(root)
        self.assertEqual(problems, [f"{SETTINGS}: fleet's marketplace `fleet` is at ref 'main', but the fleet content is at "
                                    f"{VERSION}; run `fleet sync --to {VERSION}`, which moves it"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("GITHUB_ACTIONS", None)
            self.assertEqual(cli.main(["drift", "--root", str(root)]), 1)
        self.assertIn("marketplace `fleet` is at ref 'main'", err.getvalue())
        marketplace.sync(root, VERSION)
        self.assertEqual(drift.drift(root)[0], [])


if __name__ == "__main__":
    unittest.main()
