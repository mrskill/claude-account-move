import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fixture import CC, Home, tree_digest, uid  # noqa: E402
from test_cli import A, B, O1, O2  # noqa: E402

OPEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stub_pgrep_open.sh")
OLD, NEW = 1_750_000_000_000, 1_750_500_000_000      # last activity, ms


def read_bytes(p):
    with open(p, "rb") as fh:
        return fh.read()


class Repair(unittest.TestCase):
    """Source pair A/O1 holds the fresh cards, target pair B/O2 the stale ones."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-repair-")
        self.h = Home(self.tmp)
        self.src = self.h.pair(CC, A, O1)
        self.dst = self.h.pair(CC, B, O2)
        self.h.accounts(cli=B, app=B)
        self.h.log((A, O1), (B, O2))
        # 11: target bound to an older session, freshest copy has a transcript
        self.stale = self.make(11, 111, 211, OLD, NEW, title="Uralsib MVP")
        # 12: same, but the transcript of the freshest session is missing
        self.nohist = self.make(12, 112, 212, OLD, NEW, transcript=False)
        # 13: target already bound to the freshest session
        self.same = self.make(13, 113, 113, OLD, NEW)
        # 14: the target copy is the newer one
        self.newer = self.make(14, 114, 214, NEW, OLD)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, n, target_sid, source_sid, target_act, source_act,
             transcript=True, title="t"):
        self.h.card(self.dst, n, uid(target_sid), act=target_act, title=title)
        name = self.h.card(self.src, n, uid(source_sid), act=source_act, title=title)
        if transcript:
            self.h.transcript(uid(source_sid), last_ms=source_act)
        return name

    def path(self, d, name):
        return os.path.join(d, name)

    def journal(self):
        names = sorted(f for f in os.listdir(self.h.state) if f.startswith("repair-")
                       and f.endswith(".jsonl"))
        self.assertTrue(names)
        return os.path.join(self.h.state, names[-1])

    def test_dry_run_lists_exactly_the_stale_card(self):
        before = tree_digest(self.h.home)
        r = self.h.run("repair")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = [ln for ln in r.stdout.splitlines() if ln.startswith("rebind:")]
        self.assertEqual(len(lines), 1, r.stdout)
        self.assertIn(self.stale, lines[0])
        self.assertIn("Uralsib MVP", lines[0])
        self.assertIn("target %s -> %s" % (uid(111)[:8], uid(211)[:8]), lines[0])
        self.assertIn("(act ", lines[0])
        for other in (self.nohist, self.same, self.newer):
            self.assertNotIn(other, "\n".join(lines))
        self.assertIn("total: 1 card(s) to rebind", r.stdout)
        self.assertIn("dry run", r.stdout)
        self.assertEqual(tree_digest(self.h.home), before)    # nothing written

    def test_json_report_matches_the_dry_run(self):
        r = self.h.run("repair", "--json")
        rep = json.loads(r.stdout)
        self.assertEqual(rep["command"], "repair")
        self.assertEqual(rep["verdict"], "dry_run")
        self.assertEqual(rep["planned"], 1)
        self.assertEqual(rep["cards"][0]["name"], self.stale)
        self.assertEqual(rep["cards"][0]["new_session"], uid(211))

    def test_card_with_missing_freshest_transcript_is_skipped(self):
        r = self.h.run("repair")
        self.assertIn("skipped, transcript of the freshest copy is missing: "
                      + self.nohist, r.stdout)
        raw = read_bytes(self.path(self.dst, self.nohist))
        r = self.h.run("repair", "--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(read_bytes(self.path(self.dst, self.nohist)), raw)
        self.assertEqual(json.loads(raw)["cliSessionId"], uid(112))

    def test_apply_rebinds_and_undo_round_trips_the_bytes(self):
        originals = {n: read_bytes(self.path(self.dst, n))
                     for n in (self.stale, self.nohist, self.same, self.newer)}
        r = self.h.run("repair", "--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("replaced 1,", r.stdout)
        # the target card now is the freshest copy, byte for byte
        self.assertEqual(read_bytes(self.path(self.dst, self.stale)),
                         read_bytes(self.path(self.src, self.stale)))
        self.assertEqual(json.loads(read_bytes(self.path(self.dst, self.stale)))
                         ["cliSessionId"], uid(211))
        for n in (self.nohist, self.same, self.newer):         # untouched
            self.assertEqual(read_bytes(self.path(self.dst, n)), originals[n])
        self.assertEqual([f for f in os.listdir(self.dst) if f.startswith(".tmp")], [])
        baks = [f for f in os.listdir(self.h.state) if f.endswith(".bak")]
        self.assertEqual(len(baks), 1)
        self.assertEqual(read_bytes(os.path.join(self.h.state, baks[0])),
                         originals[self.stale])
        again = self.h.run("repair")                           # nothing left to do
        self.assertIn("total: 0 card(s) to rebind", again.stdout)
        r = self.h.run("repair", "--undo", self.journal())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("restored 1 cards", r.stdout)
        for n, raw in originals.items():
            self.assertEqual(read_bytes(self.path(self.dst, n)), raw, n)

    def test_undo_leaves_a_card_changed_since_the_apply(self):
        self.assertEqual(self.h.run("repair", "--apply").returncode, 0)
        target = self.path(self.dst, self.stale)
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
        # a dry run is still allowed while the app runs
        self.assertEqual(self.h.run("repair", CLAUDE_ACCOUNT_MOVE_PGREP=OPEN).returncode, 0)

    def test_tombstoned_card_is_left_alone(self):
        with open(os.path.join(self.dst, "deleted_" + self.stale[len("local_"):-len(".json")]),
                  "w") as fh:
            fh.write("")
        r = self.h.run("repair")
        self.assertIn("total: 0 card(s) to rebind", r.stdout)


if __name__ == "__main__":
    unittest.main()
