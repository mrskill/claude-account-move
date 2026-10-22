"""CAM-FINISH-WRONGCID: finish names every card whose target copy is bound to
another session, with the last message time of both transcripts and a
direction tag. Every such card stays a failure."""
import os
import sys
import time
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fixture import CC, uid  # noqa: E402
from test_cli import parse  # noqa: E402
from test_v011 import Base  # noqa: E402


def now_ms():
    return int(time.time() * 1000)


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%d.%m %H:%M")


class WrongCid(Base):
    def rebind(self, target, n, cid, title="resumed title"):
        os.unlink(os.path.join(target, "local_%s.json" % uid(n, 9)))
        self.h.card(target, n, cid, title=title)

    def entry(self, rep):
        return rep["stores"][CC]["target_copy_wrong_cid_list"][0]

    def test_fresh_foreign_cid_fails_and_is_tagged_newer(self):
        _, target = self.moved()
        self.h.transcript(uid(60), last_ms=now_ms() + 3600000)
        self.rebind(target, 11, uid(60))
        r = self.finish()
        self.assertEqual(r.returncode, 3, r.stdout)
        rep = parse(r)
        self.assertEqual(rep["stores"][CC]["target_copy_wrong_cid"], 1)
        self.assertFalse({c["id"]: c for c in rep["checks"]}[CC + ":loss"]["ok"])
        self.assertEqual(self.entry(rep)["direction"], "newer")
        self.assertNotIn("rebound", r.stdout)

    def test_older_cid_fails_and_is_tagged_older(self):
        _, target = self.moved()
        self.h.transcript(uid(61), last_ms=now_ms() - 7 * 86400000)
        self.rebind(target, 11, uid(61))
        r = self.finish()
        self.assertEqual(r.returncode, 3, r.stdout)
        rep = parse(r)
        self.assertEqual(self.entry(rep)["direction"], "OLDER")
        self.assertTrue(rep["wrong_id_lines"][0].endswith("OLDER title "
                                                         "«resumed title»"),
                        rep["wrong_id_lines"])

    def test_missing_transcript_prints_none_and_fails(self):
        _, target = self.moved()
        self.rebind(target, 11, uid(77))
        r = self.finish()
        self.assertEqual(r.returncode, 3, r.stdout)
        rep = parse(r)
        e = self.entry(rep)
        self.assertIsNone(e["now_last_ms"])
        self.assertEqual(e["direction"], "OLDER")
        self.assertIn("now %s (last none) OLDER" % uid(77)[:8],
                      rep["wrong_id_lines"][0])

    def test_line_has_both_times_ids_and_title_on_screen(self):
        _, target = self.moved()
        new_ms = now_ms() + 3600000
        self.h.transcript(uid(60), last_ms=new_ms)
        self.rebind(target, 11, uid(60), title="Quarterly plan")
        r = self.h.run("finish", "--wait-login", "0")
        self.assertEqual(r.returncode, 3, r.stdout)
        lines = [x for x in r.stdout.splitlines() if "wrong id:" in x]
        self.assertEqual(len(lines), 1, r.stdout)
        rec_ms = parse(self.finish())["stores"][CC][
            "target_copy_wrong_cid_list"][0]["recorded_last_ms"]
        self.assertIsNotNone(rec_ms)
        self.assertIn("wrong id: local_%s.json recorded %s (last %s) now %s "
                      "(last %s) newer title «Quarterly plan»"
                      % (uid(11, 9), uid(11)[:8], stamp(rec_ms), uid(60)[:8],
                         stamp(new_ms)), lines[0])

    def test_list_is_stored_in_the_report(self):
        _, target = self.moved()
        self.rebind(target, 11, uid(77), title="")
        rep = parse(self.finish())
        e = self.entry(rep)
        self.assertEqual(e["card"], "local_%s.json" % uid(11, 9))
        self.assertEqual(e["recorded"], uid(11))
        self.assertEqual(e["now"], uid(77))
        self.assertIsNotNone(e["recorded_last_ms"])
        self.assertEqual(len(rep["wrong_id_lines"]), 1)
        self.assertNotIn("title", rep["wrong_id_lines"][0])

    def test_screen_list_is_capped_at_twenty(self):
        from claude_account_move import post_move_check as post
        lines = ["wrong id: local_%d.json" % i for i in range(25)]
        capped = post.cap_lines(lines)
        self.assertEqual(capped[:20], lines[:20])
        self.assertEqual(len(capped), 21)
        self.assertIn("5 more", capped[20])
        self.assertEqual(post.cap_lines(lines[:20]), lines[:20])


if __name__ == "__main__":
    unittest.main()
