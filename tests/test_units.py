import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from claude_account_move import post_move_check as post  # noqa: E402
from claude_account_move import pre_move_check as pre  # noqa: E402
from claude_account_move import store as st  # noqa: E402
from claude_account_move.cli import login_reason  # noqa: E402
from claude_account_move.common import Paths  # noqa: E402
from fixture import CC, Home, iso, uid  # noqa: E402


class Classify(unittest.TestCase):
    def test_buckets(self):
        tmap = {uid(1): ["x"], uid(2, 9): ["y"]}
        cards = {"local_%s.json" % uid(1): uid(1),
                 "local_%s.json" % uid(2, 9): "",
                 "local_%s.json" % uid(3, 9): "",
                 "local_%s.json" % uid(4, 9): uid(9)}
        live, dead, no_tr = post.classify(cards, tmap)
        self.assertEqual(live, ["local_%s.json" % uid(2, 9)])
        self.assertEqual(dead, ["local_%s.json" % uid(3, 9)])
        self.assertEqual(no_tr, [("local_%s.json" % uid(4, 9), uid(9))])

    def test_upper_case_card_name_still_counts_as_loss(self):
        low = uid(10, 0xabc)
        live, dead, _ = post.classify({"local_%s.json" % low.upper(): ""}, {low: ["x"]})
        self.assertEqual((len(live), len(dead)), (1, 0))


class Verdict(unittest.TestCase):
    def test_contract(self):
        kw = dict(missing=[], lost_ids=[])
        self.assertEqual(post.store_verdict({}, [], [], 10, 10, **kw), 0)
        self.assertEqual(post.store_verdict({}, [], ["dead"], 10, 10, **kw), 0)
        self.assertEqual(post.store_verdict({}, ["x"], [], 10, 10, **kw), 3)
        self.assertEqual(post.store_verdict({"d": ["x"]}, [], [], 10, 10, **kw), 3)
        self.assertEqual(post.store_verdict({}, [], [], 9, 10, **kw), 3)
        self.assertEqual(post.store_verdict({}, [], [], 10, 10, missing=["a"],
                                            lost_ids=[]), 3)
        self.assertEqual(post.store_verdict({}, [], [], 10, 10, missing=None,
                                            lost_ids=None), 0)
        self.assertEqual(post.store_verdict({}, [], [], 10, 10, missing=[],
                                            lost_ids=["i"]), 3)
        with self.assertRaises(TypeError):
            post.store_verdict({}, [], [], 10, 10)


class Lag(unittest.TestCase):
    def test_median_of_live_sessions(self):
        now = 1_000_000_000_000

        def row(lag, age):
            real = now - age * 1000
            return {"real_ms": real, "card_ms": real - lag * 1000, "lag_s": lag}
        self.assertTrue(pre.lag_verdict([row(76, 60), row(120, 90), row(226, 30)], now)[0])
        bad = [row(7014, 200), row(7750, 250), row(8692, 300), row(53255, 100),
               row(7541, 150)]
        ok, live, med = pre.lag_verdict(bad, now)
        self.assertEqual((ok, live, med), (False, 5, 7750))
        self.assertIsNone(pre.lag_verdict([row(9000, 7200)], now)[0])
        self.assertTrue(pre.lag_verdict([row(30, 60), row(9000, 100), row(40, 120)],
                                        now)[0])


class Heartbeat(unittest.TestCase):
    def test_optional_and_age(self):
        self.assertTrue(pre.check_sync_agent(None)[0])
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "hb")
            self.assertFalse(pre.check_sync_agent(p)[0])
            open(p, "w").close()
            self.assertTrue(pre.check_sync_agent(p)[0])
            old = os.stat(p).st_mtime - 3600
            os.utime(p, (old, old))
            self.assertFalse(pre.check_sync_agent(p, 30)[0])
            self.assertTrue(pre.check_sync_agent(p, 90)[0])
            fut = os.stat(p).st_mtime + 7200
            os.utime(p, (fut, fut))
            self.assertFalse(pre.check_sync_agent(p)[0])


class LoginReason(unittest.TestCase):
    def ident(self, cli, app, cs="ok", as_="ok"):
        return {"cli": {"status": cs, "value": cli},
                "app": {"status": as_, "value": app}}

    def test_order(self):
        self.assertIsNone(login_reason(self.ident("b", "b"), "a", False))
        self.assertEqual(login_reason(self.ident("a", "a"), "a", False), "app_not_switched")
        self.assertIsNone(login_reason(self.ident("a", "a"), "a", True))
        self.assertEqual(login_reason(self.ident("a", "b"), "a", False), "cli_lags")
        self.assertEqual(login_reason(self.ident(None, None, "invalid", "invalid"),
                                      "a", False), "identity_invalid")
        self.assertEqual(login_reason(self.ident(None, "b", "absent"), "a", False),
                         "identity_invalid")


class Scan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-unit-")
        self.h = Home(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_aliases_are_one_physical_directory(self):
        real = self.h.pair(CC, uid(1), uid(2))
        self.h.card(real, 1, uid(11))
        other = os.path.join(self.h.support, CC, uid(3))
        os.makedirs(other)
        os.symlink(real, os.path.join(other, uid(4)), target_is_directory=True)
        idx = st.scan(Paths(self.h.home))
        s = idx.stores[CC]
        self.assertEqual((len(s.aliases), len(s.dirs)), (2, 1))
        self.assertEqual({a["kind"] for a in s.aliases}, {"dir", "link"})

    def test_link_outside_store_and_dangling_are_excluded_and_counted(self):
        self.h.pair(CC, uid(1), uid(2))
        outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(outside)
        a = os.path.join(self.h.support, CC, uid(5))
        os.makedirs(a)
        os.symlink(outside, os.path.join(a, uid(6)), target_is_directory=True)
        os.symlink(os.path.join(self.tmp, "nowhere"), os.path.join(a, uid(7)))
        idx = st.scan(Paths(self.h.home))
        self.assertEqual(idx.obs.excluded_outside, 1)
        self.assertEqual(idx.obs.excluded_dangling, 1)
        self.assertEqual(len(idx.stores[CC].dirs), 1)

    def test_malformed_card_makes_observation_incomplete(self):
        d = self.h.pair(CC, uid(1), uid(2))
        self.h.card(d, 1, uid(11))
        with open(os.path.join(d, "local_bad.json"), "w") as fh:
            fh.write("{not json")
        idx = st.scan(Paths(self.h.home))
        self.assertFalse(idx.obs.complete())
        self.assertFalse(pre.check_observation(idx)[0])

    def test_duplicates_and_zombies(self):
        d1 = self.h.pair(CC, uid(1), uid(2))
        d2 = self.h.pair(CC, uid(3), uid(4))
        self.h.card(d1, 1, uid(11))
        self.h.card(d1, 2, uid(11))          # second card naming the same session
        self.h.card(d2, 3, uid(12))
        open(os.path.join(d2, "deleted_%s" % uid(3, 9)), "w").close()
        idx = st.scan(Paths(self.h.home))
        self.assertFalse(pre.check_duplicates(idx)[0])
        self.assertFalse(pre.check_zombies(idx)[0])

    def test_tail_reader_ignores_non_turns_and_mtime(self):
        self.h.transcript(uid(1), last_ms=1_700_000_000_000)
        p = os.path.join(self.h.projects, uid(1) + ".jsonl")
        old = 1_000_000
        os.utime(p, (old, old))
        self.assertEqual(st.last_message_ms(p), 1_700_000_000_000)
        self.assertIsNone(st.last_message_ms(p + ".missing"))

    def test_tail_reader_falls_back_to_whole_file(self):
        p = os.path.join(self.tmp, "big.jsonl")
        import json
        with open(p, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "timestamp": iso(1_700_000_000_000),
                                 "message": {"content": "x"}}) + "\n")
            for _ in range(20000):
                fh.write(json.dumps({"type": "queue-operation",
                                     "timestamp": iso(1_800_000_000_000),
                                     "pad": "y" * 30}) + "\n")
        self.assertGreater(os.path.getsize(p), st.TAIL_BYTES)
        self.assertEqual(st.last_message_ms(p), 1_700_000_000_000)


class EffectiveLagging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-lag-")
        self.h = Home(self.tmp)
        self.src = self.h.pair(CC, uid(1), uid(2))
        self.dst = self.h.pair(CC, uid(3), uid(4))
        self.h.card(self.dst, 1, uid(11))
        self.idx = st.scan(Paths(self.h.home))
        s = self.idx.stores[CC]
        self.by_dir = {r: set(d["cards"]) for r, d in s.dirs.items()}
        self.union = s.union_names()
        self.src_real = os.path.realpath(self.src)
        self.dst_real = os.path.realpath(self.dst)
        self.lag = {self.src_real: sorted(self.union)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_source_directory_is_tolerated(self):
        eff = post.effective_lagging(self.lag, self.idx, CC, uid(1), uid(3),
                                     self.by_dir, self.union)
        self.assertEqual(eff, {})

    def test_empty_target_directory_is_not(self):
        lag = {self.dst_real: ["x"]}
        eff = post.effective_lagging(lag, self.idx, CC, uid(1), uid(3),
                                     self.by_dir, self.union)
        self.assertEqual(eff, lag)

    def test_unknown_source_tolerates_nothing(self):
        eff = post.effective_lagging(self.lag, self.idx, CC, None, uid(3),
                                     self.by_dir, self.union)
        self.assertEqual(eff, self.lag)

    def test_non_empty_partial_source_directory_is_not_tolerated(self):
        self.h.card(self.src, 2, uid(12))
        idx = st.scan(Paths(self.h.home))
        s = idx.stores[CC]
        by_dir = {r: set(d["cards"]) for r, d in s.dirs.items()}
        lag = {self.src_real: sorted(s.union_names() - by_dir[self.src_real])}
        eff = post.effective_lagging(lag, idx, CC, uid(1), uid(3), by_dir,
                                     s.union_names())
        self.assertEqual(eff, lag)


if __name__ == "__main__":
    unittest.main()
