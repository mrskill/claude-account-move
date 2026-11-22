"""ORACLE for item A9 of CAM-ADOPT-ORPHANS: the pair the program judges and writes
into is the pair the APP loads (the latest `Loaded N persisted sessions from
<store>/<acc>/<org>` line of main.log after the app start), not the pair a later
plain mention of the log, heal or the CLI org names.

Contract: _Deliverables/task-outputs/move-program/adopt-orphans/CONTRACT.md, A9.
One test per part (a), (b), (c), (d). Fake HOME only.

Red on 6009713 means, per test:
  (a) adopt --apply writes the card into the pair of the LAST log mention (the
      plain `saved ... <acc>/<orgA>` line), not into the pair of the latest
      `Loaded` line, and prints no «pair used / source» statement.
  (b) finish judges target_names/tasks against the last-mentioned pair (empty)
      and fails, instead of the pair the app loaded (which holds every card).
  (c) with no `Loaded` line for the account, finish still claims «names missing
      in the target pair» and never says «pair not seen in the app log».
  (d) adopt prints no statement of the pair it used and its source (the account
      filter itself already holds on 6009713, so the card checks are green and
      the red is the missing statement; see the report of the hand).

Assumptions left open by the contract (the judge may tighten them): the exact
wording of the «pair used» statement (searched as a line holding the org id and
one of «app log», «main.log», «Loaded»); where finish prints the two pairs (any
output line holding both org ids); the exit code of finish in case (c) (only the
absence of the loss claim and the phrase are pinned).
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from fixture import CC, LAM, Home, uid, v5  # noqa: E402
from test_cli import A, B, O1, O2, build_source, parse, switch_to_target  # noqa: E402
from test_adopt_orphans import cards, card_for, make_template, active_dir  # noqa: E402
import test_tasks  # noqa: E402

ORG_A = O1                 # the pair heal / the plain log mention name
ORG_LOADED = uid(6)        # the pair the app actually loaded
ACC_C = uid(8)             # another account
ORG_C = uid(9)
ORG_X = uid(5)             # target account: the pair the app loaded after the move
PHRASE_NOT_SEEN = "pair not seen in the app log"
SOURCE_WORD = re.compile(r"(?i)app log|main\.log|loaded")


def loaded(h, store, acc, org, n=3, ts="2026-11-22 10:00:00"):
    return "%s info: Loaded %d persisted sessions from %s" % (
        ts, n, os.path.join(h.support, store, acc, org))


def plain(store, acc, org, ts="2026-11-22 10:05:00"):
    return "%s info: saved card path %s/%s/%s/local_x.json" % (ts, store, acc, org)


class AdoptBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-a9-")
        self.h = Home(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def adopt_world(self, lines_after_loaded=()):
        """Account A: pair A/O1 and pair A/ORG_LOADED, both with a template card;
        one orphan transcript. The app log (main.log) lists, in order: an older
        Loaded of A/O1, an app start line, the Loaded of A/ORG_LOADED, then the
        given lines."""
        h = self.h
        build_source(h)
        self.d_a = active_dir(h)
        make_template(h, self.d_a)
        self.d_l = h.pair(CC, A, ORG_LOADED)
        make_template(h, self.d_l)
        self.orphan = v5(77)
        h.orphan(self.orphan, cwd="/work/a9", prompts=["a9 first prompt"])
        h.log_file("main.log", [
            loaded(h, CC, A, O1, ts="2026-11-22 08:00:00"),
            "2026-11-22 09:59:00 info: app starting",
            loaded(h, CC, A, ORG_LOADED, n=4),
        ] + list(lines_after_loaded))

    def new_cards(self, d, before):
        return sorted(set(cards(d)) - set(before))


class A9aAdoptWritesIntoAppPair(AdoptBase):
    def test_a9a_apply_writes_into_the_loaded_pair_and_says_which_and_why(self):
        # a later plain mention of the OTHER org must not win over the Loaded line
        self.adopt_world(lines_after_loaded=[plain(CC, A, O1)])
        before_a, before_l = cards(self.d_a), cards(self.d_l)
        r = self.h.run("adopt", "--apply")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(self.new_cards(self.d_a, before_a), [],
                         "no card may land in the pair the app did not load: " + out)
        new = self.new_cards(self.d_l, before_l)
        self.assertEqual(len(new), 1,
                         "one card in the app-loaded pair %s: %s" % (ORG_LOADED, out))
        hit = card_for(self.d_l, self.orphan)
        self.assertIsNotNone(hit, out)
        said = [ln for ln in out.splitlines()
                if ORG_LOADED in ln and SOURCE_WORD.search(ln)]
        self.assertTrue(said, "output says which pair it used and from which source "
                        "(a line with the org id and the app log): " + out)


class FinishBase(AdoptBase):
    def moved_world(self, with_sync_to_loaded):
        """Source A with 3+1 cards and a scheduled-tasks file, prepared, then the
        move to account B (empty pairs B/O2). With `with_sync_to_loaded` every
        card and the tasks file also lie in B/ORG_X, the pair the app loaded."""
        h = self.h
        src = build_source(h)
        test_tasks.put(src, [test_tasks.task("t1"), test_tasks.task("t2")])
        assert h.run("prepare").returncode == 0
        switch_to_target(h)
        self.d_o2 = os.path.join(h.support, CC, B, O2)
        if with_sync_to_loaded:
            self.d_x = h.pair(CC, B, ORG_X)
            for n in os.listdir(src):
                shutil.copy2(os.path.join(src, n), os.path.join(self.d_x, n))
            lam_src = os.path.join(h.support, LAM, A, O1)
            lam_dst = os.path.join(h.support, LAM, B, O2)
            for n in os.listdir(lam_src):
                shutil.copy2(os.path.join(lam_src, n), os.path.join(lam_dst, n))
        return src

    def finish_json(self):
        r = self.h.run("finish", "--wait-login", "0", "--json")
        return r, {c["id"]: c for c in parse(r)["checks"]}


class A9bFinishJudgesTheLoadedPair(FinishBase):
    def test_a9b_target_names_lagging_tasks_judged_on_the_loaded_pair(self):
        self.moved_world(with_sync_to_loaded=True)
        h = self.h
        h.log_file("main.log", [
            plain(CC, A, O1, ts="2026-11-22 08:00:00"),
            "2026-11-22 09:59:00 info: app starting",
            loaded(h, CC, B, ORG_X, n=3),
            loaded(h, LAM, B, O2, n=1),
            plain(CC, B, O2),                    # a later mention of the empty pair
        ])
        r, ids = self.finish_json()
        out = r.stdout + r.stderr
        self.assertTrue(ids[CC + ":target_names"]["ok"],
                        "target_names judged on the loaded pair: %s" % ids[CC + ":target_names"])
        self.assertNotIn(CC + ":lagging", ids, out)
        self.assertTrue(ids["tasks"]["ok"], ids["tasks"])
        self.assertEqual(r.returncode, 0, out)
        # the text report prints both pairs when they differ
        t = h.run("finish", "--wait-login", "0")
        both = [ln for ln in (t.stdout + t.stderr).splitlines()
                if ORG_X in ln and O2 in ln]
        self.assertTrue(both, "a line names both pairs, the loaded %s and the "
                        "last-mentioned %s: %s" % (ORG_X, O2, t.stdout + t.stderr))


class A9cPairNotSeen(FinishBase):
    def test_a9c_no_loaded_line_says_pair_not_seen_and_claims_no_loss(self):
        self.moved_world(with_sync_to_loaded=False)   # target pair B/O2 is empty
        h = self.h
        # ruling R2-C case (ii): after the launch line a Loaded line names a pair
        # of ANOTHER account (A); no Loaded line names the CLI account (B)
        h.log_file("main.log", [plain(CC, A, O1, ts="2026-11-22 08:00:00"),
                                "2026-11-22 09:59:00 info: app starting",
                                loaded(h, CC, A, O1, n=3),
                                plain(CC, B, O2)])
        r, ids = self.finish_json()
        out = r.stdout + r.stderr
        claim = ids.get(CC + ":target_names")
        self.assertTrue(claim is None or claim["ok"],
                        "no loss claim about the target pair when the app log "
                        "does not name it: %s" % claim)
        t = h.run("finish", "--wait-login", "0")
        text = t.stdout + t.stderr
        self.assertIn(PHRASE_NOT_SEEN, text)
        self.assertNotRegex(text, r"names missing in the target pair")


class A9dNeverAnotherAccountsPair(AdoptBase):
    def test_a9d_other_accounts_loaded_pair_is_never_used(self):
        h = self.h
        self.adopt_world(lines_after_loaded=[])
        d_c = h.pair(CC, ACC_C, ORG_C)
        make_template(h, d_c)
        # the NEWEST Loaded line of the log names another account's pair
        with open(os.path.join(h.home, "Library", "Logs", "Claude", "main.log"),
                  "a") as fh:
            fh.write(loaded(h, CC, ACC_C, ORG_C, n=9, ts="2026-11-22 10:10:00") + "\n")
        before_c, before_a, before_l = cards(d_c), cards(self.d_a), cards(self.d_l)
        r = h.run("adopt", "--apply")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(self.new_cards(d_c, before_c), [],
                         "nothing is written into another account's pair: " + out)
        self.assertEqual(self.new_cards(self.d_a, before_a), [], out)
        self.assertEqual(len(self.new_cards(self.d_l, before_l)), 1,
                         "the card goes to the loaded pair of the account")
        said = [ln for ln in out.splitlines()
                if ORG_LOADED in ln and SOURCE_WORD.search(ln)]
        self.assertTrue(said, "output names the pair used (the account's own "
                        "loaded pair) and the source: " + out)
        self.assertFalse([ln for ln in out.splitlines()
                          if ORG_C in ln or ACC_C in ln],
                         "the other account's pair is never presented as used: " + out)


if __name__ == "__main__":
    unittest.main()
