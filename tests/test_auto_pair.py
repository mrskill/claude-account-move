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
from fixture import CC, Home, uid  # noqa: E402
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

    def test_auto_choice_by_newest_card(self):
        self.two_orgs(10 * 60 * 1000)
        r = self.prep()
        c = self.active_detail(r)
        self.assertTrue(c["ok"], c)
        self.assertIn("%s/%s" % (ACC[:8], NEW[:8]), c["detail"])
        self.assertIn("most recent session card", c["detail"])

    def test_ambiguous_is_refused_with_command(self):
        self.two_orgs(10 * 1000)
        c = self.active_detail(self.prep())
        self.assertFalse(c["ok"])
        self.assertIn("not guessing", c["detail"])
        self.assertIn("--active-pair %s/%s" % (ACC, NEW), c["detail"])
        self.assertIn(OLD[:8], c["detail"])

    def test_no_cards_is_refused(self):
        self.h.pair(CC, ACC, OLD)
        self.h.pair(CC, ACC, NEW)
        c = self.active_detail(self.prep())
        self.assertFalse(c["ok"])
        self.assertIn("no session cards", c["detail"])

    def test_override_wins(self):
        self.two_orgs(10 * 60 * 1000)
        c = self.active_detail(self.prep("--active-pair", "%s/%s" % (ACC, OLD)))
        self.assertTrue(c["ok"], c)
        self.assertNotIn("chose pair", c["detail"])

    def test_log_pair_unchanged(self):
        self.two_orgs(10 * 60 * 1000)
        self.h.log((ACC, OLD))
        pair = st.live_pair(self.paths(), index=st.scan(self.paths()), notes=[],
                            auto_account=ACC)
        self.assertEqual(pair, (ACC, OLD))
        c = self.active_detail(self.prep())
        self.assertTrue(c["ok"], c)
        self.assertNotIn("chose pair", c["detail"])

    def test_other_account_is_ignored(self):
        t = now_ms()
        self.h.card(self.h.pair(CC, OTHER, NEW), 5, uid(15), act=t)
        self.h.card(self.h.pair(CC, ACC, OLD), 6, uid(16), act=t - 600000)
        pair, note, _c = st.auto_pair(st.scan(self.paths()), ACC)
        self.assertEqual(pair, (ACC, OLD))


if __name__ == "__main__":
    unittest.main()
