"""CAM-FINISH-NOTLOSS: lagging non-target directories and stale target copies
that are recoverable elsewhere are notes, not loss."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fixture import CC, Home, uid  # noqa: E402
from test_cli import parse  # noqa: E402
from test_v011 import Base  # noqa: E402


def ids(rep):
    return {c["id"]: c for c in rep["checks"]}


class Lagging(Base):
    def new_session(self, target, n=50):
        self.h.transcript(uid(n))
        self.h.card(target, n, uid(n))

    def test_new_session_only_in_target_is_a_note(self):
        src, target = self.moved()
        self.new_session(target)
        r = self.finish()
        self.assertEqual(r.returncode, 0, r.stdout)
        rep = parse(r)
        c = ids(rep)
        self.assertNotIn(CC + ":lagging", c)
        self.assertTrue(c[CC + ":loss"]["ok"])
        self.assertEqual(rep["stores"][CC]["lagging_other_dirs_noted"], 1)
        self.assertEqual(rep["stores"][CC]["lagging_other_names_missing"], 1)
        self.assertTrue(any("not loss" in n and "1 other" in n
                            for n in rep["notes"]), rep["notes"])

    def test_note_is_printed_and_verdict_is_arrived(self):
        _, target = self.moved()
        self.new_session(target)
        r = self.h.run("finish", "--wait-login", "0")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("not loss", r.stdout)

    def test_name_missing_in_target_still_fails(self):
        src, target = self.moved()
        self.new_session(target)
        os.unlink(os.path.join(target, "local_%s.json" % uid(11, 9)))
        r = self.finish()
        self.assertEqual(r.returncode, 3, r.stdout)
        c = ids(parse(r))
        self.assertFalse(c[CC + ":target_names"]["ok"])
        self.assertIn(CC + ":lagging", c)

    def test_name_missing_anywhere_still_fails(self):
        src, target = self.moved()
        self.new_session(target)
        for d in (src, target):
            os.unlink(os.path.join(d, "local_%s.json" % uid(11, 9)))
        r = self.finish()
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(parse(r)["stores"][CC]["names_missing_anywhere"], 1)

    def test_name_only_in_non_target_dir_still_fails(self):
        src, target = self.moved()
        self.h.transcript(uid(51))
        self.h.card(src, 51, uid(51))             # new session, target lacks it
        r = self.finish()
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn(CC + ":lagging", ids(parse(r)))

    def test_store_without_target_directory_still_fails(self):
        from test_cli import build_source
        build_source(self.h)
        self.assertEqual(self.h.run("prepare").returncode, 0)
        from test_cli import switch_to_target
        switch_to_target(self.h)
        r = self.finish()                         # nothing synced yet
        self.assertEqual(r.returncode, 3, r.stdout)


class StaleCopy(Base):
    def stale_target(self, n=11):
        src, target = self.moved()
        os.unlink(os.path.join(target, "local_%s.json" % uid(n, 9)))
        self.h.card(target, n, uid(n), act=1, turns=0)
        return src, target

    def test_stale_target_with_fresh_copy_elsewhere_is_a_note(self):
        self.stale_target()
        r = self.finish()
        self.assertEqual(r.returncode, 0, r.stdout)
        rep = parse(r)
        self.assertNotIn(CC + ":target_fresh", ids(rep))
        self.assertEqual(rep["stores"][CC]["target_copy_stale_recoverable"], 1)
        self.assertEqual(rep["stores"][CC]["target_copy_regressed"], 0)
        self.assertTrue(any("older than recorded" in n for n in rep["notes"]))

    def test_no_fresh_copy_anywhere_still_fails(self):
        src, _ = self.stale_target()
        os.unlink(os.path.join(src, "local_%s.json" % uid(11, 9)))
        self.h.card(src, 11, uid(11), act=1, turns=0)
        r = self.finish()
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertFalse(ids(parse(r))[CC + ":target_fresh"]["ok"])

    def test_fresh_copy_with_other_session_id_still_fails(self):
        src, _ = self.stale_target()
        os.unlink(os.path.join(src, "local_%s.json" % uid(11, 9)))
        self.h.card(src, 11, uid(77))
        r = self.finish()
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn(CC + ":target_fresh", ids(parse(r)))

    def test_fresh_copy_with_empty_session_id_still_fails(self):
        src, _ = self.stale_target()
        os.unlink(os.path.join(src, "local_%s.json" % uid(11, 9)))
        self.h.card(src, 11, "")
        r = self.finish()
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn(CC + ":target_fresh", ids(parse(r)))


if __name__ == "__main__":
    unittest.main()
