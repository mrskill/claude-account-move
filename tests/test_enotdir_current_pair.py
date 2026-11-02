"""ORACLE: `prepare` counts an ENOTDIR line only for the pair the app writes now.

Defect measured live on 02.11.2026. `prepare` failed on the check "active pair
writable" (pre_move_check.check_enotdir -> heal_active.recent_enotdir) because of
ENOTDIR lines from 01.11 19:16 about pair <A>/d9bbd39a, whose
local-agent-mode-sessions folder is a symlink. The app no longer writes there:
heal_active.log_pair(paths), the newest save/read/ENOTDIR line naming a store
pair, returns <A>/252e7efa, a real directory in both stores. The two org folders
share one real folder (d9bbd39a -> 252e7efa), so healing d9bbd39a would turn the
current pair into a symlink (ping-pong). The check must not block on a pair the
app has left.

Rule pinned here. When log_pair(paths) returns a pair, only ENOTDIR hits whose
(account, org) equals that current pair count. When log_pair returns None, every
recent hit on a symlinked pair counts (today's behaviour). Unchanged: the 2-day
window, the symlink condition, the ignoring of non-store paths.

Tests 1 and 5 of the brief (stale ENOTDIR on a pair the app left; direct
recent_enotdir) are red on the release b2ced7e; the others pin behaviour that
must not change. Fake HOME only; the real ~/Library is never touched.
"""
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import test_heal_active as tha  # noqa: E402
from fixture import CC, LAM, Home, uid  # noqa: E402
from test_cli import A, O1, build_source, parse  # noqa: E402

from claude_account_move import heal_active  # noqa: E402
from claude_account_move.common import Paths  # noqa: E402

STORES = (CC, LAM)
O_NOW = uid(0x25, 2)        # the pair the app writes now (252e7efa in the field)


def stamp(age):
    return (datetime.now() - age).strftime("%Y-%m-%d %H:%M:%S")


class EnotdirCurrentPair(tha.Base):
    """prepare: ENOTDIR on a symlinked pair the app has left does not block."""

    # the helper of the existing class, reused: appends an ENOTDIR line for the
    # pair (account, org) of a store and returns that pair's path
    enotdir = tha.PrepareEnotdir.enotdir

    def fresh(self):
        """A new fake home with account A, org O1 (cards), as build_source makes."""
        self.tmp = tempfile.mkdtemp(prefix="cam-enotdir-cur-")
        self.h = Home(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        build_source(self.h)

    def setUp(self):
        self.fresh()

    # -- fixtures -----------------------------------------------------------

    def current_pair_real(self):
        """O_NOW is a real directory in both stores; in each store O1 is an
        alias of it only where `alias_in` says so (see alias)."""
        for store in STORES:
            self.h.pair(store, A, O_NOW)

    def alias(self, store):
        """The live shape: in `store`, O1 -> O_NOW (O_NOW real, holding the cards)."""
        o1 = os.path.join(self.h.support, store, A, O1)
        now = os.path.join(self.h.support, store, A, O_NOW)
        shutil.rmtree(now, ignore_errors=True)      # the empty one made above
        os.rename(o1, now)
        os.symlink(now, o1)
        return o1

    def line(self, text, age=timedelta(minutes=5)):
        with open(self.h.home + "/Library/Logs/Claude/main.log", "a") as fh:
            fh.write("%s %s\n" % (stamp(age), text))

    def path(self, store, org):
        return os.path.join(self.h.support, store, A, org)

    def save_line(self, store, org, age=timedelta(minutes=5)):
        self.line("[info] Saved session local_x to %s/local_x.json"
                  % self.path(store, org), age)

    def read_line(self, store, org, age=timedelta(minutes=5)):
        self.line("[info] Read session local_x from %s/local_x.json"
                  % self.path(store, org), age)

    def log_pair(self):
        return heal_active.log_pair(Paths(self.h.home, self.h.state))

    def prepare(self):
        return self.h.run("prepare", "--json")

    # -- 1. the live case ---------------------------------------------------

    def test_stale_enotdir_on_left_pair_newer_save_on_real_pair_is_ready(self):
        for store in STORES:
            for kind in ("save", "read"):
                with self.subTest(store=store, newer_line=kind):
                    self.fresh()
                    self.current_pair_real()
                    self.alias(store)
                    self.enotdir(store, A, O1, timedelta(hours=1))
                    getattr(self, kind + "_line")(store, O_NOW)
                    self.assertEqual(self.log_pair(), (A, O_NOW))   # premise
                    r = self.prepare()
                    self.assertEqual(r.returncode, 0, r.stdout)
                    self.assertTrue(parse(r)["ready"], r.stdout)

    # -- 2. the app came back to the symlinked pair -------------------------

    def test_newer_line_names_the_symlinked_pair_again_fails(self):
        cases = (
            ("save", lambda s: self.save_line(s, O1)),
            ("read", lambda s: self.read_line(s, O1)),
            ("enotdir", lambda s: self.enotdir(s, A, O1, timedelta(minutes=5))),
        )
        for store in STORES:
            for kind, newer in cases:
                with self.subTest(store=store, newer_line=kind):
                    self.fresh()
                    self.current_pair_real()
                    o1 = self.alias(store)
                    self.enotdir(store, A, O1, timedelta(hours=1))
                    self.save_line(store, O_NOW, timedelta(minutes=30))
                    newer(store)                       # last word: O1 again
                    self.assertEqual(self.log_pair(), (A, O1))      # premise
                    r = self.prepare()
                    self.assertEqual(r.returncode, 3, r.stdout)
                    self.assertFalse(parse(r)["ready"])
                    self.assertIn("ENOTDIR", r.stdout)
                    self.assertIn(o1, r.stdout)

    # -- 3. log_pair is the symlinked pair itself ---------------------------

    def test_recent_enotdir_alone_current_pair_is_the_symlinked_one_fails(self):
        for store in STORES:
            with self.subTest(store=store):
                self.fresh()
                self.current_pair_real()
                o1 = self.alias(store)
                self.enotdir(store, A, O1, timedelta(hours=1))
                self.assertEqual(self.log_pair(), (A, O1))          # premise
                r = self.prepare()
                self.assertEqual(r.returncode, 3, r.stdout)
                self.assertFalse(parse(r)["ready"])
                self.assertIn(o1, r.stdout)

    # -- 4. log_pair is None: today's behaviour -----------------------------

    def test_no_current_pair_known_recent_enotdir_on_symlink_fails(self):
        for store in STORES:
            with self.subTest(store=store):
                self.fresh()
                self.current_pair_real()
                o1 = self.alias(store)
                self.enotdir(store, A, O1, timedelta(hours=1))
                self.line("[info] account changed: signed in as someone else",
                          timedelta(minutes=30))
                self.assertIsNone(self.log_pair())                  # premise
                r = self.prepare()
                self.assertEqual(r.returncode, 3, r.stdout)
                self.assertFalse(parse(r)["ready"])
                self.assertIn(o1, r.stdout)

    def test_no_current_pair_known_even_after_a_save_on_real_pair(self):
        """A save on the real pair BEFORE "account changed" does not make it
        current: log_pair forgets it, so every recent hit counts again."""
        for store in STORES:
            with self.subTest(store=store):
                self.fresh()
                self.current_pair_real()
                o1 = self.alias(store)
                self.enotdir(store, A, O1, timedelta(hours=2))
                self.save_line(store, O_NOW, timedelta(hours=1))
                self.line("[info] account changed", timedelta(minutes=30))
                self.assertIsNone(self.log_pair())                  # premise
                r = self.prepare()
                self.assertEqual(r.returncode, 3, r.stdout)
                self.assertIn(o1, r.stdout)


class RecentEnotdirUnit(tha.Base):
    """heal_active.recent_enotdir directly."""

    enotdir = tha.PrepareEnotdir.enotdir

    def setUp(self):
        tha.Base.setUp(self)
        build_source(self.h)
        for store in STORES:
            self.h.pair(store, A, O_NOW)

    def alias(self, store):
        o1 = os.path.join(self.h.support, store, A, O1)
        now = os.path.join(self.h.support, store, A, O_NOW)
        shutil.rmtree(now, ignore_errors=True)
        os.rename(o1, now)
        os.symlink(now, o1)
        return o1

    def append(self, text, age):
        with open(self.h.home + "/Library/Logs/Claude/main.log", "a") as fh:
            fh.write("%s %s\n" % (stamp(age), text))

    def paths(self):
        return Paths(self.h.home, self.h.state)

    def test_stale_enotdir_on_left_pair_returns_nothing(self):
        for store in STORES:
            with self.subTest(store=store):
                self.tearDown()
                tha.Base.setUp(self)
                build_source(self.h)
                for s in STORES:
                    self.h.pair(s, A, O_NOW)
                self.alias(store)
                self.enotdir(store, A, O1, timedelta(hours=1))
                self.append("[info] Saved session local_x to %s/local_x.json"
                            % os.path.join(self.h.support, store, A, O_NOW),
                            timedelta(minutes=5))
                self.assertEqual(heal_active.log_pair(self.paths()), (A, O_NOW))
                self.assertEqual(heal_active.recent_enotdir(self.paths()), [])

    def test_enotdir_on_the_current_symlinked_pair_is_returned(self):
        for store in STORES:
            with self.subTest(store=store):
                self.tearDown()
                tha.Base.setUp(self)
                build_source(self.h)
                for s in STORES:
                    self.h.pair(s, A, O_NOW)
                o1 = self.alias(store)
                self.enotdir(store, A, O1, timedelta(hours=1))
                hits = heal_active.recent_enotdir(self.paths())
                self.assertEqual([(s, p) for s, p, _l in hits], [(store, o1)])

    def test_unknown_current_pair_returns_every_recent_symlink_hit(self):
        for store in STORES:
            with self.subTest(store=store):
                self.tearDown()
                tha.Base.setUp(self)
                build_source(self.h)
                for s in STORES:
                    self.h.pair(s, A, O_NOW)
                o1 = self.alias(store)
                self.enotdir(store, A, O1, timedelta(hours=1))
                self.append("[info] account changed", timedelta(minutes=30))
                self.assertIsNone(heal_active.log_pair(self.paths()))
                hits = heal_active.recent_enotdir(self.paths())
                self.assertEqual([(s, p) for s, p, _l in hits], [(store, o1)])


if __name__ == "__main__":
    unittest.main()
