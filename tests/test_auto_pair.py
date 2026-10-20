"""CAM-AUTO-PAIR: prepare picks the active pair from storage when the app log
names none. Synthetic storage only."""
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fixture import CC, ENTRY, Home, uid  # noqa: E402
from claude_account_move import store as st  # noqa: E402
from claude_account_move.common import Paths  # noqa: E402

ACC, OLD, NEW, OTHER = uid(1), uid(2), uid(3), uid(4)


def now_ms():
    return int(time.time() * 1000)


class AutoPair(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-autopair-")
        self.h = Home(self.tmp)
        self.h.accounts(cli=ACC, app=ACC)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def paths(self):
        return Paths(self.h.home, self.h.state)

    def prep(self, *extra):
        return self.h.run("prepare", "--json", "--wait-sync", "0", *extra)

    def active_detail(self, r):
        import json
        rep = json.loads(r.stdout)
        return [c for c in rep["checks"] if c["id"] == "static_accessibility"][0]

    def two_orgs(self, gap_ms):
        t = now_ms()
        d1 = self.h.pair(CC, ACC, OLD)
        d2 = self.h.pair(CC, ACC, NEW)
        self.h.card(d1, 1, uid(11), act=t - gap_ms)
        self.h.card(d2, 2, uid(12), act=t)

    def test_proposal_never_makes_prepare_ready(self):
        self.two_orgs(10 * 60 * 1000)
        r = self.prep()
        self.assertNotEqual(r.returncode, 0)
        c = self.active_detail(r)
        self.assertFalse(c["ok"], c)
        self.assertIn("Proposal only", c["detail"])
        self.assertIn("%s/%s" % (ACC[:8], NEW[:8]), c["detail"])

    def test_printed_command_is_a_full_invocation(self):
        self.two_orgs(10 * 60 * 1000)
        c = self.active_detail(self.prep())
        want = "%s prepare --active-pair %s/%s" % (os.path.abspath(ENTRY), ACC, NEW)
        self.assertIn(want, c["detail"])
        self.assertTrue(os.path.isabs(ENTRY))

    def test_ambiguous_prints_one_command_per_candidate(self):
        self.two_orgs(10 * 1000)
        c = self.active_detail(self.prep())
        self.assertFalse(c["ok"])
        self.assertIn("not guessing", c["detail"])
        for org in (OLD, NEW):
            self.assertIn("prepare --active-pair %s/%s" % (ACC, org), c["detail"])

    def test_boundary_60000_ms_is_ambiguous(self):
        t = 10 ** 13
        for gap, ambiguous in ((60000, True), (60001, False)):
            h = Home(tempfile.mkdtemp(prefix="cam-ap-b-"))
            self.addCleanup(shutil.rmtree, h.root, True)
            h.card(h.pair(CC, ACC, OLD), 1, uid(11), act=t - gap)
            h.card(h.pair(CC, ACC, NEW), 2, uid(12), act=t)
            idx = st.scan(Paths(h.home, h.state))
            pair, note, _c = st.auto_pair(idx, ACC, now_ms=t)
            self.assertEqual(pair is None, ambiguous, (gap, note))

    def test_no_cards_is_refused(self):
        self.h.pair(CC, ACC, OLD)
        self.h.pair(CC, ACC, NEW)
        c = self.active_detail(self.prep())
        self.assertFalse(c["ok"])
        self.assertIn("no usable session cards", c["detail"])

    def test_future_dated_card_is_ignored_and_said(self):
        t = now_ms()
        self.h.card(self.h.pair(CC, ACC, OLD), 1, uid(11), act=t - 600000)
        self.h.card(self.h.pair(CC, ACC, NEW), 2, uid(12), act=t + 3600000)
        pair, note, _c = st.auto_pair(st.scan(self.paths()), ACC)
        self.assertEqual(pair, (ACC, OLD))
        self.assertIn("future were ignored", note)

    def test_sync_apply_with_only_a_proposal_writes_nothing(self):
        self.two_orgs(10 * 60 * 1000)
        d_new = os.path.join(self.h.support, CC, ACC, NEW)
        before = sorted(os.listdir(d_new))
        r = self.h.run("sync", "--apply")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(sorted(os.listdir(d_new)), before)
        self.assertIn("sync --active-pair", r.stdout + r.stderr)

    def test_finish_with_only_a_proposal_is_not_ready(self):
        self.two_orgs(10 * 60 * 1000)
        pair = "%s/%s" % (ACC, NEW)
        self.assertEqual(self.prep("--active-pair", pair).returncode, 0)
        r = self.h.run("finish", "--wait-login", "0", "--json")
        self.assertNotEqual(r.returncode, 0, r.stdout)

    def test_override_wins(self):
        self.two_orgs(10 * 60 * 1000)
        c = self.active_detail(self.prep("--active-pair", "%s/%s" % (ACC, OLD)))
        self.assertTrue(c["ok"], c)
        self.assertNotIn("Proposal only", c["detail"])

    def test_log_pair_unchanged(self):
        self.two_orgs(10 * 60 * 1000)
        self.h.log((ACC, OLD))
        pair = st.live_pair(self.paths(), index=st.scan(self.paths()), notes=[],
                            auto_account=ACC)
        self.assertEqual(pair, (ACC, OLD))
        c = self.active_detail(self.prep())
        self.assertTrue(c["ok"], c)
        self.assertNotIn("Proposal only", c["detail"])

    def test_other_account_is_ignored(self):
        t = now_ms()
        self.h.card(self.h.pair(CC, OTHER, NEW), 5, uid(15), act=t)
        self.h.card(self.h.pair(CC, ACC, OLD), 6, uid(16), act=t - 600000)
        pair, note, _c = st.auto_pair(st.scan(self.paths()), ACC)
        self.assertEqual(pair, (ACC, OLD))


if __name__ == "__main__":
    unittest.main()
