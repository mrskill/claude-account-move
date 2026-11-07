"""CAM-START-WAIT-PROGRESS: every wait of `auto` that blocks on the owner shows a
countdown. wait_app_start (the wait for the app to start under the new account)
printed nothing for up to 300 s. Class guard: each wait_* function of auto_move
is run with a short limit, a small poll and a fake app that never answers, and
must print at least one countdown line. A new silent wait fails this test.
Fake HOME only."""
import inspect
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fixture import Home  # noqa: E402
from test_cli import A, build_source  # noqa: E402
from test_auto import Base  # noqa: E402  (its fixtures only)

from claude_account_move import auto_move, cli, common  # noqa: E402
from claude_account_move import ru_text as ru  # noqa: E402
from claude_account_move.common import Paths  # noqa: E402


class Say(object):
    """A stand-in for cli.Out: collects the lines."""

    def __init__(self):
        self.lines = []

    def say(self, text=""):
        self.lines.append(text)


class WaitsShowProgress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-waitprog-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.h = Home(self.tmp)
        build_source(self.h)                   # app and CLI both on A: nobody logs in
        self.paths = Paths(self.h.home, self.h.state)

    # one recipe per wait function: call it so that it blocks until the limit
    def recipes(self, out):
        return {
            "wait_app_closed": lambda: auto_move.wait_app_closed(0.3, 0.01, out),
            "wait_app_switch": lambda: auto_move.wait_app_switch(
                cli, self.paths, A, False, 0.3, 0.01, out),
            "wait_app_start": lambda: auto_move.wait_app_start(
                self.paths, A, (None, 0), 0.3, 0.01, out),
        }

    def test_every_wait_prints_a_countdown_line_when_nobody_answers(self):
        waits = sorted(n for n, f in inspect.getmembers(auto_move, inspect.isfunction)
                       if n.startswith("wait_app_") and f.__module__ == auto_move.__name__)
        out = Say()
        recipes = self.recipes(out)
        self.assertEqual(waits, sorted(recipes),
                         "a wait_app_* function without a recipe here: add it, "
                         "and make it print a countdown")
        for name in waits:
            out.lines[:] = []
            with mock.patch.object(common, "claude_app_running", lambda: True):
                recipes[name]()
            self.assertTrue([ln for ln in out.lines if ln.strip()],
                            "%s waited for the owner and printed nothing" % name)

    def test_wait_app_start_countdown_starts_at_the_limit_and_ticks_every_30_s(self):
        """Same pattern as wait_app_closed: a line at the start, then every 30 s."""
        out = Say()
        clock = [1000.0]

        def sleep(s):
            clock[0] += s

        with mock.patch.object(auto_move.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(auto_move.time, "sleep", sleep):
            res = auto_move.wait_app_start(self.paths, A, (None, 0), 100, 10, out)
        self.assertIsNone(res)
        marker = ru.START_WAIT.split("%")[0]
        lines = [ln for ln in out.lines if marker in ln]
        nums = [int(re.search(r"(\d+)", ln[len(marker):]).group(1)) for ln in lines]
        self.assertEqual(nums[0], 100, lines)
        self.assertGreaterEqual(len(nums), 3, lines)
        self.assertEqual(nums, sorted(nums, reverse=True), "the countdown must go down")
        self.assertLessEqual(len(nums), 5, "it must tick every 30 s, not every poll")


class AutoPrintsTheStartCountdown(Base):
    def test_a_move_whose_app_never_starts_shows_the_countdown(self):
        build_source(self.h)
        import fixture
        with mock.patch.object(fixture.OwnerApp, "start", lambda self: None):
            code, text, err, _ = self.run_auto("--wait-start", "0.4")
        self.assertEqual(code, 3, text + err)
        self.assertIn(ru.START_WAIT.split("%")[0], text,
                      "the wait for the app start printed no countdown")

    def test_countdown_text_exists(self):
        self.assertIn("%d", ru.START_WAIT)
        self.assertTrue(re.search("[" + chr(0x400) + "-" + chr(0x4FF) + "]", ru.START_WAIT))
        self.assertNotIn(chr(0x451), ru.START_WAIT)
        self.assertNotIn(chr(0x2014), ru.START_WAIT)


if __name__ == "__main__":
    unittest.main()
