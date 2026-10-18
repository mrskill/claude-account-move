import io
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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

    def test_tty_say_clears_the_line_before_text(self):
        buf = FakeTty()
        out = out_with(buf)
        pg = prog.Progress(out, 4)
        pg.step("one")
        self.assertNotIn("\n", buf.getvalue())
        out.say("hello")
        self.assertTrue(buf.getvalue().endswith("\r\x1b[Khello\n"))
        pg.close()
        self.assertIsNone(out.progress)

    def test_tty_wait_draws_off_tty_stays_silent(self):
        tty = FakeTty()
        prog.Progress(out_with(tty), 1).wait("waiting for login", 30, 60)
        self.assertIn(" 50% waiting for login limit 60s 30s", tty.getvalue())
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

    def _frames(self, cols):
        import re
        from unittest import mock
        buf = FakeTty()
        with mock.patch.object(prog.shutil, "get_terminal_size",
                               return_value=os.terminal_size((cols, 24))):
            pg = prog.Progress(out_with(buf), 6, tick=0.01)
            pg.step("scanning storage of the long named account")
            time.sleep(0.1)
            pg.wait("waiting for login", 725, 900)
            time.sleep(0.1)
            pg.close()
        return re.findall(r"\r(.*?)\x1b\[K", buf.getvalue())

    def test_tty_frames_fit_the_width(self):
        for cols in (50, 40, 20):
            frames = self._frames(cols)
            self.assertGreater(len(frames), 5)
            for f in frames:
                self.assertLessEqual(len(f), cols - 1, (cols, f))

    def test_wide_terminal_keeps_full_text(self):
        frames = self._frames(120)
        self.assertTrue(any("scanning storage of the long named account" in f
                            for f in frames))
        self.assertTrue(any("limit 900s" in f for f in frames))
        self.assertIn("[########################]", prog.fit(100, "h", "t", 1, "", 120))

    def test_width_is_measured_on_the_written_stream(self):
        import re
        from unittest import mock

        class Sized(FakeTty):
            def fileno(self):
                return 99

        buf = Sized()
        with mock.patch.object(prog.os, "get_terminal_size",
                               return_value=os.terminal_size((50, 24))), \
                mock.patch.object(prog.shutil, "get_terminal_size",
                                  return_value=os.terminal_size((80, 24))):
            pg = prog.Progress(out_with(buf), 6, tick=0.01)
            pg.step("scanning storage of the long named account")
            time.sleep(0.1)
            pg.close()
        frames = re.findall(r"\r(.*?)\x1b\[K", buf.getvalue())
        self.assertGreater(len(frames), 3)
        self.assertTrue(all(len(f) <= 49 for f in frames))
        self.assertTrue(any(len(f) > 40 for f in frames))   # not the 20-col case

    def test_tiny_widths_never_overflow(self):
        for w, cap in ((0, 0), (1, 0), (2, 1)):
            f = prog.fit(50, "step 2/8", "scanning", 7, "|", w)
            self.assertLessEqual(len(f), cap, (w, f))
        self.assertEqual(prog.fit(50, "step 2/8", "scanning", 7, "|", 1), "")

    @staticmethod
    def _screen(raw):
        """Apply \\r and clear-to-end-of-line semantics to a byte stream."""
        rows = []
        for line in raw.split("\n")[:-1] if raw.endswith("\n") else raw.split("\n"):
            rows.append(line.split("\r")[-1].replace("\x1b[K", ""))
        return rows

    def test_one_finalized_line_per_step(self):
        from unittest import mock
        buf = FakeTty()
        with mock.patch.object(prog.shutil, "get_terminal_size",
                               return_value=os.terminal_size((120, 24))):
            pg = prog.Progress(out_with(buf), 3, tick=0.01)
            for name in ("one", "two", "done"):
                pg.step(name)
                time.sleep(0.06)
            pg.close()
        rows = self._screen(buf.getvalue())
        self.assertEqual(len(rows), 3, rows)
        for r, name in zip(rows, ("one", "two", "done")):
            self.assertIn("100%", r)
            self.assertIn(name, r)
            self.assertIn(" done", r)

    def test_wait_after_say_finalizes_the_previous_step(self):
        from unittest import mock
        buf = FakeTty()
        with mock.patch.object(prog.shutil, "get_terminal_size",
                               return_value=os.terminal_size((120, 24))):
            pg = prog.Progress(out_with(buf), 3, tick=0.01)
            pg.step("alpha")
            pg.out.progress = pg
            pg.end_line()
            pg.wait("bravo", 0, 100)
            pg.step("charlie")
            pg.close()
        rows = self._screen(buf.getvalue())
        self.assertEqual(sum(1 for r in rows if "alpha" in r and " done" in r),
                         1, rows)
        self.assertEqual(sum(1 for r in rows if "bravo" in r and " done" in r),
                         1, rows)

    def test_callback_progress_is_monotonic_within_a_step(self):
        import re
        buf = FakeTty()
        pg = prog.Progress(out_with(buf), 2, tick=10, throttle=0)
        pg.step("scanning storage")
        for i in range(0, 11):
            pg.update(i, 10)
        pg.close()
        pcts = [int(m) for m in re.findall(r"\] +(\d+)%", buf.getvalue())]
        self.assertGreater(len(pcts), 5)
        body = pcts[:-1]                      # the last frame is the finalized 100
        self.assertEqual(body, sorted(body))
        self.assertEqual(body[-1], 100)

    def test_update_is_throttled(self):
        buf = FakeTty()
        pg = prog.Progress(out_with(buf), 2, tick=10, throttle=60)
        pg.step("x")
        before = buf.getvalue().count("\r")
        for i in range(100):
            pg.update(i, 100)
        self.assertEqual(buf.getvalue().count("\r"), before + 1)
        pg.close()

    def test_unknown_total_bouncer_moves(self):
        import re
        buf = FakeTty()
        pg = prog.Progress(out_with(buf), 2, tick=0.05)
        pg.step("scanning storage")
        time.sleep(0.6)
        th = pg.thread
        pg.close()
        frames = re.findall(r"\r(.*?)\x1b\[K", buf.getvalue())[:-1]
        bars = {f.split("]")[0] for f in frames}
        self.assertTrue(all("<=>" in b for b in bars))
        self.assertGreater(len(bars), 2)
        self.assertIn("--%", frames[0])
        self.assertFalse(th.is_alive())

    def test_step_lines_fit_width_50(self):
        import re
        from unittest import mock
        buf = FakeTty()
        with mock.patch.object(prog.shutil, "get_terminal_size",
                               return_value=os.terminal_size((50, 24))):
            pg = prog.Progress(out_with(buf), 3, tick=0.01, throttle=0)
            for name in ("scanning storage", "reading transcripts", "done"):
                pg.step(name)
                pg.update(3, 7)
                time.sleep(0.05)
            pg.close()
        frames = re.findall(r"\r(.*?)\x1b\[K", buf.getvalue())
        self.assertGreater(len(frames), 6)
        self.assertTrue(all(len(f) <= 49 for f in frames), frames)

    def test_names_survive_at_width_50(self):
        for name in ("scanning storage", "reading transcripts"):
            for label in ("|", "done"):
                f = prog.fit(38, "2/8", name, 12, label, 50)
                self.assertIn(name, f)
                self.assertLessEqual(len(f), 49)
        f = prog.fit(38, "2/8", "comparing with the snapshot", 12, "|", 50)
        self.assertLessEqual(len(f), 49)
        self.assertIn("comparing with the", f)       # loses only its tail
        self.assertTrue(f.endswith(" 12s |"))
        self.assertEqual(prog.fit(100, "2/8", "scanning storage", 241, "done", 120),
                         "[########################] 100% 2/8 scanning storage "
                         "241s done")


class StoreProgressTest(unittest.TestCase):
    def test_transcripts_and_scan_callbacks_are_optional(self):
        from claude_account_move import store as st
        from claude_account_move.common import Paths
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            paths = Paths(home=d, state=None)
            calls = []
            st.transcripts(paths, lambda a, b: calls.append((a, b)))
            st.transcripts(paths)
            st.scan(paths, lambda a, b: calls.append((a, b)))
            st.scan(paths)
            self.assertEqual(calls, [])

    def test_scan_and_transcripts_report_real_counts(self):
        import tempfile
        from fixture import CC, Home, uid
        from claude_account_move import store as st
        from claude_account_move.common import Paths
        with tempfile.TemporaryDirectory() as d:
            h = Home(d)
            pair = h.pair(CC, uid(1), uid(2))
            for n in (11, 12, 13):
                h.card(pair, n, uid(n))
                h.transcript(uid(n))
            paths = Paths(home=h.home, state=h.state)
            calls = []
            idx = st.scan(paths, lambda a, b: calls.append((a, b)))
            self.assertEqual(calls, [(1, 3), (2, 3), (3, 3)])
            self.assertEqual(len(idx.stores[CC].union_names()), 3)
            tcalls = []
            st.transcripts(paths, lambda a, b: tcalls.append((a, b)))
            self.assertEqual(tcalls[-1], (3, 3))


    def _store_home(self, d):
        from fixture import CC, Home, uid
        h = Home(d)
        pair = h.pair(CC, uid(1), uid(2))
        for n in (11, 12, 13):
            h.card(pair, n, uid(n))
        return h, pair, CC

    def test_precount_ignores_stray_file_listed_first(self):
        import tempfile
        from claude_account_move import store as st
        from claude_account_move.common import Paths
        with tempfile.TemporaryDirectory() as d:
            h, pair, CC = self._store_home(d)
            paths = Paths(home=h.home, state=h.state)
            base = os.path.join(paths.support, CC)
            open(os.path.join(base, ".DS_Store"), "w").close()
            from fixture import uid
            acc = os.path.join(base, uid(1))
            open(os.path.join(acc, ".DS_Store"), "w").close()
            self.assertEqual(st._count_cards(paths), 3)
            calls = []
            st.scan(paths, lambda a, b: calls.append((a, b)))
            self.assertEqual(calls[-1], (3, 3))
            self.assertEqual(calls[0], (1, 3))

    def test_precount_skips_org_symlinked_outside_the_store(self):
        import tempfile
        from fixture import uid
        from claude_account_move import store as st
        from claude_account_move.common import Paths
        with tempfile.TemporaryDirectory() as d:
            h, pair, CC = self._store_home(d)
            paths = Paths(home=h.home, state=h.state)
            outside = os.path.join(d, "outside-" + "x")
            os.makedirs(outside)
            for i in range(5):
                open(os.path.join(outside, st.CARD_PREFIX + str(i) + ".json"),
                     "w").close()
            link = os.path.join(paths.support, st.STORES[0], uid(1), uid(9))
            os.symlink(outside, link)
            self.assertEqual(st._count_cards(paths), 3)
            idx = st.scan(paths)
            self.assertEqual(idx.obs.excluded_outside, 1)


if __name__ == "__main__":
    unittest.main()
