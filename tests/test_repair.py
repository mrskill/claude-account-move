import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fixture import CC, Home, tree_digest, uid  # noqa: E402
from test_cli import A, B, O1, O2  # noqa: E402

from claude_account_move import panel_snapshot as snap  # noqa: E402
from claude_account_move import repair_cards  # noqa: E402
from claude_account_move import store as st  # noqa: E402
from claude_account_move.common import Paths, read_identity  # noqa: E402
from claude_account_move.sync_cards import Journal  # noqa: E402

OPEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stub_pgrep_open.sh")
OLD, REC, NEWER = 1_750_000_000_000, 1_750_500_000_000, 1_750_900_000_000
LQ, RQ = "«", "»"


def read_bytes(p):
    with open(p, "rb") as fh:
        return fh.read()


class Repair(unittest.TestCase):
    """The snapshot records the source pair; the target pair is then wrong.

    11 foreign   target bound to ANOTHER session with the same title and a
                 fresher activity and history (the owner opened it by mistake)
    12 older     target bound to an older session
    13 nosrc     no copy with the recorded id exists any more
    14 shrunk    the recorded transcript is older than the snapshot recorded
    15 same      target already bound to the recorded session
    16 notr      the recorded transcript is gone
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-repair-")
        self.h = Home(self.tmp)
        self.src = self.h.pair(CC, A, O1)
        self.h.accounts(cli=A, app=A)
        self.h.log((A, O1))
        self.rec = {}
        for n in (11, 12, 13, 14, 15, 16):
            self.rec[n] = self.h.card(self.src, n, uid(n), act=OLD,
                                      title="Uralsib MVP")
            self.h.transcript(uid(n), last_ms=REC)
        paths = self.paths()
        index, tmap = st.scan(paths), st.transcripts(paths)
        before = snap.build_before(paths, index, tmap, read_identity(paths))
        self.move_id, _, _ = snap.write_snapshot(paths, before, True, index)
        # the move happens: the target pair holds the wrong bindings
        self.dst = self.h.pair(CC, B, O2)
        self.h.accounts(cli=B, app=B)
        self.h.log((A, O1), (B, O2))
        self.h.card(self.dst, 11, uid(311), act=NEWER, title="Uralsib MVP")
        self.h.transcript(uid(311), last_ms=NEWER)
        self.h.card(self.dst, 12, uid(312), act=OLD - 5000, title="Uralsib MVP")
        self.h.transcript(uid(312), last_ms=OLD - 5000)
        os.unlink(os.path.join(self.src, self.rec[13]))        # no source copy
        self.h.card(self.dst, 13, uid(313), act=OLD, title="Uralsib MVP")
        self.h.transcript(uid(14), last_ms=OLD)                # older than recorded
        self.h.card(self.dst, 14, uid(314), act=OLD, title="Uralsib MVP")
        self.h.card(self.dst, 15, uid(15), act=OLD, title="Uralsib MVP")
        os.unlink(os.path.join(self.h.projects, uid(16) + ".jsonl"))
        self.h.card(self.dst, 16, uid(316), act=OLD, title="Uralsib MVP")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def paths(self):
        return Paths(self.h.home, self.h.state)

    def raw(self, d, n):
        return read_bytes(os.path.join(d, self.rec[n]))

    def journal(self):
        names = sorted(f for f in os.listdir(self.h.state) if f.startswith("repair-")
                       and f.endswith(".jsonl"))
        self.assertTrue(names)
        return os.path.join(self.h.state, names[-1])

    def test_dry_run_lists_foreign_and_older_bound_cards_only(self):
        before = tree_digest(self.h.home)
        r = self.h.run("repair")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = [ln for ln in r.stdout.splitlines() if ln.startswith("rebind:")]
        self.assertEqual(len(lines), 2, r.stdout)
        self.assertEqual(
            lines[0].split(" (last")[0],
            "rebind: %s %sUralsib MVP%s target %s" % (self.rec[11], LQ, RQ,
                                                       uid(311)[:8]))
        self.assertIn("-> %s (last " % uid(11)[:8], lines[0])
        self.assertIn(self.rec[12], lines[1])
        self.assertIn("target %s" % uid(312)[:8], lines[1])
        text = "\n".join(lines)
        for n in (13, 14, 15, 16):
            self.assertNotIn(self.rec[n], text)
        self.assertIn("total: 2 card(s) to rebind, 3 skipped", r.stdout)
        self.assertEqual(tree_digest(self.h.home), before)     # nothing written

    def test_skipped_cards_print_their_reason(self):
        out = self.h.run("repair").stdout
        skipped = {ln.split()[1]: ln for ln in out.splitlines()
                   if ln.startswith("skipped:")}
        self.assertEqual(set(skipped), {self.rec[13], self.rec[14], self.rec[16]})
        self.assertIn("no source copy", skipped[self.rec[13]])
        self.assertIn("older than the snapshot", skipped[self.rec[14]])
        self.assertIn("missing or unreadable", skipped[self.rec[16]])

    def test_same_title_foreign_session_with_fresher_act_goes_back(self):
        self.assertEqual(self.h.run("repair", "--apply").returncode, 0)
        card = json.loads(read_bytes(os.path.join(self.dst, self.rec[11])))
        self.assertEqual(card["cliSessionId"], uid(11))        # not the fresher one
        self.assertEqual(card["lastActivityAt"], OLD)

    def test_apply_rebinds_and_undo_round_trips_the_bytes(self):
        originals = {n: self.raw(self.dst, n) for n in (11, 12, 13, 14, 15, 16)}
        r = self.h.run("repair", "--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("replaced 2,", r.stdout)
        for n in (11, 12):                       # byte for byte the recorded copy
            self.assertEqual(self.raw(self.dst, n), self.raw(self.src, n))
        for n in (13, 14, 15, 16):               # skipped or fine: untouched
            self.assertEqual(self.raw(self.dst, n), originals[n])
        self.assertEqual([f for f in os.listdir(self.dst) if f.startswith(".tmp")], [])
        baks = [f for f in os.listdir(self.h.state) if f.endswith(".bak")]
        self.assertEqual(len(baks), 2)
        self.assertIn("total: 0 card(s) to rebind", self.h.run("repair").stdout)
        r = self.h.run("repair", "--undo", self.journal())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("restored 2 cards", r.stdout)
        for n, raw in originals.items():
            self.assertEqual(self.raw(self.dst, n), raw, n)

    def test_ack_failure_after_replace_stays_undoable(self):
        paths = self.paths()
        index, tmap = st.scan(paths), st.transcripts(paths)
        before, _m, _i, problem = snap.load_baseline(paths)
        self.assertIsNone(problem)
        items, _sk = repair_cards.plan_repair(
            index, tmap, B, os.path.realpath(self.dst), before)
        items = [i for i in items if i["name"] == self.rec[11]]
        self.assertEqual(len(items), 1)
        original = self.raw(self.dst, 11)
        real_add = Journal.add

        def add(jr, **rec):
            if rec.get("op") == "done":
                raise OSError("disk full")
            return real_add(jr, **rec)

        os.makedirs(self.h.state, exist_ok=True)
        journal = repair_cards.new_journal_path(self.h.state)
        with mock.patch.object(Journal, "add", add):
            res = repair_cards.apply_repairs(items, journal, self.h.state,
                                             snap.path_id(self.h.home))
        self.assertEqual(res["replaced"], 1)
        self.assertEqual(res["unacknowledged"], 1)
        self.assertEqual(res["failed"], 0)
        self.assertEqual(self.raw(self.dst, 11), self.raw(self.src, 11))
        out = repair_cards.undo_repairs(journal, snap.path_id(self.h.home),
                                        os.path.realpath(self.h.support))
        self.assertEqual((out["restored"], out["skipped"], out["ambiguous"]), (1, 0, 0))
        self.assertEqual(self.raw(self.dst, 11), original)

    def test_undo_leaves_a_card_changed_since_the_apply(self):
        self.assertEqual(self.h.run("repair", "--apply").returncode, 0)
        target = os.path.join(self.dst, self.rec[11])
        with open(target, "ab") as fh:                         # someone wrote it
            fh.write(b" ")
        changed = read_bytes(target)
        r = self.h.run("repair", "--undo", self.journal())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ambiguous 1", r.stdout)
        self.assertEqual(read_bytes(target), changed)

    def test_apply_and_undo_refused_while_the_app_runs(self):
        self.assertEqual(self.h.run("repair", "--apply").returncode, 0)
        self.assertEqual(self.h.run("repair", "--undo", self.journal()).returncode, 0)
        before = tree_digest(self.h.home)
        r = self.h.run("repair", "--apply", CLAUDE_ACCOUNT_MOVE_PGREP=OPEN)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Quit the Claude app", r.stdout + r.stderr)
        r = self.h.run("repair", "--undo", self.journal(), CLAUDE_ACCOUNT_MOVE_PGREP=OPEN)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Quit the Claude app", r.stdout + r.stderr)
        self.assertEqual(tree_digest(self.h.home), before)
        self.assertEqual(self.h.run("repair", CLAUDE_ACCOUNT_MOVE_PGREP=OPEN).returncode, 0)

    def test_without_a_snapshot_nothing_is_planned(self):
        shutil.rmtree(os.path.join(self.h.state, "moves"))
        os.unlink(os.path.join(self.h.state, snap.POINTER))
        r = self.h.run("repair", "--apply")
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        self.assertIn("no usable 'before' snapshot", r.stdout)

    def test_json_report(self):
        rep = json.loads(self.h.run("repair", "--json").stdout)
        self.assertEqual((rep["command"], rep["verdict"], rep["planned"]),
                         ("repair", "dry_run", 2))
        self.assertEqual(rep["move_id"], self.move_id)
        self.assertEqual(rep["cards"][0]["recorded_session"], uid(11))
        self.assertEqual(sorted(s["reason"] for s in rep["skipped_cards"]),
                         ["no_source_copy", "no_transcript", "transcript_older"])

    def test_tombstoned_card_is_left_alone(self):
        open(os.path.join(self.dst, "deleted_" + self.rec[11][len("local_"):-5]),
             "w").close()
        self.assertIn("total: 1 card(s) to rebind", self.h.run("repair").stdout)


if __name__ == "__main__":
    unittest.main()
