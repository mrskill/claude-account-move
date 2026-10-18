import io
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from claude_account_move import cli  # noqa: E402
from claude_account_move import progress as prog  # noqa: E402


class FakeTty(io.StringIO):
    def isatty(self):
        return True


def out_with(stream):
    out = cli.Out(False)
    out.stream = stream
    return out


class ProgressTest(unittest.TestCase):
    def test_non_tty_one_plain_line_per_step(self):
        buf = io.StringIO()
        pg = prog.Progress(out_with(buf), 5)
        pg.n = 2
        pg.step("scanning storage")
        line = buf.getvalue()
        self.assertEqual(line, "[#########...............]  40%  step 3/5  "
                               "scanning storage  (0s)\n")
        self.assertNotIn("\r", line)

    def test_done_is_full(self):
        buf = io.StringIO()
        pg = prog.Progress(out_with(buf), 2)
        pg.step("a")
        pg.step("done")
        self.assertIn("[########################] 100%  step 2/2  done",
                      buf.getvalue())

    def test_tty_redraws_in_place_and_breaks_before_text(self):
        buf = FakeTty()
        out = out_with(buf)
        pg = prog.Progress(out, 4)
        pg.step("one")
        pg.step("two")
        self.assertEqual(buf.getvalue().count("\r"), 2)
        self.assertNotIn("\n", buf.getvalue())
        out.say("hello")
        self.assertTrue(buf.getvalue().endswith("\nhello\n"))
        pg.close()
        self.assertIsNone(out.progress)

    def test_tty_wait_draws_off_tty_stays_silent(self):
        tty = FakeTty()
        prog.Progress(out_with(tty), 1).wait("waiting for login", 30, 60)
        self.assertIn(" 50%  waiting for login  limit 60s  (30s)", tty.getvalue())
        plain = io.StringIO()
        pg = prog.Progress(out_with(plain), 1)
        pg.wait("waiting for login", 30, 60)
        self.assertEqual(plain.getvalue(), "")
        pg.wait("waiting for login", 30, 60, plain=True)
        self.assertIn("50%", plain.getvalue())

    def test_quiet_prints_nothing(self):
        out = cli.Out(False, quiet=True)
        pg = prog.Progress(out, 3)
        pg.step("x")
        pg.wait("y", 1, 2, plain=True)
        pg.close()
        self.assertIsNone(out.stream)

    def test_json_mode_routes_to_stderr(self):
        out = cli.Out(True)
        self.assertIs(out.stream, sys.stderr)

    def test_hints_match_timeout_texts(self):
        self.assertEqual(prog.HINTS["cli_lags"], "run `claude /login` in a terminal")
        self.assertEqual(set(prog.HINTS), {"identity_invalid", "app_not_switched",
                                           "cli_lags"})

    def test_off_tty_still_line_for_a_long_step(self):
        buf = io.StringIO()
        pg = prog.Progress(out_with(buf), 3, tick=0.01, still=0.05)
        pg.step("scanning storage")
        time.sleep(0.3)
        pg.close()
        self.assertIn("  still scanning storage (0s)\n", buf.getvalue())
        self.assertNotIn("\r", buf.getvalue())

    def test_off_tty_default_is_30s_no_early_still(self):
        buf = io.StringIO()
        pg = prog.Progress(out_with(buf), 3, tick=0.01)
        pg.step("scanning storage")
        time.sleep(0.1)
        pg.close()
        self.assertNotIn("still", buf.getvalue())

    def test_tty_thread_ticks_and_stops_on_close(self):
        buf = FakeTty()
        pg = prog.Progress(out_with(buf), 3, tick=0.01)
        pg.step("scanning storage")
        time.sleep(0.2)
        self.assertGreater(buf.getvalue().count("\r"), 3)
        self.assertIn("|", buf.getvalue().replace("[", ""))
        th = pg.thread
        pg.close()
        done = buf.getvalue()
        time.sleep(0.1)
        self.assertEqual(buf.getvalue(), done)
        self.assertFalse(th.is_alive())

    def test_say_stops_redraw_until_next_step(self):
        buf = FakeTty()
        out = out_with(buf)
        pg = prog.Progress(out, 3, tick=0.01)
        pg.step("one")
        out.say("text")
        mark = buf.getvalue()
        time.sleep(0.1)
        self.assertEqual(buf.getvalue(), mark)
        pg.close()

    def _args(self):
        import argparse
        return argparse.Namespace(json=False, quiet=False)

    def test_exception_inside_step_closes_thread_and_ends_line(self):
        from unittest import mock
        buf = FakeTty()
        out = out_with(buf)
        before = threading.active_count()

        def boom(*a, **k):
            pg = prog.Progress(out, 3, tick=0.01)
            pg.step("scanning storage")
            time.sleep(0.05)
            raise ValueError("pair error")
        with mock.patch.object(cli, "_cmd_prepare", boom):
            with mock.patch.object(cli.Out, "of", return_value=out):
                with self.assertRaises(ValueError):
                    cli.cmd_prepare(self._args())
        self.assertTrue(buf.getvalue().endswith("\n"))
        self.assertIsNone(out.progress)
        time.sleep(0.05)
        self.assertEqual(threading.active_count(), before)

    def test_keyboard_interrupt_closes_thread_and_ends_line(self):
        from unittest import mock
        buf = FakeTty()
        out = out_with(buf)

        holder = []

        def stop(*a, **k):
            pg = prog.Progress(out, 3, tick=0.01)
            pg.wait("waiting for login", 1, 10)
            time.sleep(0.05)
            holder.append(pg.thread)
            raise KeyboardInterrupt
        with mock.patch.object(cli, "_cmd_finish", stop):
            with mock.patch.object(cli.Out, "of", return_value=out):
                with self.assertRaises(KeyboardInterrupt):
                    cli.cmd_finish(self._args())
        self.assertTrue(buf.getvalue().endswith("\n"))
        self.assertFalse(holder[0].is_alive())
        mark = buf.getvalue()
        time.sleep(0.1)
        self.assertEqual(buf.getvalue(), mark)

    def test_say_never_interleaves_with_still_off_tty(self):
        buf = io.StringIO()
        out = out_with(buf)
        pg = prog.Progress(out, 3, tick=0.001, still=0.002)
        for i in range(300):
            pg.step("work")
            out.say("SAYLINE %d" % i)
            time.sleep(0.002)
        pg.close()
        for line in buf.getvalue().splitlines():
            ok = (line.startswith("SAYLINE ") and line[8:].isdigit()
                  or line.startswith("  still work (") and line.endswith("s)")
                  or line.startswith("[") and "step " in line)
            self.assertTrue(ok, repr(line))


if __name__ == "__main__":
    unittest.main()
