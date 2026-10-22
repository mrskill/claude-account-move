"""claude_app_running ignores helpers that outlive the app (chrome-native-host,
the crash handler) and keeps treating every other process of the bundle as
"running". A stand-in for the process lookup prints chosen lines."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fixture import Home  # noqa: E402

from claude_account_move import common  # noqa: E402

B = "/Applications/Claude.app/Contents/"
NATIVE = B + "Helpers/chrome-native-host chrome-extension://fcoeoabgfenejglbffodgkkbkcdhcgfn/"
CRASHPAD = (B + "Frameworks/Electron Framework.framework/Helpers/chrome_crashpad_handler "
            "--no-rate-limit --database=/Users/x/Library/Application Support/Claude/Crashpad")
RENDERER = (B + "Frameworks/Claude Helper (Renderer).app/Contents/MacOS/"
            "Claude Helper (Renderer) --type=renderer --user-data-dir=/Users/x/Support")
GPU = (B + "Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper --type=gpu-process")
MAIN = B + "MacOS/Claude"


class AppCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def lookup(self, lines=(), exact=1, full=None):
        """Stand-in lookup: `-x Claude` exits `exact`; `-fl PATTERN` prints the
        lines and exits `full` (default 0 when there are lines, else 1)."""
        full = (0 if lines else 1) if full is None else full
        data = os.path.join(self.tmp, "lines-%d.txt" % len(os.listdir(self.tmp)))
        with open(data, "w") as fh:
            for i, ln in enumerate(lines):
                fh.write("%d %s\n" % (1000 + i, ln))
        p = os.path.join(self.tmp, "lookup-%d.sh" % len(os.listdir(self.tmp)))
        with open(p, "w") as fh:
            fh.write('#!/bin/sh\nif [ "$1" = "-x" ]; then exit %d; fi\n'
                     'cat "%s"\nexit %d\n' % (exact, data, full))
        os.chmod(p, 0o755)
        return p

    def running(self, script):
        with mock.patch.dict(os.environ, {"CLAUDE_ACCOUNT_MOVE_PGREP": script}):
            return common.claude_app_running()

    def test_only_chrome_native_host_is_not_running(self):
        self.assertFalse(self.running(self.lookup([NATIVE])))

    def test_only_crashpad_is_not_running(self):
        self.assertFalse(self.running(self.lookup([CRASHPAD])))

    def test_both_lingering_helpers_together_are_not_running(self):
        self.assertFalse(self.running(self.lookup([NATIVE, CRASHPAD])))

    def test_a_renderer_helper_is_running(self):
        self.assertTrue(self.running(self.lookup([RENDERER])))
        self.assertTrue(self.running(self.lookup([GPU])))

    def test_a_renderer_next_to_lingering_helpers_is_running(self):
        self.assertTrue(self.running(self.lookup([NATIVE, RENDERER, CRASHPAD])))

    def test_the_main_process_is_running(self):
        self.assertTrue(self.running(self.lookup([MAIN])))
        # named exactly Claude, whatever the bundle lookup says
        self.assertTrue(self.running(self.lookup([NATIVE], exact=0)))

    def test_a_lookup_failure_is_running(self):
        self.assertTrue(self.running(self.lookup([], full=2)))
        self.assertTrue(self.running(self.lookup([], exact=2)))
        self.assertTrue(self.running(os.path.join(self.tmp, "missing")))

    def test_success_without_a_readable_line_is_running(self):
        self.assertTrue(self.running(self.lookup([], full=0)))

    def test_blockers_name_the_command_cut_to_120_chars(self):
        with mock.patch.dict(os.environ,
                             {"CLAUDE_ACCOUNT_MOVE_PGREP": self.lookup([NATIVE, RENDERER])}):
            found = common.claude_app_blockers()
        self.assertEqual(len(found), 1)
        self.assertIn("Claude Helper (Renderer)", found[0])
        self.assertLessEqual(len(found[0]), 120)
        self.assertTrue(found[0].endswith("..."))

    def test_refusal_prints_the_blocking_command(self):
        h = Home(os.path.join(self.tmp, "h"))
        r = h.run("repair", "--apply",
                  CLAUDE_ACCOUNT_MOVE_PGREP=self.lookup([NATIVE, RENDERER]))
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 3, out)
        self.assertIn("Quit the Claude app first", out)
        self.assertIn("Blocking process:", out)
        self.assertIn("Claude Helper (Renderer)", out)
        self.assertNotIn("chrome-native-host", out)

    def test_only_lingering_helpers_do_not_refuse(self):
        h = Home(os.path.join(self.tmp, "h"))
        r = h.run("repair", "--apply",
                  CLAUDE_ACCOUNT_MOVE_PGREP=self.lookup([NATIVE, CRASHPAD]))
        self.assertNotIn("Quit the Claude app first", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
