import argparse
import contextlib
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from typing import Any

from PIL import Image

from fleet_tools import cli
from fleet_tools import screenshot_diff as sd

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "screenshot-diff.yml"
GRAY = (40, 44, 52, 255)
Pixel = tuple[int, int, int, int]


def image(size: tuple[int, int] = (100, 80), color: Pixel = GRAY, **pixels: Pixel) -> Image.Image:
    img = Image.new("RGBA", size, color)
    for name, value in pixels.items():
        _, x, y = name.split("_")
        img.putpixel((int(x), int(y)), value)
    return img


def changed(old: Image.Image, new: Image.Image) -> tuple[int, list[sd.Box]]:
    mask = sd.changed_mask(old, new)
    return mask.histogram()[255], sd.regions(mask)


def run(*argv: str) -> tuple[int, str]:
    shown = io.StringIO()
    with contextlib.redirect_stdout(shown):
        code = sd.command(list(argv))
    return code, shown.getvalue()


class Comparing(unittest.TestCase):
    def test_identical_images_have_no_diff(self) -> None:
        self.assertEqual(changed(image(), image()), (0, []))

    def test_one_changed_pixel_is_counted_and_boxed_tightly(self) -> None:
        count, boxes = changed(image(), image(p_37_21=(255, 255, 255, 255)))
        self.assertEqual(count, 1)
        self.assertEqual(boxes, [(37, 21, 38, 22)])

    def test_a_pixel_in_the_last_partial_cell_is_found(self) -> None:
        self.assertEqual(changed(image(), image(p_99_79=(0, 0, 0, 255))), (1, [(99, 79, 100, 80)]))

    def test_the_smallest_difference_in_any_channel_counts(self) -> None:
        for channel in range(4):
            new = list(GRAY)
            new[channel] += 1 if new[channel] < 255 else -1
            with self.subTest(channel="RGBA"[channel]):
                self.assertEqual(changed(image(), image(p_5_5=(new[0], new[1], new[2], new[3])))[0], 1)

    def test_two_far_apart_changes_are_two_boxes(self) -> None:
        new = image((400, 300), p_10_10=(255, 0, 0, 255), p_12_11=(255, 0, 0, 255), p_380_280=(0, 255, 0, 255))
        count, boxes = changed(image((400, 300)), new)
        self.assertEqual(count, 3)
        self.assertEqual(boxes, [(10, 10, 13, 12), (380, 280, 381, 281)])

    def test_boxes_come_in_reading_order_by_their_top_edge(self) -> None:
        count, boxes = changed(image((600, 200)), image((600, 200), p_5_95=(255, 0, 0, 255), p_400_80=(255, 0, 0, 255)))
        self.assertEqual((count, boxes), (2, [(400, 80, 401, 81), (5, 95, 6, 96)]))

    def test_changes_close_together_are_one_box(self) -> None:
        self.assertEqual(changed(image(), image(p_10_10=(1, 2, 3, 255), p_30_10=(1, 2, 3, 255))), (2, [(10, 10, 31, 11)]))

    def test_a_scattered_change_is_a_few_boxes_that_hold_every_changed_pixel(self) -> None:
        spots = [(x, y) for x in range(5, 600, 60) for y in range(5, 400, 60)]
        new = image((600, 400))
        for x, y in spots:
            new.putpixel((x, y), (255, 0, 0, 255))
        count, boxes = changed(image((600, 400)), new)
        self.assertEqual(count, len(spots))
        self.assertLessEqual(len(boxes), sd.MAX_BOXES)
        for x, y in spots:
            self.assertTrue(any(b[0] <= x < b[2] and b[1] <= y < b[3] for b in boxes), (x, y))

    def test_a_very_scattered_change_stays_quick_and_bounded(self) -> None:
        new = image((1280, 3000))
        for x in range(3, 1280, 20):
            for y in range(3, 3000, 20):
                new.putpixel((x, y), (255, 0, 0, 255))
        count, boxes = changed(image((1280, 3000)), new)
        self.assertEqual(count, 64 * 150)
        self.assertEqual(len(boxes), 1)

    def test_a_different_size_compares_the_area_both_have(self) -> None:
        old, new = image((100, 80)), image((100, 120), p_50_70=(255, 0, 0, 255), p_50_100=(255, 0, 0, 255))
        mask = sd.changed_mask(old, new)
        self.assertEqual(mask.size, (100, 80))
        self.assertEqual(mask.histogram()[255], 1)


class OverlayBoxes(unittest.TestCase):
    def file(self, old: tuple[int, int] | None, new: tuple[int, int] | None, boxes: list[sd.Box] | None = None) -> sd.Entry:
        return {"error": None, "old_size": old, "new_size": new, "boxes": boxes or []}

    def test_a_same_size_image_gets_the_boxes_of_its_changes_only(self) -> None:
        f = self.file((100, 80), (100, 80), [(20, 30, 21, 31)])
        self.assertEqual(sd.overlay_boxes(f), [("change", (20, 30, 21, 31), "")])

    def test_rows_only_the_actual_image_has_are_a_labelled_added_box(self) -> None:
        f = self.file((100, 80), (100, 100), [(1, 1, 2, 2)])
        self.assertEqual(sd.overlay_boxes(f), [("change", (1, 1, 2, 2), ""),
                                               ("added", (0, 80, 100, 100), "20 rows only in the actual image")])

    def test_rows_only_the_reference_has_are_a_labelled_removed_box_on_the_actual_canvas(self) -> None:
        f = self.file((100, 80), (100, 50))
        self.assertEqual(sd.overlay_boxes(f), [("removed", (0, 50, 100, 80), "30 rows only in the reference")])

    def test_columns_only_one_image_has_are_boxed_as_tall_as_the_shorter_one(self) -> None:
        wider = sd.overlay_boxes(self.file((100, 80), (130, 60)))
        self.assertEqual(wider, [("removed", (0, 60, 100, 80), "20 rows only in the reference"),
                                 ("added", (100, 0, 130, 60), "30 columns only in the actual image")])
        narrower = sd.overlay_boxes(self.file((100, 80), (90, 80)))
        self.assertEqual(narrower, [("removed", (90, 0, 100, 80), "10 columns only in the reference")])

    def test_a_file_that_could_not_be_compared_or_has_one_image_has_no_extra_boxes(self) -> None:
        self.assertEqual(sd.overlay_boxes(self.file((100, 80), None)), [])
        self.assertEqual(sd.overlay_boxes(self.file(None, (100, 80))), [])
        self.assertEqual(sd.overlay_boxes({"error": "x", "old_size": None, "new_size": None, "boxes": []}), [])


class GitRepository(unittest.TestCase):
    directory = "screenshots"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / self.directory).mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.save("same-phone.png", image())
        self.save("edited-phone.png", image())
        self.save("gone-phone.png", image())
        self.save("taller-phone.png", image())
        self.save("main-only-phone.png", image())
        (self.repo / self.directory / "notes.txt").write_text("one", encoding="utf-8")
        self.commit("start")

    def git(self, *args: str) -> str:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, env=env, text=True).stdout.strip()

    def path(self, name: str) -> str:
        return f"{self.directory}/{name}"

    def save(self, name: str, img: Image.Image, **kwargs: Any) -> None:
        img.save(self.repo / self.directory / name, **kwargs)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def diff(self, base: str, head: str) -> sd.Entry:
        return sd.diff(base, head, self.repo, self.directory + "/")

    def branch(self) -> tuple[str, str]:
        self.git("checkout", "-q", "-b", "pr")
        self.save("edited-phone.png", image(p_20_30=(255, 0, 0, 255)))
        self.save("taller-phone.png", image((100, 100), p_1_1=(255, 0, 0, 255)))
        self.save("new-phone.png", image((50, 60)))
        (self.repo / self.directory / "gone-phone.png").unlink()
        (self.repo / self.directory / "notes.txt").write_text("two", encoding="utf-8")
        (self.repo / "other.png").write_bytes(b"not in the screenshots directory")
        head = self.commit("the change")
        self.git("checkout", "-q", "main")
        self.save("main-only-phone.png", image(p_0_0=(255, 0, 0, 255)))
        base = self.commit("main moves on")
        return base, head

    def test_identical_trees_have_no_changes(self) -> None:
        sha = self.git("rev-parse", "HEAD")
        report = self.diff(sha, sha)
        self.assertFalse(report["changed"])
        self.assertEqual(report["files"], [])

    def test_added_deleted_modified_and_resized_files(self) -> None:
        base, head = self.branch()
        report = self.diff(base, head)
        by_path = {f["path"]: f for f in report["files"]}
        self.assertEqual(sorted(by_path), [self.path(n) for n in ("edited-phone.png", "gone-phone.png", "new-phone.png", "taller-phone.png")])
        edited = by_path[self.path("edited-phone.png")]
        self.assertEqual((edited["status"], edited["changed_pixels"], edited["compared_pixels"]), ("modified", 1, 8000))
        self.assertEqual(edited["boxes"], [(20, 30, 21, 31)])
        taller = by_path[self.path("taller-phone.png")]
        self.assertEqual((taller["old_size"], taller["new_size"], taller["changed_pixels"]), ((100, 80), (100, 100), 1))
        gone = by_path[self.path("gone-phone.png")]
        self.assertEqual((gone["status"], gone["old_size"]), ("deleted", (100, 80)))
        new = by_path[self.path("new-phone.png")]
        self.assertEqual((new["status"], new["new_size"]), ("added", (50, 60)))

    def test_the_report_records_the_directory_and_the_marker(self) -> None:
        base, head = self.branch()
        report = sd.diff(base, head, self.repo, "screenshots/", "<!-- shots -->")
        self.assertEqual((report["dir"], report["marker"]), ("screenshots/", "<!-- shots -->"))
        self.assertTrue(sd.render(report).startswith("<!-- shots -->\n"))

    def test_a_png_with_the_same_pixels_but_other_bytes_has_no_changed_pixels(self) -> None:
        self.git("checkout", "-q", "-b", "pr")
        self.save("edited-phone.png", image(), compress_level=0)
        head = self.commit("re-encoded")
        (entry,) = self.diff("main", head)["files"]
        self.assertEqual((entry["status"], entry["changed_pixels"], entry["boxes"]), ("modified", 0, []))

    def test_a_file_that_is_not_an_image_is_reported_not_fatal(self) -> None:
        self.git("checkout", "-q", "-b", "pr")
        (self.repo / self.directory / "edited-phone.png").write_bytes(b"not a png")
        head = self.commit("broken")
        (entry,) = self.diff("main", head)["files"]
        self.assertIn("UnidentifiedImageError", entry["error"])
        self.assertEqual(sd.counts([entry]), "1 could not be compared")

    def test_a_ref_that_does_not_exist_is_an_error(self) -> None:
        with self.assertRaises(SystemExit):
            self.diff("no-such-ref", "HEAD")

    def test_the_command_prints_the_comment_with_dry_run_and_writes_nothing(self) -> None:
        _, head = self.branch()
        out = self.repo / "out"
        code, shown = run("--base", "main", "--head", head, "--out", str(out), "--dry-run", "--repo", str(self.repo),
                          "--dir", self.directory)
        self.assertEqual(code, 0)
        self.assertTrue(shown.startswith(sd.MARKER))
        self.assertIn("2 modified, 1 added, 1 deleted", shown)
        self.assertFalse(out.exists())

    def test_the_command_writes_the_comment_the_facts_and_the_report(self) -> None:
        _, head = self.branch()
        out = self.repo / "out"
        code, _ = run("--base", "main", "--head", head, "--out", str(out), "--run-url", "https://x.invalid/r/1", "--repo", str(self.repo),
                      "--dir", self.directory)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["report.html", "summary.json", "summary.md"])
        self.assertTrue((out / "summary.md").read_text(encoding="utf-8").startswith(sd.MARKER))
        self.assertIn("https://x.invalid/r/1#artifacts", (out / "summary.md").read_text(encoding="utf-8"))
        self.assertTrue(json.loads((out / "summary.json").read_text(encoding="utf-8"))["changed"])

    def test_the_command_needs_a_base(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _ = run("--repo", str(self.repo))
        self.assertEqual(code, 2)
        self.assertIn("--base is required", err.getvalue())


class AnotherDirectory(GitRepository):
    directory = "docs/src/assets/screenshots"

    def test_the_directory_argument_picks_the_screenshots(self) -> None:
        _, head = self.branch()
        out = self.repo / "out"
        code, shown = run("--base", "main", "--head", head, "--dir", self.directory, "--marker", "<!-- shots -->",
                          "--repo", str(self.repo), "--out", str(out))
        self.assertEqual(code, 0)
        self.assertIn("4 screenshots changed", shown)
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["dir"], self.directory + "/")
        self.assertEqual(sorted(f["path"] for f in summary["files"]),
                         [self.path(n) for n in ("edited-phone.png", "gone-phone.png", "new-phone.png", "taller-phone.png")])
        self.assertTrue((out / "summary.md").read_text(encoding="utf-8").startswith("<!-- shots -->\n"))
        code, shown = run("--from-json", str(out / "summary.json"), "--report-url", "https://x.invalid/a/1")
        self.assertTrue(shown.startswith("<!-- shots -->\n"))

    def test_the_default_directory_sees_nothing_here(self) -> None:
        _, head = self.branch()
        self.assertEqual(sd.diff("main", head, self.repo)["files"], [])


def entry(name: str, changed_pixels: int = 10, status: str = "modified", **extra: Any) -> sd.Entry:
    base: sd.Entry = {"path": f"screenshots/{name}", "status": status, "old_size": [440, 956], "new_size": [440, 956],
                      "changed_pixels": changed_pixels, "compared_pixels": 440 * 956, "percent": 100 * changed_pixels / (440 * 956),
                      "boxes": [(1, 2, 11, 12)] if changed_pixels else [], "error": None}
    return base | extra


class Summary(unittest.TestCase):
    URL = "https://x.invalid/artifacts/7"

    def test_no_changes_says_so_and_keeps_the_marker(self) -> None:
        text = sd.render({"files": []})
        self.assertTrue(text.startswith(sd.MARKER))
        self.assertIn("No screenshot changes", text)
        self.assertNotIn("View the visual diff", sd.render({"files": []}, "https://x.invalid/runs/9", self.URL))

    def test_the_comment_is_the_link_one_line_of_counts_and_the_note(self) -> None:
        files = [entry(f"page-{i}-phone.png", 5 + i) for i in range(23)]
        files += [entry(f"new-{i}-phone.png", 0, "added", old_size=None, boxes=[]) for i in range(2)]
        files += [entry("old-phone.png", 0, "deleted", new_size=None, boxes=[])]
        text = sd.render({"files": files}, "https://x.invalid/runs/9", self.URL)
        self.assertEqual(text.splitlines(), [
            sd.MARKER, "### Screenshot changes", "",
            f"**View the visual diff: [report.html]({self.URL})**", "",
            "23 modified, 2 added, 1 deleted.", "",
            "_Reference, Actual and Slider for each image, with a box around what changed. The link needs you to be logged "
            "in to GitHub, and the file is kept 14 days._", "",
            f"_{sd.REMINDER}_"])

    def test_no_screenshot_is_listed_in_the_comment(self) -> None:
        files = [entry("a-phone.png", 5), entry("b-phone.png", 4400, new_size=[440, 1000]),
                 entry("new-phone.png", 0, "added", old_size=None, boxes=[]), entry("old-phone.png", 0, "deleted", new_size=None, boxes=[])]
        text = sd.render({"files": files}, "https://x.invalid/runs/9", self.URL)
        for needle in ("a-phone", "b-phone", "new-phone", "old-phone", "|", "4,400", "resized"):
            self.assertNotIn(needle, text)
        self.assertIn("2 modified, 1 added, 1 deleted.", text)

    def test_the_comment_stays_short_for_thousands_of_files(self) -> None:
        files = [entry(f"flow-{i:05d}-with-a-long-name-desktop.png", 50 + i) for i in range(5000)]
        self.assertLess(len(sd.render({"files": files}, "https://x.invalid/runs/9", self.URL)), 1000)

    def test_counts_leave_out_what_is_zero_and_name_what_could_not_be_compared(self) -> None:
        self.assertEqual(sd.counts([entry("a.png"), entry("b.png")]), "2 modified")
        self.assertEqual(sd.counts([entry("n.png", 0, "added", old_size=None)]), "1 added")
        self.assertEqual(sd.counts([entry("o.png", 0, "deleted", new_size=None)]), "1 deleted")
        self.assertEqual(sd.counts([entry("a.png"), entry("x.png", 0, error="OSError: x")]), "1 modified, 1 could not be compared")

    def test_without_the_report_link_the_comment_points_at_the_run_artifacts(self) -> None:
        text = sd.render({"files": [entry("a-phone.png", 5)]}, "https://x.invalid/runs/9")
        self.assertNotIn("View the visual diff", text)
        self.assertIn("1 modified.", text)
        self.assertIn("`report.html`, is in the artifacts of [this run](https://x.invalid/runs/9#artifacts)", text)
        self.assertIn("logged in to GitHub", text)
        self.assertIn("kept 14 days", text)
        self.assertIn(sd.REMINDER, text)
        self.assertIn("artifacts of this workflow run", sd.render({"files": [entry("a-phone.png", 5)]}))

    def test_a_marker_in_the_report_is_the_comments_first_line(self) -> None:
        text = sd.render({"files": [entry("a-phone.png", 5)], "marker": "<!-- shots -->"})
        self.assertTrue(text.startswith("<!-- shots -->\n### Screenshot changes"))


def noise(size: tuple[int, int], seed: int = 1) -> Image.Image:
    rnd = random.Random(seed)
    return Image.frombytes("RGBA", size, rnd.randbytes(size[0] * size[1] * 4))


def cards(page: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for card in page.split("<article ")[1:]:
        match = re.search(r"<h2><a [^>]*>([^<]+)</a>", card)
        assert match is not None
        found[match.group(1)] = card
    return found


def tabs(card: str) -> list[str]:
    return re.findall(r'role="tab"[^>]*>([^<]+)<', card)


def selected(card: str) -> str:
    match = re.search(r'role="tab" id="t-\d+-(\w+)"[^>]*aria-selected="true"', card)
    assert match is not None
    return match.group(1)


def pane(card: str, kind: str) -> str:
    match = re.search(rf'<div role="tabpanel" class="pane pane-{kind}".*?(?=<div role="tabpanel"|</article>|$)', card, re.S)
    assert match is not None
    return match.group(0)


def boxes(markup: str) -> list[tuple[str, str]]:
    return re.findall(r'<div class="box (\w+)"[^>]*>(?:<b>([^<]*)</b>)?</div>', markup)


class Report(GitRepository):
    def report(self, base: str, head: str, **kwargs: Any) -> tuple[sd.Entry, str, list[str]]:
        data = self.diff(base, head)
        page, notes = sd.build_report(data, self.repo, **kwargs)
        return data, page, notes

    def on_main(self, branch: str, **files: Image.Image) -> str:
        self.git("checkout", "-q", "-b", branch)
        for name, img in files.items():
            self.save(name, img)
        self.commit(branch)
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", branch)
        return self.git("rev-parse", "HEAD")

    def test_every_changed_file_has_a_card_and_the_tabs_its_status_allows(self) -> None:
        base, head = self.branch()
        _, page, notes = self.report(base, head)
        self.assertEqual(notes, [])
        by = cards(page)
        self.assertEqual(sorted(by), [self.path(n) for n in ("edited-phone.png", "gone-phone.png", "new-phone.png", "taller-phone.png")])
        self.assertIn("4 images changed (1 added, 1 deleted, 1 modified, 1 resized)", page)
        self.assertEqual(tabs(by[self.path("edited-phone.png")]), ["Reference", "Actual", "Slider"])
        self.assertEqual(tabs(by[self.path("taller-phone.png")]), ["Reference", "Actual", "Slider"])
        self.assertEqual(tabs(by[self.path("new-phone.png")]), ["Actual"])
        self.assertEqual(tabs(by[self.path("gone-phone.png")]), ["Reference"])
        self.assertIn("Screenshot does not match the stored reference. 1 pixels (ratio 0.0001) differ.", by[self.path("edited-phone.png")])
        self.assertIn("New screenshot: there is no stored reference.", by[self.path("new-phone.png")])
        self.assertIn("Screenshot removed", by[self.path("gone-phone.png")])
        self.assertRegex(page, r'<img src="data:image/webp;base64,[^"]+" alt="Actual screenshot screenshots/new-phone.png')

    def test_each_card_opens_on_the_tab_that_suits_its_status(self) -> None:
        base, head = self.branch()
        by = cards(self.report(base, head)[1])
        self.assertEqual(selected(by[self.path("edited-phone.png")]), "slider")
        self.assertEqual(selected(by[self.path("taller-phone.png")]), "slider")
        self.assertEqual(selected(by[self.path("new-phone.png")]), "act")
        self.assertEqual(selected(by[self.path("gone-phone.png")]), "ref")
        card = by[self.path("edited-phone.png")]
        self.assertEqual(re.findall(r'role="tab"[^>]*tabindex="(-?\d)"', card), ["-1", "-1", "0"])

    def test_tabs_are_an_accessible_tablist_the_arrow_keys_move_along(self) -> None:
        base, head = self.branch()
        _, page, _ = self.report(base, head)
        card = cards(page)[self.path("edited-phone.png")]
        self.assertRegex(card, r'<div class="tabs" role="tablist" aria-label="Views of screenshots/edited-phone.png">')
        for kind in ("ref", "act", "slider"):
            self.assertRegex(card, rf'role="tab" id="t-\d+-{kind}" aria-controls="p-\d+-{kind}"')
            self.assertRegex(card, rf'role="tabpanel" class="pane pane-{kind}" id="p-\d+-{kind}" aria-labelledby="t-\d+-{kind}"')
        for needle in ('"ArrowRight"', '"ArrowLeft"', '"Home"', '"End"', "aria-selected", ".focus()", "e.preventDefault()"):
            self.assertIn(needle, sd.REPORT_JS)
        self.assertIn('role","slider"', sd.REPORT_JS)
        self.assertIn("aria-valuenow", sd.REPORT_JS)

    def test_the_actual_pane_has_the_change_boxes_and_the_reference_pane_has_none(self) -> None:
        base, head = self.branch()
        card = cards(self.report(base, head)[1])[self.path("edited-phone.png")]
        (box,) = re.findall(r'<div class="box change"[^>]*>', pane(card, "act"))
        self.assertIn('title="Changed region at (20, 30), 1x1"', box)
        self.assertIn('aria-hidden="true"', box)
        self.assertEqual(boxes(pane(card, "act")), [("change", "")])
        self.assertEqual(boxes(pane(card, "ref")), [])
        self.assertIn("slider(refWins[i],actWins[i])", sd.REPORT_JS)
        self.assertIn("base=actWin.cloneNode(true)", sd.REPORT_JS)
        self.assertEqual(boxes(pane(card, "slider")), [])

    def test_a_box_is_placed_by_percentages_of_its_band(self) -> None:
        self.git("checkout", "-q", "-b", "pr")
        self.save("edited-phone.png", image(p_20_30=(255, 0, 0, 255)))
        head = self.commit("one pixel")
        card = cards(self.report("main", head)[1])[self.path("edited-phone.png")]
        self.assertIn('style="left:20.0000%;top:37.5000%;width:1.0000%;height:1.2500%"', pane(card, "act"))

    def test_the_toggle_is_a_checkbox_on_by_default_that_hides_the_boxes_and_is_remembered_nowhere(self) -> None:
        base, head = self.branch()
        by = cards(self.report(base, head)[1])
        for name in ("edited", "taller"):
            self.assertIn('<label class="boxtoggle"><input type="checkbox" checked> Show change boxes</label>',
                          by[self.path(f"{name}-phone.png")])
        for name in ("new", "gone"):
            self.assertNotIn("Show change boxes", by[self.path(f"{name}-phone.png")])
            self.assertEqual(boxes(by[self.path(f"{name}-phone.png")]), [])
        self.assertIn(".hide-boxes .box{display:none}", sd.REPORT_CSS)
        self.assertIn('root.classList.toggle("hide-boxes",!c.checked)', sd.REPORT_JS)
        self.assertNotIn("localStorage", sd.REPORT_JS)
        self.assertNotIn("sessionStorage", sd.REPORT_JS)

    def test_the_all_rows_view_gets_the_boxes_from_a_template(self) -> None:
        base = self.on_main("tall", **{"tall-desktop.png": image((120, 3000))})
        self.git("checkout", "-q", "-b", "change")
        self.save("tall-desktop.png", image((120, 3000), p_5_400=(255, 255, 255, 255)))
        head = self.commit("one change")
        card = cards(self.report(base, head)[1])[self.path("tall-desktop.png")]
        (template,) = re.findall(r"<template>(.*?)</template>", card)
        self.assertEqual(boxes(template), [("change", "")])
        self.assertIn("top:13.3333%", template)
        self.assertIn("t.content.cloneNode(true)", sd.REPORT_JS)

    def test_the_page_is_self_contained(self) -> None:
        base, head = self.branch()
        _, page, _ = self.report(base, head)
        self.assertNotRegex(page, r"https?://|//[a-z]")
        self.assertNotRegex(page, r'(?:src|href|action|poster)="(?!data:image/|#)')
        for needle in ("<link", "@import", "url(http", "<iframe", "fetch(", "XMLHttpRequest", "import("):
            self.assertNotIn(needle, page)
        self.assertEqual(len(re.findall(r"<script", page)), 1)
        self.assertEqual(len(re.findall(r"<style", page)), 1)

    def test_a_resized_image_that_got_shorter_boxes_the_rows_only_the_reference_has(self) -> None:
        self.git("checkout", "-q", "-b", "pr")
        self.save("taller-phone.png", image((100, 50), p_1_1=(255, 0, 0, 255)))
        head = self.commit("shorter")
        data, page, _ = self.report("main", head)
        card = cards(page)[self.path("taller-phone.png")]
        self.assertEqual(data["files"][0]["old_size"], (100, 80))
        self.assertIn("Screenshot size changed from 100x80 to 100x50. 1 pixels", card)
        self.assertIn("Dimensions differ: reference 100x80, actual 100x50.", card)
        self.assertIn("The reference has 30 more rows than the actual image.", card)
        self.assertEqual(tabs(card), ["Reference", "Actual", "Slider"])
        self.assertEqual(boxes(pane(card, "act")), [("change", ""), ("removed", "30 rows only in the reference")])
        self.assertEqual(boxes(pane(card, "ref")), [])
        self.assertIn("top:62.5000%;width:100.0000%;height:37.5000%", pane(card, "act"))

    def test_a_resized_image_that_got_taller_boxes_the_new_rows(self) -> None:
        base, head = self.branch()
        card = cards(self.report(base, head)[1])[self.path("taller-phone.png")]
        self.assertIn("The actual image has 20 more rows than the reference.", card)
        self.assertEqual(boxes(pane(card, "act")), [("change", ""), ("added", "20 rows only in the actual image")])
        self.assertIn("top:80.0000%;width:100.0000%;height:20.0000%", pane(card, "act"))

    def test_a_band_shows_only_the_boxes_that_reach_into_it(self) -> None:
        base = self.on_main("tall", **{"tall-desktop.png": image((120, 3000))})
        self.git("checkout", "-q", "-b", "change")
        self.save("tall-desktop.png", image((120, 3000), p_5_400=(255, 255, 255, 255), p_6_2500=(255, 255, 255, 255)))
        head = self.commit("two changes")
        card = cards(self.report(base, head, full_budget=0)[1])[self.path("tall-desktop.png")]
        windows = re.findall(r'<div class="win".*?</div></div>', pane(card, "act"), re.S)
        self.assertEqual([len(boxes(w)) for w in windows], [1, 1])
        self.assertNotIn("<template>", card)

    def test_the_comment_starts_with_the_link_when_the_report_has_a_url(self) -> None:
        base, head = self.branch()
        data = self.diff(base, head)
        plain = sd.render(data, "https://x.invalid/runs/9")
        linked = sd.render(data, "https://x.invalid/runs/9", report_url="https://x.invalid/artifacts/7")
        self.assertEqual(linked.splitlines()[:6], [sd.MARKER, "### Screenshot changes", "",
                                                   "**View the visual diff: [report.html](https://x.invalid/artifacts/7)**", "",
                                                   "2 modified, 1 added, 1 deleted."])
        self.assertIn("logged in to GitHub", linked)
        self.assertNotIn("edited-phone", linked)
        self.assertNotIn("View the visual diff", plain)

    def test_only_the_changed_bands_are_embedded_with_their_labels(self) -> None:
        base = self.on_main("tall", **{"tall-desktop.png": image((120, 3000))})
        self.git("checkout", "-q", "-b", "change")
        self.save("tall-desktop.png", image((120, 3000), p_5_400=(255, 255, 255, 255), p_6_2500=(255, 255, 255, 255)))
        head = self.commit("two changes")
        _, page, _ = self.report(base, head, full_budget=0)
        card = cards(page)[self.path("tall-desktop.png")]
        self.assertIn("Rows y 250-551 of 3,000 (unchanged rows omitted)", card)
        self.assertIn("Rows y 2,350-2,651 of 3,000 (unchanged rows omitted)", card)
        self.assertIn("250 unchanged rows (y 0-250) omitted from this report", card)
        self.assertIn("1,799 unchanged rows (y 551-2,350) omitted from this report", card)
        self.assertNotIn('class="allbtn"', card)
        self.assertEqual(len(re.findall(r"data:image/webp", card)), 2 * 2)
        _, whole, _ = self.report(base, head)
        card = cards(whole)[self.path("tall-desktop.png")]
        self.assertIn('class="allbtn"', card)
        self.assertIn("Show all 3,000 rows", card)
        self.assertEqual(len(re.findall(r"data:image/webp", card)), 2)
        self.assertIn('(y 551-2,350) hidden: use "Show all rows"', card)

    def test_a_smaller_budget_cuts_context_first_then_scales_down_and_says_so(self) -> None:
        base = self.on_main("noise", **{f"noise{n}-phone.png": noise((120, 900), n) for n in range(3)})
        self.git("checkout", "-q", "-b", "change")
        for n in range(3):
            img = noise((120, 900), n)
            img.putpixel((3, 450), (1, 2, 3, 255))
            img.putpixel((4, 40 + n), (1, 2, 3, 255))
            self.save(f"noise{n}-phone.png", img)
        head = self.commit("changes")
        data = self.diff(base, head)
        full, notes = sd.build_report(data, self.repo, full_budget=0)
        self.assertEqual(notes, [])
        with unittest.mock.patch.object(sd, "LEVELS", [(0, 1.0)]):
            less, _ = sd.build_report(data, self.repo, full_budget=0)
        with unittest.mock.patch.object(sd, "LEVELS", [(0, 0.5)]):
            small, _ = sd.build_report(data, self.repo, full_budget=0)
        self.assertLess(len(less), len(full))
        self.assertLess(len(small), len(less))
        budget = (len(less) + len(full)) // 2
        page, notes = sd.build_report(data, self.repo, budget=budget, full_budget=0)
        self.assertLessEqual(len(page), budget)
        self.assertEqual(len(notes), 1)
        self.assertRegex(notes[0], r"context around each change was cut from 150 to (60|20|0) rows\.$")
        budget = (len(small) + len(less)) // 2
        page, notes = sd.build_report(data, self.repo, budget=budget, full_budget=0)
        self.assertLessEqual(len(page), budget)
        self.assertRegex(notes[0], r"cut from 150 to 0 rows and the images were scaled down to \d+%\.$")
        self.assertIn(notes[0], page)

    def test_the_budget_counts_the_notes_and_the_bytes_that_are_written(self) -> None:
        base = self.on_main("noise", **{f"noise{n}-phone.png": noise((120, 900), n) for n in range(3)})
        self.git("checkout", "-q", "-b", "change")
        for n in range(3):
            img = noise((120, 900), n)
            img.putpixel((3, 450), (1, 2, 3, 255))
            self.save(f"noise{n}-phone.png", img)
        head = self.commit("changes")
        data = self.diff(base, head)
        with unittest.mock.patch.object(sd, "LEVELS", [(60, 1.0)]):
            edge, notes = sd.build_report(data, self.repo, full_budget=0)
        self.assertEqual(len(notes), 1)
        budget = len(edge.encode()) - 1
        self.assertLessEqual(len(edge) - len(notes[0]), budget)
        page, notes = sd.build_report(data, self.repo, budget=budget, full_budget=0)
        self.assertLessEqual(len(page.encode()), budget)
        self.assertTrue(notes)
        page, notes = sd.build_report(data, self.repo, budget=budget // 3, full_budget=0)
        self.assertLessEqual(len(page.encode()), budget // 3)
        self.assertTrue(any("cut from 150" in n for n in notes))

    def test_the_markup_stays_within_the_share_the_budget_keeps_for_it(self) -> None:
        names = [f"page{n:02d}-desktop.png" for n in range(12)]
        base = self.on_main("pages", **{name: image((200, 2400)) for name in names})
        self.git("checkout", "-q", "-b", "change")
        for name in names:
            self.save(name, image((200, 2400), p_5_100=(255, 0, 0, 255), p_5_1200=(255, 0, 0, 255), p_5_2300=(255, 0, 0, 255)))
        head = self.commit("three changes each")
        with unittest.mock.patch.object(sd, "LEVELS", [(20, 1.0)]):
            page, _ = sd.build_report(self.diff(base, head), self.repo, full_budget=0)
        markup = len(re.sub(r"data:image/[a-z]+;base64,[A-Za-z0-9+/=]+", "", page).encode())
        self.assertLessEqual(markup, 60_000 + 6_500 * len(names))
        self.assertEqual(len(re.findall(r"data:image/webp", page)), len(names) * 3 * 2)

    def test_a_budget_nothing_fits_in_leaves_images_out_instead_of_failing(self) -> None:
        self.git("checkout", "-q", "-b", "change")
        self.save("edited-phone.png", noise((300, 300)))
        self.save("taller-phone.png", noise((300, 300), 2))
        head = self.commit("noise")
        _, page, notes = self.report("main", head, budget=90_000)
        self.assertTrue(any("without their images" in n for n in notes))
        self.assertIn("The images of this file are left out", page)
        self.assertEqual(sorted(cards(page)), [self.path("edited-phone.png"), self.path("taller-phone.png")])
        self.assertIn("Screenshot size changed from 100x80 to 300x300", page)

    def test_a_file_that_is_not_an_image_and_a_same_pixels_file_get_a_card_without_tabs(self) -> None:
        self.git("checkout", "-q", "-b", "pr")
        (self.repo / self.directory / "edited-phone.png").write_bytes(b"not a png")
        self.save("same-phone.png", image(), compress_level=0)
        head = self.commit("odd")
        _, page, _ = self.report("main", head)
        by = cards(page)
        self.assertIn("Could not be compared", by[self.path("edited-phone.png")])
        self.assertIn("Same pixels as the stored reference", by[self.path("same-phone.png")])
        self.assertEqual(tabs(by[self.path("same-phone.png")]), [])

    def test_the_command_writes_report_html_and_from_json_adds_the_link(self) -> None:
        _, head = self.branch()
        out = self.repo / "out"
        _, shown = run("--base", "main", "--head", head, "--out", str(out), "--repo", str(self.repo))
        self.assertIn("report.html:", shown)
        self.assertTrue((out / "report.html").read_text(encoding="utf-8").startswith("<!doctype html>"))
        self.assertNotIn("View the visual diff", (out / "summary.md").read_text(encoding="utf-8"))
        run("--from-json", str(out / "summary.json"), "--out", str(out), "--report-url", "https://x.invalid/a/1")
        self.assertIn("**View the visual diff: [report.html](https://x.invalid/a/1)**", (out / "summary.md").read_text(encoding="utf-8"))

    def test_no_changes_make_no_report(self) -> None:
        sha = self.git("rev-parse", "HEAD")
        out = self.repo / "out"
        run("--base", sha, "--head", sha, "--out", str(out), "--repo", str(self.repo))
        self.assertFalse((out / "report.html").exists())


class Bands(unittest.TestCase):
    def test_a_change_gets_context_and_stays_inside_the_image(self) -> None:
        self.assertEqual(sd.make_bands([(10, 12), (900, 901)], 1000, 150), [(0, 162), (750, 1000)])

    def test_changes_whose_context_touches_or_nearly_touches_are_one_band(self) -> None:
        self.assertEqual(sd.make_bands([(500, 501), (800, 801)], 2000, 150), [(350, 951)])
        self.assertEqual(sd.make_bands([(500, 501), (841, 842)], 2000, 150), [(350, 992)])
        self.assertEqual(len(sd.make_bands([(500, 501), (900, 901)], 2000, 150)), 2)

    def test_no_context_keeps_only_the_changed_rows(self) -> None:
        self.assertEqual(sd.make_bands([(500, 501), (900, 901)], 2000, 0), [(500, 501), (900, 901)])

    def test_a_very_scattered_change_is_capped_by_merging_the_nearest(self) -> None:
        runs = [(y, y + 1) for y in range(0, 20_000, 400)]
        bands = sd.make_bands(runs, 20_000, 0)
        self.assertEqual(len(bands), sd.MAX_BANDS)
        self.assertTrue(all(bands[i][1] <= bands[i + 1][0] for i in range(len(bands) - 1)))
        self.assertEqual((bands[0][0], bands[-1][1]), (0, 19_601))

    def test_changed_rows_come_from_the_mask_and_the_extra_rows(self) -> None:
        old, new = image((100, 80)), image((100, 100), p_5_10=(255, 0, 0, 255), p_5_11=(255, 0, 0, 255), p_9_50=(255, 0, 0, 255))
        self.assertEqual(sd.changed_runs(old, new, sd.changed_mask(old, new)), [(10, 12), (50, 51), (80, 100)])

    def test_a_different_width_changes_every_row(self) -> None:
        old, new = image((100, 80)), image((90, 80))
        self.assertEqual(sd.changed_runs(old, new, sd.changed_mask(old, new)), [(0, 80)])

    def test_the_ratio_reads_like_vitests(self) -> None:
        self.assertEqual([sd._ratio(x) for x in (0, 0.02, 0.0213456, 0.00004)], ["0", "0.02", "0.0213", "<0.0001"])

    def test_big_images_are_png_not_webp(self) -> None:
        self.assertEqual(sd.encode(Image.new("RGB", (sd.WEBP_MAX + 1, 2)), 1.0)[0], "image/png")
        self.assertEqual(sd.encode(Image.new("RGBA", (30, 30), GRAY), 1.0)[0], "image/webp")


class Command(unittest.TestCase):
    def test_the_command_is_registered(self) -> None:
        self.assertIn("screenshot-diff", cli.COMMANDS)

    def test_without_pillow_the_command_says_what_to_install(self) -> None:
        err = io.StringIO()
        with unittest.mock.patch.object(sd, "PILLOW", False), contextlib.redirect_stderr(err):
            code = sd.run(argparse.Namespace())
        self.assertEqual(code, 2)
        self.assertIn("images", err.getvalue())

    def test_the_cli_imports_without_pillow(self) -> None:
        script = "import sys; sys.modules['PIL'] = None; import fleet_tools.cli as c; print('screenshot-diff' in c.COMMANDS)"
        done = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "True")


class Workflow(unittest.TestCase):
    def test_the_workflow_looks_for_the_marker_the_tool_writes(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(sd.MARKER, text)
        self.assertIn(sd.DIRECTORY, text)
        self.assertIn(f"retention-days: {sd.RETENTION_DAYS}", text)

    def test_the_workflow_uploads_the_report_and_summary_json_unzipped_and_no_zip_of_images(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        uploads = text.split("actions/upload-artifact@")[1:]
        self.assertEqual(len(uploads), 2)
        for upload, name in zip(uploads, ("report.html", "summary.json"), strict=True):
            step = upload.split("\n      - ")[0]
            self.assertIn(f"screenshot-diff/{name}", step)
            self.assertIn("archive: false", step)
            self.assertIn(f"retention-days: {sd.RETENTION_DAYS}", step)
            self.assertNotIn(".png", step)

    def test_the_tool_runs_from_fleets_checkout_not_the_pull_requests(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("repository: AnthonyPluth/fleet", text)
        self.assertIn('pip install --quiet "./.fleet[images]"', text)
        self.assertIn("python -I -m fleet_tools.cli screenshot-diff", text)
        self.assertNotIn("pull_request_target:", text)
        self.assertIn("workflow_call:", text)


if __name__ == "__main__":
    unittest.main()
