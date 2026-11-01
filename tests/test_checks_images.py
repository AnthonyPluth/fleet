import unittest

from consumers import RUNWAY, WAYPOINT, checks, commit, copy, empty_consumer, ran, write

from fleet_tools import config
from fleet_tools.checks import images

WAYPOINT_VERIFY = "export const PAGES = ['upcoming', \"trips\", 'oauth-approve'];\n"
RUNWAY_VERIFY = 'export const PAGES = ["budget", "setup"];\nexport const DEFAULT_VIEWPORTS = ["phone", "desktop"];\n'
FLOWS = [{"name": "emoji", "steps": [{"goto": "#setup"}, {"screenshot": "open"}, {"click": "a"}]},
         {"name": "lock", "viewports": ["phone", "landscape"], "steps": [{"screenshot": "locked"}]}]
PNG = b"\x89PNG\r\n\x1a\n"


class Pages(unittest.TestCase):
    SHOTS = "docs/src/assets/screenshots/"
    HOMES = ("docs/public/", "waypoint/static/", "waypoint/storage/logos/")
    FILES = ("docs/src/assets/logo.png",)

    def problems(self, *paths: str) -> list[str]:
        return images.check_images(list(paths), images.page_images(WAYPOINT_VERIFY), self.SHOTS, self.HOMES, self.FILES)

    def test_the_pages_come_from_verify_mjs(self) -> None:
        self.assertEqual(images.verify_list(WAYPOINT_VERIFY, "PAGES"), ["upcoming", "trips", "oauth-approve"])
        self.assertEqual(images.verify_list("nothing here", "PAGES"), [])

    def test_a_page_image_make_verify_writes_passes(self) -> None:
        self.assertEqual(self.problems(self.SHOTS + "trips-phone.png", self.SHOTS + "oauth-approve-desktop.png"), [])

    def test_a_page_s_first_screen_image_passes(self) -> None:
        self.assertEqual(self.problems(self.SHOTS + "trips-phone-hero.png", self.SHOTS + "upcoming-desktop-hero.png"), [])
        self.assertEqual(len(self.problems(self.SHOTS + "trips-hero.png")), 1)
        self.assertEqual(len(self.problems(self.SHOTS + "nope-phone-hero.png")), 1)

    def test_a_file_in_the_screenshots_folder_make_verify_would_not_write_fails(self) -> None:
        for name in ("before-after.png", "trips-tablet.png", "trips-phone-top.png", "nope-phone.png"):
            with self.subTest(name=name):
                self.assertEqual(len(self.problems(self.SHOTS + name)), 1)

    def test_the_homes_and_the_named_files_pass(self) -> None:
        self.assertEqual(self.problems("waypoint/storage/logos/delta.png", "waypoint/static/icon-192.png",
                                       "docs/public/screenshots/x.png", "docs/src/assets/logo.png"), [])

    def test_a_file_exception_is_exact_not_a_prefix(self) -> None:
        self.assertEqual(len(self.problems("docs/src/assets/logo.png-before.png", "docs/src/assets/logo.before.png")), 2)

    def test_a_hand_made_comparison_image_fails(self) -> None:
        problems = self.problems("docs/src/assets/tabbar-before-after.png", self.SHOTS + "trips-phone.png")
        self.assertEqual(len(problems), 1)
        self.assertIn("docs/src/assets/tabbar-before-after.png: the only images a change commits", problems[0])

    def test_every_raster_format_is_counted_and_vector_and_text_files_are_not(self) -> None:
        raster = ["a.png", "b.JPG", "c.jpeg", "d.gif", "e.webp", "f.avif", "g.ico", "h.heic", "i.bmp", "j.tif", "k.TIFF"]
        self.assertEqual([p for p in [*raster, "l.svg", "m.md"] if images.IMAGE.search(p)], raster)


class PagesAndFlows(unittest.TestCase):
    def setUp(self) -> None:
        self.made = images.page_and_flow_images(RUNWAY_VERIFY, FLOWS)

    def problems(self, *paths: str) -> list[str]:
        return images.check_images(list(paths), self.made, "screenshots/", ("extension/icons/", "runway/static/"),
                                   ("docs/src/assets/logo.png",), "pages-and-flows")

    def test_the_names_come_from_the_pages_and_the_flows(self) -> None:
        self.assertEqual(self.made, {
            "budget-phone.png", "budget-desktop.png", "setup-phone.png", "setup-desktop.png",
            "budget-top-phone.png", "budget-top-desktop.png", "setup-top-phone.png", "setup-top-desktop.png",
            "flow-emoji-phone.png", "flow-emoji-desktop.png", "flow-emoji-open-phone.png", "flow-emoji-open-desktop.png",
            "flow-lock-phone.png", "flow-lock-landscape.png", "flow-lock-locked-phone.png", "flow-lock-locked-landscape.png"})

    def test_top_names_the_first_screen(self) -> None:
        made = images.page_and_flow_images(RUNWAY_VERIFY + 'export const TOP = "hero";\n', [])
        self.assertIn("budget-hero-phone.png", made)
        self.assertNotIn("budget-top-phone.png", made)

    def test_a_screenshot_make_verify_writes_passes(self) -> None:
        self.assertEqual(self.problems("screenshots/budget-phone.png", "screenshots/flow-emoji-open-desktop.png",
                                       "screenshots/flow-lock-locked-landscape.png"), [])

    def test_a_file_in_screenshots_make_verify_would_not_write_fails(self) -> None:
        for name in ("before-after.png", "budget-tablet.png", "budget-phone-top.png", "nope-phone.png",
                     "flow-emoji-open-landscape.png"):
            with self.subTest(name=name):
                self.assertEqual(len(self.problems(f"screenshots/{name}")), 1)

    def test_the_screenshots_must_be_in_the_screenshots_folder(self) -> None:
        self.assertEqual(len(self.problems("docs/src/assets/screenshots/budget-phone.png")), 1)
        self.assertEqual(len(self.problems("budget-phone.png")), 1)

    def test_icons_assets_and_the_docs_site_logo_pass(self) -> None:
        self.assertEqual(self.problems("extension/icons/icon-16.png", "runway/static/icon-192.png",
                                       "docs/src/assets/logo.png"), [])


class FixtureConsumers(unittest.TestCase):
    def add(self, fixture: str, *paths: str) -> list[str]:
        root = copy(self, WAYPOINT if fixture == "waypoint" else RUNWAY)
        for path in paths:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(PNG)
        commit(root, "docs: images")
        return checks(root, "HEAD~1..HEAD")

    def test_waypoint_takes_page_images_and_its_homes(self) -> None:
        self.assertEqual(self.add("waypoint", "docs/src/assets/screenshots/trips-phone.png",
                                  "docs/src/assets/screenshots/upcoming-desktop-hero.png", "waypoint/storage/logos/x.png",
                                  "docs/src/assets/logo.png"), [])

    def test_a_hand_made_image_fires_in_waypoint(self) -> None:
        [problem] = self.add("waypoint", "docs/src/assets/before-after.png")
        self.assertIn("docs/src/assets/before-after.png: the only images a change commits", problem)

    def test_a_flow_image_is_a_runway_name_not_a_waypoint_one(self) -> None:
        self.assertEqual(len(self.add("waypoint", "docs/src/assets/screenshots/flow-lock-phone.png")), 1)
        self.assertEqual(self.add("runway", "screenshots/flow-lock-locked-landscape.png", "screenshots/budget-top-phone.png",
                                  "extension/icons/icon-16.png"), [])

    def test_an_image_make_verify_would_not_write_fires_in_runway(self) -> None:
        [problem] = self.add("runway", "screenshots/budget-tablet.png")
        self.assertIn("screenshots/budget-tablet.png: the only images a change commits", problem)

    def test_the_page_list_is_read_from_the_branch_s_head(self) -> None:
        root = copy(self, RUNWAY)
        write(root, "frontend/verify/verify.mjs", RUNWAY_VERIFY.replace('"setup"', '"reports"'))
        (root / "screenshots/reports-phone.png").write_bytes(PNG)
        commit(root, "feat: reports")
        self.assertEqual(checks(root, "HEAD~1..HEAD"), [])

    def test_a_deleted_image_is_always_fine(self) -> None:
        root = copy(self, RUNWAY)
        (root / "screenshots/stray.png").write_bytes(PNG)
        commit(root, "chore: an old stray")
        (root / "screenshots/stray.png").unlink()
        commit(root, "chore: drop it")
        self.assertEqual(checks(root, "HEAD~1..HEAD"), [])

    def test_runs_only_with_commits(self) -> None:
        root = copy(self, WAYPOINT)
        self.assertNotIn("images", ran(root))
        self.assertIn("images", ran(root, "HEAD..HEAD"))

    def test_silent_where_not_configured(self) -> None:
        root = empty_consumer(self)
        (root / "before-after.png").write_bytes(PNG)
        commit(root, "docs: a picture")
        self.assertNotIn("images", ran(root, "HEAD~1..HEAD"))
        self.assertEqual(checks(root, "HEAD~1..HEAD"), [])

    def test_an_unknown_names_value_is_a_config_error(self) -> None:
        root = copy(self, RUNWAY)
        pyproject = root / "pyproject.toml"
        pyproject.write_text(pyproject.read_text().replace('names = "pages-and-flows"', 'names = "flows"'))
        commit(root, "chore: config")
        with self.assertRaisesRegex(config.ConfigError, "names must be one of pages, pages-and-flows"):
            checks(root, "HEAD~1..HEAD")


if __name__ == "__main__":
    unittest.main()
