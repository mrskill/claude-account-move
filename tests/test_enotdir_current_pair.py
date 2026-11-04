"""ORACLE (round 2): `prepare` counts an ENOTDIR line unless the app has since
re-rooted its store onto another pair.

Defect measured live on 02.11.2026. `prepare` failed on "active pair writable"
(pre_move_check.check_enotdir -> heal_active.recent_enotdir) because of ENOTDIR
lines from 01.11 19:16 about pair <A>/d9bbd39a, whose local-agent-mode-sessions
folder is a symlink to <A>/252e7efa. The app has since reloaded its stores onto
<A>/252e7efa, so the old failure is stale; healing d9bbd39a would turn the real
pair into a symlink (ping-pong).

What the real main.log shows (measured 02.11.2026): the app logs NO successful
saves. It logs failures and, at store start, ONE line per store root:
  [info] Loaded 176 persisted sessions from <home>/Library/Application Support/
         Claude/local-agent-mode-sessions/<acc>/<org>
  [info] Loaded 1812 persisted sessions from <home>/.../claude-code-sessions/
         <acc>/<org> (200 archived deferred, 1331 ms)
The root it loads is the root it then writes cards into.

Rule pinned here (judge ruling after the Codex review of d01b696, which found that
a read of pair B suppressed a write failure on A, that success in one store
suppressed a failure in the other, and that log_pair() and the hit scan read the
log twice):

 R1. A hit is a timestamped main.log line of the last 2 days containing ENOTDIR
     and naming <store>/<acc>/<org> whose pair path is a symlink now.
 R2. (round 3, replaces the round-2 wording) A hit on pair P in store S is
     cleared only when the LAST store-root load in S that comes after the hit
     names a pair Q != P (account or org differ, compared lower-case). If the
     last later root load in S names P, or there is none, the hit counts.
     Loads of the other store never matter. Each hit is judged on its own: with
     two hits P1 and P2 in S and a last later load of P2, P2 counts, P1 is cleared.
 R3. A store-root load is a timestamped line with no enotdir/fail/error
     (case-insensitive) containing `loaded` and `sessions from`
     (case-insensitive), where the character right after the org UUID of the
     matched pair path is not `/` (end of line, space, quote, `(` are fine).
 R4. Nothing else clears a hit: not a card read, not a `Saved session` line, not
     a root load in the OTHER store, not `account changed`, not a failed load, not
     a load of a deeper path.
 R5. A root load naming P itself does not clear P, and it undoes an earlier
     clearing load: ENOTDIR(P) -> Loaded(Q) -> Loaded(P) counts (the writer came
     back to P, found by Codex r2 on 86be622); ... -> Loaded(Q) clears again. An
     ENOTDIR on P after a clearing root load is a new hit and counts.
 R6. One snapshot: recent_enotdir reads the log once per call
     (heal_active._log_text called once) and no longer calls log_pair.
 R7. Signature: recent_enotdir(paths, now=None, max_age_s=ENOTDIR_MAX_AGE_S);
     the `current` parameter of d01b696 is gone. Return shape: [(store, path, line)].
 Unchanged: the 2-day window, the symlink condition, non-store paths ignored,
 prepare exit 3 with the symlink path in stdout on a counted hit, exit 0 and
 `ready: true` otherwise.

Every case runs for both stores unless it is inherently two-store; each goes
through `prepare --json` and directly through `recent_enotdir`. Fake HOME only;
the real ~/Library is never touched.
"""
import inspect
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import test_heal_active as tha  # noqa: E402
from fixture import CC, LAM, Home, uid  # noqa: E402
from test_cli import A, B, O1, O2, build_source, parse  # noqa: E402

from claude_account_move import heal_active  # noqa: E402
from claude_account_move.common import Paths  # noqa: E402

STORES = (CC, LAM)
O_NOW = uid(0xab, 0xcd)     # Q: the pair the app loads now (real dir); has hex letters
O3 = uid(0x33, 3)           # a second symlinked pair, for the two-store case
SUFFIXES = ("", " (200 archived deferred, 1331 ms)", "'", '"', " ")


def stamp(age):
    return (datetime.now() - age).strftime("%Y-%m-%d %H:%M:%S")


def other(store):
    return LAM if store == CC else CC


class Enotdir(tha.Base):
    enotdir = tha.PrepareEnotdir.enotdir    # appends an ENOTDIR failure line, returns the path

    # -- fixtures -----------------------------------------------------------

    def fresh(self):
        """A new fake home: account A, org O1 with cards (build_source); the
        real pair Q = O_NOW in both stores."""
        self.tmp = tempfile.mkdtemp(prefix="cam-r2-")
        self.h = Home(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        build_source(self.h)
        for store in STORES:
            self.h.pair(store, A, O_NOW)

    def setUp(self):
        self.fresh()

    def alias(self, store):
        """The live shape: in `store`, O1 -> O_NOW (O_NOW real, holding the cards).
        Returns the symlink path of P = O1."""
        o1 = os.path.join(self.h.support, store, A, O1)
        now = os.path.join(self.h.support, store, A, O_NOW)
        shutil.rmtree(now, ignore_errors=True)
        os.rename(o1, now)
        os.symlink(now, o1)
        return o1

    def symlink_pair(self, store, org, acc=A, target=O_NOW):
        """pair `org` of `store` as a symlink to the real pair `target`."""
        p = os.path.join(self.h.support, store, acc, org)
        shutil.rmtree(p, ignore_errors=True)
        os.symlink(os.path.join(self.h.support, store, acc, target), p)
        return p

    def path(self, store, org, acc=A):
        return os.path.join(self.h.support, store, acc, org)

    def line(self, text, age=timedelta(minutes=5)):
        with open(self.h.home + "/Library/Logs/Claude/main.log", "a") as fh:
            fh.write("%s %s\n" % (stamp(age), text))

    def read_line(self, store, org, age=timedelta(minutes=5), acc=A):
        self.line("[info] Read session local_x from %s/local_x.json"
                  % self.path(store, org, acc), age)

    def save_line(self, store, org, age=timedelta(minutes=5), acc=A):
        self.line("[info] Saved session local_x to %s/local_x.json"
                  % self.path(store, org, acc), age)

    def load_line(self, store, org, age=timedelta(minutes=5), acc=A, suffix="",
                  upper=False):
        p = self.path(store, org, acc)
        if upper:
            p = p.replace(org, org.upper()).replace(acc, acc.upper())
        self.line("[info] Loaded 176 persisted sessions from %s%s" % (p, suffix), age)

    def paths(self):
        return Paths(self.h.home, self.h.state)

    def hits(self):
        return [(s, p) for s, p, _l in heal_active.recent_enotdir(self.paths())]

    def prepare(self):
        return self.h.run("prepare", "--json")

    def blocks(self, store, p):
        """Both views agree: the hit on symlink `p` of `store` counts."""
        self.assertEqual(self.hits(), [(store, p)])
        r = self.prepare()
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertFalse(parse(r)["ready"], r.stdout)
        self.assertIn("ENOTDIR", r.stdout)
        self.assertIn(p, r.stdout)

    def clear(self):
        """Both views agree: no hit counts."""
        self.assertEqual(self.hits(), [])
        r = self.prepare()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(parse(r)["ready"], r.stdout)

    def scene(self, store):
        """fresh home, P = O1 aliased to Q in `store`, ENOTDIR on P an hour ago."""
        self.fresh()
        p = self.alias(store)
        self.enotdir(store, A, O1, timedelta(hours=1))
        return p

    # -- N1 (Codex 1): a read or a save of Q is not a writer transition ------

    def test_n1_later_card_read_from_real_pair_still_blocks(self):
        for store in STORES:
            with self.subTest(store=store):
                p = self.scene(store)
                self.read_line(store, O_NOW)
                self.blocks(store, p)

    def test_n1_later_saved_session_on_real_pair_still_blocks(self):
        for store in STORES:
            with self.subTest(store=store):
                p = self.scene(store)
                self.save_line(store, O_NOW)
                self.blocks(store, p)

    # -- N2 (Codex 2): the evidence must come from the SAME store ------------

    def test_n2_root_load_only_in_the_other_store_still_blocks(self):
        for store in STORES:
            with self.subTest(store=store):
                p = self.scene(store)
                self.load_line(other(store), O_NOW)
                self.blocks(store, p)

    def test_n2_two_stores_different_pairs_only_the_uncleared_hit_is_returned(self):
        for lam_cleared in (False, True):
            with self.subTest(lam_cleared=lam_cleared):
                self.fresh()
                stale, live = (CC, LAM) if lam_cleared else (LAM, CC)
                # `live` keeps its hit: P = O1 symlink, no root load in `live`
                live_p = self.alias(live)
                self.enotdir(live, A, O1, timedelta(hours=1))
                # `stale` has its own symlinked pair O3, hit, then a root load of Q
                self.symlink_pair(stale, O3)
                self.enotdir(stale, A, O3, timedelta(minutes=50))
                self.load_line(stale, O_NOW, timedelta(minutes=5))
                self.assertEqual(self.hits(), [(live, live_p)])
                r = self.prepare()
                self.assertEqual(r.returncode, 3, r.stdout)
                self.assertFalse(parse(r)["ready"], r.stdout)
                self.assertIn(live_p, r.stdout)

    # -- N3 (Codex 3): one snapshot per call, no log_pair --------------------

    def test_n3_one_log_read_per_call_and_log_pair_is_not_called(self):
        for store in STORES:
            with self.subTest(store=store):
                p = self.scene(store)
                hit = ("%s [error] Failed to save session local_x: ENOTDIR: not a "
                       "directory, mkdir '%s' at ensureStorageDir\n"
                       % (stamp(timedelta(hours=1)), p))
                load = ("%s [info] Loaded 176 persisted sessions from %s\n"
                        % (stamp(timedelta(minutes=5)), self.path(store, O_NOW)))
                again = ("%s [error] Failed to save session local_x: ENOTDIR: not a "
                         "directory, mkdir '%s' at ensureStorageDir\n"
                         % (stamp(timedelta(minutes=1)), p))
                snap1 = hit + load
                snap2 = snap1 + again
                calls = []

                def fake_log_text(_paths):
                    calls.append(1)
                    return snap1 if len(calls) == 1 else snap2

                with mock.patch.object(heal_active, "_log_text",
                                       side_effect=fake_log_text) as lt, \
                        mock.patch.object(heal_active, "log_pair",
                                          side_effect=AssertionError(
                                              "recent_enotdir must not call log_pair")):
                    hits = heal_active.recent_enotdir(self.paths())
                    self.assertEqual(lt.call_count, 1)
                self.assertEqual(hits, [])             # snapshot 1: hit cleared
                # a second call is a new snapshot (here: snapshot 2): the new hit counts
                with mock.patch.object(heal_active, "_log_text",
                                       return_value=snap2) as lt:
                    hits = heal_active.recent_enotdir(self.paths())
                    self.assertEqual(lt.call_count, 1)
                self.assertEqual([(s, q) for s, q, _l in hits], [(store, p)])

    # -- R7: signature -------------------------------------------------------

    def test_r7_signature_has_no_current_parameter(self):
        sig = inspect.signature(heal_active.recent_enotdir)
        self.assertEqual(list(sig.parameters), ["paths", "now", "max_age_s"])
        self.assertIsNone(sig.parameters["now"].default)
        self.assertEqual(sig.parameters["max_age_s"].default,
                         heal_active.ENOTDIR_MAX_AGE_S)

    def test_r7_return_shape_is_store_path_line(self):
        for store in STORES:
            with self.subTest(store=store):
                p = self.scene(store)
                hits = heal_active.recent_enotdir(self.paths())
                self.assertEqual(len(hits), 1)
                s, q, ln = hits[0]
                self.assertEqual((s, q), (store, p))
                self.assertIn("ENOTDIR", ln)

    # -- N4: account changed is not a root load ------------------------------

    def test_n4_account_changed_after_the_hit_still_blocks(self):
        for store in STORES:
            with self.subTest(store=store):
                p = self.scene(store)
                self.line("[info] account changed: signed in as someone else",
                          timedelta(minutes=30))
                self.blocks(store, p)

    def test_n4_account_changed_after_a_root_load_before_the_hit_still_blocks(self):
        for store in STORES:
            with self.subTest(store=store):
                self.fresh()
                p = self.alias(store)
                self.load_line(store, O_NOW, timedelta(hours=2))   # before the hit
                self.enotdir(store, A, O1, timedelta(hours=1))
                self.line("[info] account changed", timedelta(minutes=30))
                self.blocks(store, p)

    # -- N5: R5 --------------------------------------------------------------

    def test_n5_root_load_of_the_same_pair_does_not_clear(self):
        for store in STORES:
            with self.subTest(store=store):
                p = self.scene(store)
                self.load_line(store, O1)
                self.blocks(store, p)

    def test_n5_new_enotdir_after_a_clearing_root_load_counts(self):
        for store in STORES:
            with self.subTest(store=store):
                p = self.scene(store)
                self.load_line(store, O_NOW, timedelta(minutes=30))
                self.enotdir(store, A, O1, timedelta(minutes=10))
                self.blocks(store, p)

    # -- N6: R4 / R3 ---------------------------------------------------------

    def test_n6_failed_load_of_another_pair_does_not_clear(self):
        for store in STORES:
            with self.subTest(store=store):
                p = self.scene(store)
                self.line("[error] Failed to load sessions from %s"
                          % self.path(store, O_NOW))
                self.blocks(store, p)

    def test_n6_load_of_a_deeper_path_does_not_clear(self):
        for store in STORES:
            with self.subTest(store=store):
                p = self.scene(store)
                self.line("[info] Loaded 3 sessions from %s/sub"
                          % self.path(store, O_NOW))
                self.blocks(store, p)

    def test_n6_load_line_with_error_or_enotdir_or_fail_does_not_clear(self):
        for store in STORES:
            with self.subTest(store=store):
                p = self.scene(store)
                for word in ("2 errors", "ENOTDIR", "fail"):
                    self.line("[info] Loaded 3 persisted sessions from %s (%s)"
                              % (self.path(store, O_NOW), word))
                self.blocks(store, p)

    def test_n6_untimestamped_load_line_does_not_clear(self):
        for store in STORES:
            with self.subTest(store=store):
                p = self.scene(store)
                with open(self.h.home + "/Library/Logs/Claude/main.log", "a") as fh:
                    fh.write("Loaded 176 persisted sessions from %s\n"
                             % self.path(store, O_NOW))
                self.blocks(store, p)

    # -- N8..N11 (round 3): only the LAST later root load in the store decides --

    def test_n8_load_q_then_load_p_again_blocks(self):
        for store in STORES:
            with self.subTest(store=store):
                p = self.scene(store)
                self.load_line(store, O_NOW, timedelta(minutes=30))
                self.load_line(store, O1, timedelta(minutes=10))
                self.blocks(store, p)

    def test_n9_load_q_load_p_load_q_clears(self):
        for store in STORES:
            with self.subTest(store=store):
                self.scene(store)
                self.load_line(store, O_NOW, timedelta(minutes=30))
                self.load_line(store, O1, timedelta(minutes=20))
                self.load_line(store, O_NOW, timedelta(minutes=10))
                self.clear()

    def test_n10_load_of_p_in_the_other_store_never_matters(self):
        for store in STORES:
            with self.subTest(store=store):
                self.scene(store)
                self.load_line(store, O_NOW, timedelta(minutes=30))
                self.load_line(other(store), O1, timedelta(minutes=10))
                self.clear()

    def test_n11_two_hits_last_load_names_one_of_them(self):
        for store in STORES:
            with self.subTest(store=store):
                self.fresh()
                self.alias(store)                              # P1 = O1 -> Q
                p2 = self.symlink_pair(store, O3)              # P2 = O3 -> Q
                self.enotdir(store, A, O1, timedelta(hours=1))
                self.enotdir(store, A, O3, timedelta(minutes=50))
                self.load_line(store, O3, timedelta(minutes=5))
                self.blocks(store, p2)                         # P1 cleared, P2 counts

    # -- N7: nothing later ---------------------------------------------------

    def test_n7_no_later_line_blocks(self):
        for store in STORES:
            with self.subTest(store=store):
                p = self.scene(store)
                self.blocks(store, p)

    def test_n7_root_load_BEFORE_the_hit_does_not_clear(self):
        for store in STORES:
            with self.subTest(store=store):
                self.fresh()
                p = self.alias(store)
                self.load_line(store, O_NOW, timedelta(hours=2))
                self.enotdir(store, A, O1, timedelta(hours=1))
                self.blocks(store, p)

    # -- P1: the live 01.11 case ---------------------------------------------

    def test_p1_root_load_of_the_real_pair_in_the_same_store_clears(self):
        for store in STORES:
            for suffix in SUFFIXES:
                with self.subTest(store=store, suffix=suffix):
                    self.scene(store)
                    self.load_line(store, O_NOW, suffix=suffix)
                    self.clear()

    def test_p1_root_load_clears_even_with_reads_and_saves_around_it(self):
        for store in STORES:
            with self.subTest(store=store):
                self.scene(store)
                self.read_line(store, O1, timedelta(minutes=30))
                self.load_line(store, O_NOW, timedelta(minutes=20))
                self.read_line(store, O_NOW, timedelta(minutes=10))
                self.clear()

    def test_p1_both_stores_loaded_clears_both_hits(self):
        self.fresh()
        for store in STORES:
            self.alias(store)
            self.enotdir(store, A, O1, timedelta(hours=1))
        for store in STORES:
            self.load_line(store, O_NOW, timedelta(minutes=5))
        self.clear()

    # -- P2: case-insensitive UUIDs -------------------------------------------

    def test_p2_mixed_case_uuids_in_the_load_line_still_clear(self):
        for store in STORES:
            with self.subTest(store=store):
                self.scene(store)
                self.load_line(store, O_NOW, upper=True)
                self.clear()

    def test_p2_mixed_case_load_of_the_same_pair_does_not_clear(self):
        letters = uid(0xef, 0xab)       # P with hex letters, a symlink to Q
        for store in STORES:
            with self.subTest(store=store):
                self.fresh()
                p = self.symlink_pair(store, letters)
                self.enotdir(store, A, letters, timedelta(hours=1))
                self.load_line(store, letters, upper=True)
                self.assertEqual(self.hits(), [(store, p)])

    # -- P3: a different account counts as a different pair -------------------

    def test_p3_root_load_of_another_account_in_the_same_store_clears(self):
        for store in STORES:
            with self.subTest(store=store):
                self.scene(store)
                self.h.pair(store, B, O2)
                self.load_line(store, O2, acc=B)
                self.assertEqual(self.hits(), [])

    # -- unchanged behaviour ----------------------------------------------------

    def test_unchanged_enotdir_older_than_two_days_is_ignored(self):
        for store in STORES:
            with self.subTest(store=store):
                self.fresh()
                self.alias(store)
                self.enotdir(store, A, O1, timedelta(days=3))
                self.clear()

    def test_unchanged_pair_that_is_a_real_directory_is_ignored(self):
        for store in STORES:
            with self.subTest(store=store):
                self.fresh()
                self.enotdir(store, A, O1, timedelta(hours=1))   # O1 stays real
                self.clear()

    def test_unchanged_path_outside_the_stores_is_ignored(self):
        self.fresh()
        self.alias(CC)
        self.line("[error] Failed to save: ENOTDIR: not a directory, mkdir '%s/%s/%s'"
                  % (os.path.join(self.h.support, "other-sessions"), A, O1),
                  timedelta(hours=1))
        self.clear()


if __name__ == "__main__":
    unittest.main()
