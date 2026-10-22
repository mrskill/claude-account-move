"""CAM-FINISH-WRONGCID: finish names the cards whose target copy is bound to
another session; a rebind to a newer live session is a note, not loss."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fixture import CC, uid  # noqa: E402
from test_cli import parse  # noqa: E402
from test_v011 import Base  # noqa: E402


def future_ms(extra=3600000):
    return int(time.time() * 1000) + extra


class WrongCid(Base):
    def rebind(self, target, n, cid, act, title="resumed title"):
        os.unlink(os.path.join(target, "local_%s.json" % uid(n, 9)))
        self.h.card(target, n, cid, act=act, title=title)

    def test_rebound_to_newer_live_session_is_a_note_and_ok(self):
        _, target = self.moved()
        self.h.transcript(uid(60), last_ms=future_ms())      # resumed later
        self.rebind(target, 11, uid(60), future_ms())
        r = self.finish()
        self.assertEqual(r.returncode, 0, r.stdout)
        rep = parse(r)
        store = rep["stores"][CC]
        self.assertEqual(store["target_copy_wrong_cid"], 0)
        self.assertEqual(store["target_copy_rebound_live"], 1)
        loss = {c["id"]: c for c in rep["checks"]}[CC + ":loss"]
        self.assertTrue(loss["ok"], loss)
        self.assertTrue(any("newer live session" in n and n.startswith(CC + ": 1")
                            for n in rep["notes"]), rep["notes"])
        self.assertEqual(rep["wrong_id_lines"], [])
        kinds = [e["kind"] for e in store["target_copy_wrong_cid_list"]]
        self.assertEqual(kinds, ["rebound_live"])

    def test_note_is_printed_for_a_human_run(self):
        _, target = self.moved()
        self.h.transcript(uid(60), last_ms=future_ms())
        self.rebind(target, 11, uid(60), future_ms())
        r = self.h.run("finish", "--wait-login", "0")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("note: " + CC + ": 1 target copies are bound to a newer "
                      "live session", r.stdout)
        self.assertNotIn("wrong id:", r.stdout)

    def test_rebound_to_older_transcript_fails(self):
        _, target = self.moved()
        self.rebind(target, 11, uid(12), act=1)   # transcript older than snapshot
        r = self.finish()
        self.assertEqual(r.returncode, 3, r.stdout)
        rep = parse(r)
        self.assertEqual(rep["stores"][CC]["target_copy_wrong_cid"], 1)
        self.assertEqual(rep["stores"][CC]["target_copy_rebound_live"], 0)
        self.assertFalse({c["id"]: c for c in rep["checks"]}[CC + ":loss"]["ok"])

    def test_rebound_to_missing_transcript_fails_even_when_card_is_new(self):
        _, target = self.moved()
        self.rebind(target, 11, uid(77), future_ms())   # no transcript at all
        r = self.finish()
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(parse(r)["stores"][CC]["target_copy_wrong_cid"], 1)

    def test_rebound_to_empty_id_fails(self):
        _, target = self.moved()
        self.rebind(target, 11, "", future_ms())
        r = self.finish()
        self.assertEqual(r.returncode, 3, r.stdout)

    def test_newer_transcript_alone_is_enough_but_old_card_and_old_transcript_not(self):
        _, target = self.moved()
        self.h.transcript(uid(60), last_ms=future_ms())
        self.rebind(target, 11, uid(60), act=1)       # card old, transcript newer
        self.assertEqual(self.finish().returncode, 0)

    def test_list_is_printed_with_short_ids_and_title(self):
        _, target = self.moved()
        self.rebind(target, 11, uid(77), act=1, title="Quarterly plan")
        r = self.h.run("finish", "--wait-login", "0")
        self.assertEqual(r.returncode, 3, r.stdout)
        line = [x for x in r.stdout.splitlines() if "wrong id:" in x]
        self.assertEqual(len(line), 1, r.stdout)
        self.assertIn("wrong id: local_%s.json recorded %s now %s title "
                      "«Quarterly plan»" % (uid(11, 9), uid(11)[:8],
                                                      uid(77)[:8]), line[0])

    def test_list_is_stored_in_the_report_and_capped_on_screen(self):
        _, target = self.moved()
        self.rebind(target, 11, uid(77), act=1, title="")
        rep = parse(self.finish())
        entry = rep["stores"][CC]["target_copy_wrong_cid_list"][0]
        self.assertEqual(entry["card"], "local_%s.json" % uid(11, 9))
        self.assertEqual(entry["recorded"], uid(11))
        self.assertEqual(entry["now"], uid(77))
        self.assertEqual(entry["kind"], "wrong_cid")
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
