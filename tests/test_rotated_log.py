"""CAM-ROTLOG: the app rotates main.log into main1.log, main2.log, ...; the pair
mention may sit only in a rotated file. Synthetic storage only."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fixture import Home, mention, uid, CC  # noqa: E402
from unittest import mock  # noqa: E402
from claude_account_move import store as st  # noqa: E402
from claude_account_move import pre_move_check as pre  # noqa: E402
from claude_account_move import auto_move  # noqa: E402
from claude_account_move.common import read_identity  # noqa: E402
import test_auto  # noqa: E402
import test_cli  # noqa: E402
from claude_account_move.common import Paths  # noqa: E402

ACC, OTHER, ORG_A, ORG_B, ORG_C = uid(1), uid(4), uid(21), uid(22), uid(23)
NOISE = "2026-01-01 10:00:00 info: nothing about a pair here"


def local_ts(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()


class RotatedLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-rotlog-")
        self.h = Home(self.tmp)
        self.h.accounts(cli=ACC, app=ACC)
        self.locked = []

    def lock(self, path):
        os.chmod(path, 0)
        self.locked.append(path)

    def tearDown(self):
        for p in self.locked:
            os.chmod(p, 0o644)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def paths(self):
        return Paths(self.h.home, self.h.state)

    def live(self, account=None):
        src = {}
        pair = st.live_pair(self.paths(), account=account, source=src)
        return pair, src

    def healthy_store(self):
        self.h.card(self.h.pair(CC, ACC, ORG_A), 1, uid(11))

    def prep(self, *extra):
        return self.h.run("prepare", "--json", "--wait-sync", "0", *extra)

    def active_check(self, r):
        rep = json.loads(r.stdout)
        c = [c for c in rep["checks"] if c["id"] == "static_accessibility"][0]
        return rep, c

    # Mutant: live_pair reads main.log only (the old behavior) -> fails.
    def test_pair_only_in_rotated_log_is_found(self):
        self.h.log_file("main.log", [NOISE])
        self.h.log_file("main1.log", [NOISE])
        self.h.log_file("main2.log", [mention(ACC, ORG_A)])
        pair, src = self.live()
        self.assertEqual(pair, (ACC, ORG_A))
        self.assertEqual(src["file"], "main2.log")

    # Mutant: a missing main.log aborts the search -> fails.
    def test_missing_main_log_is_skipped(self):
        self.h.log_file("main1.log", [mention(ACC, ORG_A)])
        pair, src = self.live()
        self.assertEqual(pair, (ACC, ORG_A))
        self.assertEqual(src["file"], "main1.log")

    # Mutant: the fallback is absent -> the with-rotated-file run stays NOT MEASURED.
    def test_prepare_no_longer_not_measured_with_rotated_pair(self):
        self.healthy_store()
        self.h.log_file("main.log", [NOISE])
        rep0, c0 = self.active_check(self.prep())
        self.assertFalse(c0["ok"], c0)
        self.assertIn("NOT MEASURED", c0["detail"])
        self.assertIsNone(rep0["pair_source"])
        self.h.log_file("main2.log", [mention(ACC, ORG_A, "2026-10-08 19:44:12")])
        r = self.prep()
        rep, c = self.active_check(r)
        self.assertTrue(c["ok"], c)
        self.assertNotIn("NOT MEASURED", c["detail"])
        self.assertEqual(rep["pair_source"]["file"], "main2.log")
        self.assertEqual(rep["pair_source"]["mention_time"], "2026-10-08 19:44:12")
        self.assertEqual(rep["pair_source"]["time_from"], "log_line")
        self.assertIn("main2.log", c["detail"])
        self.assertIn("2026-10-08 19:44:12", c["detail"])

    # Mutant: the text output drops the source (describe_source returns "") -> fails.
    def test_prepare_text_output_names_source_and_age(self):
        self.healthy_store()
        self.h.log_file("main.log", [NOISE])
        self.h.log_file("main3.log", [mention(ACC, ORG_A, "[2026-10-08 19:44:12.123]")])
        r = self.h.run("prepare", "--wait-sync", "0")
        self.assertIn("pair named in main3.log", r.stdout)
        self.assertIn("2026-10-08 19:44:12", r.stdout)
        self.assertIn(" h ago", r.stdout)

    # Mutant: "no logs" is turned into a confirmed pair (e.g. the auto proposal
    # is promoted) -> fails.
    def test_no_logs_at_all_is_not_measured(self):
        self.healthy_store()
        pair, src = self.live()
        self.assertIsNone(pair)
        self.assertEqual(src, {})
        found, problem = st.find_pair_mention(self.paths())
        self.assertIsNone(found)
        self.assertEqual(problem["problem"], "no_logs")
        rep, c = self.active_check(self.prep())
        self.assertFalse(c["ok"])
        self.assertIn("NOT MEASURED", c["detail"])

    # Mutant: the account filter is skipped for rotated files -> the other
    # account's pair is returned.
    def test_rotated_log_with_only_another_account_gives_no_pair(self):
        self.h.log_file("main.log", [NOISE])
        self.h.log_file("main1.log", [mention(OTHER, ORG_B)])
        pair, _src = self.live(account=ACC)
        self.assertIsNone(pair)
        found, problem = st.find_pair_mention(self.paths(), ACC)
        self.assertIsNone(found)
        self.assertEqual(problem["problem"], "not_found")

    # Mutant: the first file with ANY pair wins, not the first with the wanted
    # account -> returns None here.
    def test_older_file_with_wanted_account_beats_newer_file_with_other(self):
        self.h.log_file("main1.log", [mention(OTHER, ORG_B)])
        self.h.log_file("main2.log", [mention(ACC, ORG_A)])
        pair, src = self.live(account=ACC)
        self.assertEqual(pair, (ACC, ORG_A))
        self.assertEqual(src["file"], "main2.log")

    # Mutant: rotated logs are read before main.log, or the newest by mtime wins.
    def test_main_log_wins_over_rotated(self):
        self.h.log_file("main.log", [mention(ACC, ORG_A)], mtime=1000000)
        self.h.log_file("main1.log", [mention(ACC, ORG_B)], mtime=2000000)
        pair, src = self.live()
        self.assertEqual(pair, (ACC, ORG_A))
        self.assertEqual(src["file"], "main.log")

    # Mutant: the order is reversed (main3 before main1) or follows mtime.
    def test_main1_wins_over_main3(self):
        self.h.log_file("main.log", [NOISE])
        self.h.log_file("main1.log", [mention(ACC, ORG_A)], mtime=1000000)
        self.h.log_file("main3.log", [mention(ACC, ORG_B)], mtime=3000000)
        pair, src = self.live()
        self.assertEqual(pair, (ACC, ORG_A))
        self.assertEqual(src["file"], "main1.log")

    # Mutant: names are sorted as text (main10 before main2) -> ORG_B returned.
    def test_numeric_order_main2_before_main10(self):
        self.h.log_file("main2.log", [mention(ACC, ORG_A)])
        self.h.log_file("main10.log", [mention(ACC, ORG_B)])
        pair, src = self.live()
        self.assertEqual(pair, (ACC, ORG_A))
        self.assertEqual(src["file"], "main2.log")

    # Mutant: an unreadable file is treated like a missing one -> the rotated
    # pair is returned.
    def test_unreadable_main_log_is_not_measured_and_rotated_is_not_used(self):
        if os.geteuid() == 0:
            self.skipTest("root reads files whatever their mode")
        self.healthy_store()
        p = self.h.log_file("main.log", [NOISE])
        self.h.log_file("main1.log", [mention(ACC, ORG_A)])
        self.lock(p)
        pair, _src = self.live()
        self.assertIsNone(pair)
        found, problem = st.find_pair_mention(self.paths())
        self.assertIsNone(found)
        self.assertEqual(problem, {"problem": "unreadable", "file": "main.log"})
        rep, c = self.active_check(self.prep())
        self.assertFalse(c["ok"])
        self.assertIn("NOT MEASURED", c["detail"])
        self.assertIn("main.log exists but cannot be read", c["detail"])

    # Mutant: the search skips an unreadable rotated file and reads an older one.
    def test_unreadable_rotated_file_stops_the_search(self):
        if os.geteuid() == 0:
            self.skipTest("root reads files whatever their mode")
        self.h.log_file("main.log", [NOISE])
        p = self.h.log_file("main1.log", [NOISE])
        self.h.log_file("main2.log", [mention(ACC, ORG_A)])
        self.lock(p)
        pair, _src = self.live()
        self.assertIsNone(pair)
        _found, problem = st.find_pair_mention(self.paths())
        self.assertEqual(problem, {"problem": "unreadable", "file": "main1.log"})

    # Mutant: all rotated files are read before deciding (unreadable file met
    # AFTER a pair is found must not matter) -> the search fails instead.
    def test_unreadable_file_after_the_winner_is_never_touched(self):
        if os.geteuid() == 0:
            self.skipTest("root reads files whatever their mode")
        self.h.log_file("main.log", [NOISE])
        self.h.log_file("main1.log", [mention(ACC, ORG_A)])
        p = self.h.log_file("main2.log", [NOISE])
        self.lock(p)
        pair, src = self.live()
        self.assertEqual(pair, (ACC, ORG_A))
        self.assertEqual(src["file"], "main1.log")

    # Mutant: the first mention in a file wins.
    def test_last_mention_inside_one_file_wins(self):
        self.h.log_file("main.log", [NOISE])
        self.h.log_file("main1.log", [mention(ACC, ORG_A, "2026-10-01 10:00:00"),
                                      NOISE,
                                      mention(ACC, ORG_B, "2026-10-02 11:30:00"),
                                      NOISE])
        pair, src = self.live()
        self.assertEqual(pair, (ACC, ORG_B))
        self.assertEqual(src["mention_time"], "2026-10-02 11:30:00")

    # Mutant: the rotated read has no tail bound (reads the whole file) -> the
    # head-only mention is found.
    def test_rotated_logs_use_the_same_tail_bound(self):
        old = st.LOG_TAIL_BYTES
        st.LOG_TAIL_BYTES = 2000
        self.addCleanup(setattr, st, "LOG_TAIL_BYTES", old)
        self.h.log_file("main.log", [NOISE])
        self.h.log_file("main1.log", [mention(ACC, ORG_A)] + ["x" * 99] * 60)
        self.h.log_file("main2.log", [mention(ACC, ORG_B)])
        pair, src = self.live()
        self.assertEqual(pair, (ACC, ORG_B))
        self.assertEqual(src["file"], "main2.log")
        self.h.log_file("main1.log", ["x" * 99] * 60 + [mention(ACC, ORG_C)])
        pair, src = self.live()
        self.assertEqual(pair, (ACC, ORG_C))
        self.assertEqual(src["file"], "main1.log")

    # Mutant: fh.read() without a byte limit -> returns the grown file whole.
    def test_read_never_returns_more_than_the_bound(self):
        old = st.LOG_TAIL_BYTES
        st.LOG_TAIL_BYTES = 2000
        self.addCleanup(setattr, st, "LOG_TAIL_BYTES", old)
        p = self.h.log_file("main.log", ["y" * 99] * 60)
        text, err = st._read_log_tail(p)
        self.assertIsNone(err)
        self.assertLessEqual(len(text.encode("utf-8")), 2000)
        self.assertTrue(text.endswith("y" * 99 + "\n"))

    # Mutant: fh.read() without a byte limit -> a file that grows between the
    # seek and the read is consumed whole (the wrapper appends right after the
    # seek that positions the tail).
    def test_read_bound_holds_when_the_file_grows_during_the_read(self):
        old = st.LOG_TAIL_BYTES
        st.LOG_TAIL_BYTES = 2000
        self.addCleanup(setattr, st, "LOG_TAIL_BYTES", old)
        p = self.h.log_file("main.log", ["z" * 99] * 10)
        real_open = open

        class Growing(object):
            def __init__(self, fh):
                self.fh, self.seeks = fh, 0

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.fh.close()

            def tell(self):
                return self.fh.tell()

            def seek(self, *a):
                r = self.fh.seek(*a)
                self.seeks += 1
                if self.seeks == 2:
                    with real_open(p, "ab") as more:
                        more.write(b"g" * 5000)
                return r

            def read(self, *a):
                return self.fh.read(*a)

        st.open = lambda path, mode="r": Growing(real_open(path, mode))
        self.addCleanup(delattr, st, "open")
        text, err = st._read_log_tail(p)
        self.assertIsNone(err)
        self.assertLessEqual(len(text.encode("utf-8")), 2000)
        self.assertIn("g", text)

    # Mutant: the failing branches drop the source -> the detail lacks main1.log.
    def test_pair_folder_missing_still_names_source_and_age(self):
        self.healthy_store()
        self.h.log_file("main.log", [NOISE])
        self.h.log_file("main1.log", [mention(ACC, ORG_B, "2026-10-08 19:44:12")])
        rep, c = self.active_check(self.prep())
        self.assertFalse(c["ok"])
        self.assertIn("NOT MEASURED", c["detail"])
        self.assertIn("has no folder in the store", c["detail"])
        self.assertNotIn("names no pair", c["detail"])
        self.assertIn("main1.log", c["detail"])
        self.assertIn(" h ago", c["detail"])
        self.assertIn("2026-10-08 19:44:12", c["detail"])
        self.assertEqual(rep["pair_source"]["file"], "main1.log")

    # Mutant: the "not a directory inside the store" branch drops `extra`.
    def test_pair_outside_the_store_still_names_source_and_age(self):
        self.healthy_store()
        outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(outside)
        acc_dir = os.path.join(self.h.support, CC, ACC)
        os.symlink(outside, os.path.join(acc_dir, ORG_B))
        self.h.log_file("main1.log", [mention(ACC, ORG_B, "2026-10-08 19:44:12")])
        index = st.scan(self.paths())
        src = {}
        pair = st.live_pair(self.paths(), source=src)
        active = st.pair_dir(self.paths(), *pair)
        ok, detail = pre.check_static_access(index, active, [], src)
        self.assertFalse(ok)
        self.assertIn("not a directory inside the store", detail)
        self.assertIn("main1.log", detail)
        self.assertIn(" h ago", detail)

    # Mutant: the "not writable" branch drops `extra`.
    def test_pair_not_writable_still_names_source_and_age(self):
        if os.geteuid() == 0:
            self.skipTest("root writes whatever the mode")
        d = self.h.pair(CC, ACC, ORG_A)
        self.h.card(d, 1, uid(11))
        self.h.log_file("main1.log", [mention(ACC, ORG_A, "2026-10-08 19:44:12")])
        index = st.scan(self.paths())
        src = {}
        pair = st.live_pair(self.paths(), source=src)
        active = st.pair_dir(self.paths(), *pair)
        os.chmod(active, 0o500)
        self.addCleanup(os.chmod, active, 0o755)
        ok, detail = pre.check_static_access(index, active, [], src)
        self.assertFalse(ok)
        self.assertIn("not writable", detail)
        self.assertIn("main1.log", detail)
        self.assertIn(" h ago", detail)

    # Mutant: mention time always taken from the file mtime -> mention_time and
    # time_from differ.
    def test_age_and_source_from_the_line_both_formats(self):
        for line_ts, shown in (("2026-10-08 19:44:12", "2026-10-08 19:44:12"),
                               ("[2026-10-08 19:44:12.123]", "2026-10-08 19:44:12")):
            self.h.log_file("main.log", [NOISE])
            self.h.log_file("main1.log", [mention(ACC, ORG_A, line_ts)],
                            mtime=local_ts("2026-01-01 00:00:00"))
            now = local_ts("2026-10-10 19:44:12")
            found, problem = st.find_pair_mention(self.paths(), now=now)
            self.assertIsNone(problem)
            src = found["source"]
            self.assertEqual(src["file"], "main1.log")
            self.assertEqual(src["mention_time"], shown)
            self.assertEqual(src["age_hours"], 48.0)
            self.assertEqual(src["time_from"], "log_line")
            self.assertIn("time from the log line", st.describe_source(src))

    # Mutant: a line without a time is reported as if its time came from the
    # line, or the label is dropped.
    def test_line_without_time_falls_back_to_mtime_and_says_so(self):
        mt = local_ts("2026-10-08 10:00:00")
        self.h.log_file("main.log", [NOISE])
        self.h.log_file("main1.log", [mention(ACC, ORG_A, ts="")], mtime=mt)
        found, problem = st.find_pair_mention(self.paths(), now=mt + 7200)
        self.assertIsNone(problem)
        src = found["source"]
        self.assertEqual(src["time_from"], "file_mtime")
        self.assertEqual(src["mention_time"], "2026-10-08 10:00:00")
        self.assertEqual(src["age_hours"], 2.0)
        self.assertIn("file modification time", st.describe_source(src))

    # Mutant: run_checks passes account=None to live_pair again (the pair of the
    # OTHER account in a rotated log is then confirmed and prepare says ready).
    def test_prepare_ignores_a_rotated_pair_of_another_account(self):
        self.healthy_store()
        self.h.pair(CC, OTHER, ORG_B)       # the other account's folder exists, writable
        self.h.log_file("main.log", [])
        self.h.log_file("main1.log", [mention(OTHER, ORG_B)])
        r = self.prep()
        self.assertEqual(r.returncode, 3, r.stdout)
        rep, c = self.active_check(r)
        self.assertFalse(c["ok"], c)
        self.assertIn("NOT MEASURED", c["detail"])
        self.assertIsNone(rep["pair_source"])
        self.assertNotEqual(rep["verdict"], "ready")

    # Mutant: an explicit --active-pair is overridden by the logs.
    def test_active_pair_override_unchanged(self):
        self.h.log_file("main1.log", [mention(ACC, ORG_A)])
        src = {}
        pair = st.live_pair(self.paths(), override="%s/%s" % (ACC, ORG_B), source=src)
        self.assertEqual(pair, (ACC, ORG_B))
        self.assertEqual(src, {})
        self.healthy_store()
        self.h.pair(CC, ACC, ORG_B)
        rep, c = self.active_check(self.prep("--active-pair", "%s/%s" % (ACC, ORG_B)))
        self.assertTrue(c["ok"], c)
        self.assertIsNone(rep["pair_source"])


class AutoProbe(test_auto.Base):
    # Mutant: auto_move passes account=None to live_pair again -> the last pair of
    # the rotated log, which belongs to ANOTHER account (B), is confirmed and the
    # write probe goes into B's folder instead of the expected account A's folder.
    # (prepare filters by account too, so it passes on A; the probe is the second
    # reader of the log and must agree with it.)
    def test_auto_probes_only_the_expected_accounts_folder(self):
        h = self.h
        d_a = test_cli.build_source(h)
        d_b = h.pair(CC, test_cli.B, test_cli.O2)
        h.log_file("main.log", [])
        h.log_file("main1.log", [mention(test_cli.A, test_cli.O1),
                                 mention(test_cli.B, test_cli.O2)])
        with mock.patch.object(auto_move, "probe_write",
                               wraps=auto_move.probe_write) as probe:
            code, out, _err, _m = self.run_auto()
        self.assertEqual(code, 0, out)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(os.path.realpath(probe.call_args[0][0]), os.path.realpath(d_a))
        self.assertNotEqual(os.path.realpath(probe.call_args[0][0]), os.path.realpath(d_b))

    # Mutant: as above; here the log names ONLY the other account, so no write
    # probe may run at all (prepare stops first, nothing is written).
    def test_auto_writes_nothing_when_the_log_names_only_another_account(self):
        h = self.h
        test_cli.build_source(h)
        h.pair(CC, test_cli.B, test_cli.O2)
        h.log_file("main.log", [])
        h.log_file("main1.log", [mention(test_cli.B, test_cli.O2)])
        with mock.patch.object(auto_move, "probe_write",
                               side_effect=AssertionError("write probe reached")) as probe:
            code, out, _err, _m = self.run_auto("--json")
        self.assertEqual(probe.call_count, 0)
        self.assertNotEqual(code, 0, out)
        self.assertEqual(json.loads(out)["verdict"], "not_ready")


if __name__ == "__main__":
    unittest.main()
