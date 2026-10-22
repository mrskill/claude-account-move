"""claude_app_running ignores helpers that outlive the app (chrome-native-host,
the crash handler) and keeps treating every other process of the bundle as
"running". The decision reads the executable PATH of each candidate process
(`ps -o comm=`), never its arguments. Stand-ins print chosen answers."""
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
NATIVE_EXE = B + "Helpers/chrome-native-host"
CRASHPAD_EXE = B + "Frameworks/Electron Framework.framework/Helpers/chrome_crashpad_handler"
RENDERER_EXE = B + "Frameworks/Claude Helper (Renderer).app/Contents/MacOS/Claude Helper (Renderer)"
GPU_EXE = B + "Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper"
MAIN_EXE = B + "MacOS/Claude"
GONE, ERR, EMPTY = "<gone>", "<err>", "<empty>"   # markers for the ps stand-in

# (executable path as ps reports it, full command line as the lookup prints it)
NATIVE = (NATIVE_EXE, NATIVE_EXE + " chrome-extension://fcoeoabgfenejglbffodgkkbkcdhcgfn/")
CRASHPAD = (CRASHPAD_EXE, CRASHPAD_EXE + " --no-rate-limit --database=Support/Crashpad")
RENDERER = (RENDERER_EXE, RENDERER_EXE + " --type=renderer --user-data-dir=Support")
GPU = (GPU_EXE, GPU_EXE + " --type=gpu-process")
MAIN = (MAIN_EXE, MAIN_EXE)
# Helpers whose ARGUMENTS name the excluded programs: they must still count.
TRICK_CRASHPAD = (RENDERER_EXE, RENDERER_EXE + " --type=utility other/chrome_crashpad_handler")
TRICK_NATIVE = (GPU_EXE, GPU_EXE + " " + NATIVE_EXE + " extra/Claude Helper")
TRICK_BUNDLE_CRASHPAD = (RENDERER_EXE, RENDERER_EXE + " " + CRASHPAD_EXE + " --x")
# The same names under another executable path of the bundle: not the exact path.
LOOKALIKE = (B + "Helpers/other/chrome-native-host", B + "Helpers/other/chrome-native-host x")


class AppCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.n = 0

    def lookup(self, procs=(), exact=1, full=None):
        """Stand-ins: the pid lookup (`-x Claude` exits `exact`; the bundle
        lookup prints "pid command" lines and exits `full`) and the
        `ps -o comm= -p PID` reader. Returns the env that selects them."""
        self.n += 1
        full = (0 if procs else 1) if full is None else full
        data = os.path.join(self.tmp, "lines-%d.txt" % self.n)
        comms = os.path.join(self.tmp, "comms-%d.txt" % self.n)
        with open(data, "w") as fh, open(comms, "w") as fc:
            for i, (comm, command) in enumerate(procs):
                fh.write("%d %s\n" % (1000 + i, command))
                fc.write("%d\t%s\n" % (1000 + i, comm))
        pg = os.path.join(self.tmp, "lookup-%d.sh" % self.n)
        with open(pg, "w") as fh:
            fh.write('#!/bin/sh\nif [ "$1" = "-x" ]; then exit %d; fi\n'
                     'cat "%s"\nexit %d\n' % (exact, data, full))
        os.chmod(pg, 0o755)
        ps = os.path.join(self.tmp, "ps-%d.sh" % self.n)
        with open(ps, "w") as fh:
            fh.write('#!/bin/sh\n'
                     'v=$(awk -F"\\t" -v p="$4" \'$1==p {print $2}\' "%s")\n'
                     'case "$v" in\n'
                     '  "%s") exit 1;;\n'
                     '  "%s") exit 2;;\n'
                     '  "%s") exit 0;;\n'
                     'esac\nprintf "%%s\\n" "$v"\nexit 0\n' % (comms, GONE, ERR, EMPTY))
        os.chmod(ps, 0o755)
        return {"CLAUDE_ACCOUNT_MOVE_PGREP": pg, "CLAUDE_ACCOUNT_MOVE_PS": ps}

    def running(self, env):
        with mock.patch.dict(os.environ, env):
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

    def test_arguments_naming_the_excluded_programs_do_not_exclude(self):
        for trick in (TRICK_CRASHPAD, TRICK_NATIVE, TRICK_BUNDLE_CRASHPAD):
            self.assertTrue(self.running(self.lookup([trick])), trick)
            self.assertTrue(self.running(self.lookup([NATIVE, trick, CRASHPAD])), trick)

    def test_only_the_exact_executable_path_is_excluded(self):
        self.assertTrue(self.running(self.lookup([LOOKALIKE])))

    def test_an_unreadable_comm_is_running(self):
        for comm in (EMPTY, ERR, B + "Helpers/chrome-native-host...", "..."):
            self.assertTrue(self.running(self.lookup([(comm, NATIVE[1])])), comm)

    def test_a_process_gone_between_the_calls_is_not_running(self):
        self.assertFalse(self.running(self.lookup([(GONE, RENDERER[1])])))

    def test_an_unparsable_pid_is_running(self):
        env = self.lookup([NATIVE])
        with open(env["CLAUDE_ACCOUNT_MOVE_PGREP"], "w") as fh:
            fh.write('#!/bin/sh\nif [ "$1" = "-x" ]; then exit 1; fi\n'
                     'echo "garbled line"\nexit 0\n')
        self.assertTrue(self.running(env))

    def test_ps_missing_is_running(self):
        env = self.lookup([NATIVE])
        env["CLAUDE_ACCOUNT_MOVE_PS"] = os.path.join(self.tmp, "no-such-ps")
        self.assertTrue(self.running(env))

    def test_the_main_process_is_running(self):
        self.assertTrue(self.running(self.lookup([MAIN])))
        # named exactly Claude, whatever the bundle lookup says
        self.assertTrue(self.running(self.lookup([NATIVE], exact=0)))

    def test_a_lookup_failure_is_running(self):
        self.assertTrue(self.running(self.lookup([], full=2)))
        self.assertTrue(self.running(self.lookup([], exact=2)))
        env = self.lookup([])
        env["CLAUDE_ACCOUNT_MOVE_PGREP"] = os.path.join(self.tmp, "missing")
        self.assertTrue(self.running(env))

    def test_success_without_a_readable_line_is_running(self):
        self.assertTrue(self.running(self.lookup([], full=0)))

    def test_blockers_name_the_command_cut_to_120_chars(self):
        with mock.patch.dict(os.environ, self.lookup([NATIVE, RENDERER])):
            found = common.claude_app_blockers()
        self.assertEqual(len(found), 1)
        self.assertIn("Claude Helper (Renderer)", found[0])
        self.assertLessEqual(len(found[0]), 120)
        self.assertTrue(found[0].endswith("..."))

    def test_refusal_prints_the_blocking_command(self):
        h = Home(os.path.join(self.tmp, "h"))
        r = h.run("repair", "--apply", **self.lookup([NATIVE, RENDERER]))
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 3, out)
        self.assertIn("Quit the Claude app first", out)
        self.assertIn("Blocking process:", out)
        self.assertIn("Claude Helper (Renderer)", out)
        self.assertNotIn("chrome-native-host", out)

    def test_only_lingering_helpers_do_not_refuse(self):
        h = Home(os.path.join(self.tmp, "h"))
        r = h.run("repair", "--apply", **self.lookup([NATIVE, CRASHPAD]))
        self.assertNotIn("Quit the Claude app first", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
