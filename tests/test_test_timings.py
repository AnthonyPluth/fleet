import contextlib
import io
import json
import unittest

from consumers import temporary

from fleet_tools import cli, test_timings

WORKER = {"tests": [{"module": "tests.test_a", "class": "A", "test": "test_slow", "seconds": 2.0,
                     "phases": {"schema": 0.5}},
                    {"module": "tests.test_a", "class": "A", "test": "test_fast", "seconds": 0.5, "phases": {}}],
          "phases": {"schema": 1.0, "clean": 0.25}, "ended": 1}
OTHER = {"tests": [{"module": "tests.test_b", "class": "B", "test": "test_mid", "seconds": 1.0, "phases": {}}],
         "phases": {"schema": 0.5}}


class Timings(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = temporary(self)
        (self.dir / "1.json").write_text(json.dumps(WORKER))
        (self.dir / "2.json").write_text(json.dumps(OTHER))
        (self.dir / "notes.txt").write_text("not a timing file")

    def test_each_worker_s_file_is_summed(self) -> None:
        timed, phases, ended = test_timings.load(self.dir)
        self.assertEqual(len(timed), 3)
        self.assertEqual(phases, {"schema": 1.5, "clean": 0.25})
        self.assertEqual(ended, 1)

    def test_the_report_lists_the_slowest_tests_and_classes(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["test-timings", str(self.dir), "--top", "2"]), 0)
        self.assertEqual(out.getvalue().splitlines(), [
            "3 tests, 3.5s of test time summed over all processes",
            "Database set-up and clean-up, summed over all processes (including what a whole class shares):",
            "  clean: 0.2s",
            "  schema: 1.5s",
            "Connections ended to give a schema back (a test left one open): 1",
            "",
            "Slowest 2 tests",
            "     2.00s  tests.test_a.A.test_slow  schema=0.50",
            "     1.00s  tests.test_b.B.test_mid",
            "",
            "Slowest 2 classes (their tests' time summed)",
            "      2.5s     2 tests  tests.test_a.A",
            "      1.0s     1 tests  tests.test_b.B"])


if __name__ == "__main__":
    unittest.main()
