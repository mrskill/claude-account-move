"""ORACLE for round 3 of CAM-ADOPT-ORPHANS: the closure list K1-K7 of the judge after
round 2 (Zorkoslav, openai gpt-6.1-sol high, head 1a7cbcc), file
_Deliverables/task-outputs/move-program/review-adopt/r2/CLOSURE.md.

At least one test per item. Every test is RED on 1a7cbcc for the reason its
docstring names. Fake HOME only. An orphan id here (v5(4xx)) has no card
anywhere (build_source gives cards to uid(11..14) only).

Assumptions left open (the judge may tighten them)
  * K1: the swap between the check and the open is simulated through the module
    attribute `auto_move.real_pair_problem` (the last check of `active_pair`).
    An implementation that no longer calls it needs the hook moved, not the
    ruling weakened; with no call the test passes only if nothing lands in
    the foreign pair, which is the property.
  * K4: the app start is simulated through `common.claude_app_running` flipping
    to true after `adopt_orphans.find` returned (K4a) and after the first
    `os.link` of a card (K4b).
  * K6: the dry run is expected to print the file name of every unreadable card.
  * K7: the foreign file is placed at the name returned by
    `adopt_orphans._tmp_name`; undo is expected to name an unprovable temp file
    in its output (the line "ambiguous, left in place: <path>").
  * Known conflicts with the round 2 oracle, for the judge to rule (settled by an
    oracle commit, not by the implementer):
      - test_adopt_r2.F2LaunchBoundary.test_f2_finish_ignores_a_loaded_line_older_than_the_launch
        keeps "no loss claim" for "old Loaded, new launch, plain mention"; K3
        makes that case (iii), where the loss claim of the last-mention pair stays.
      - test_adopt_r2.F8TempFiles.test_f8_undo_removes_leftover_temps_of_its_operation
        writes the leftover with a NEW inode and expects undo to remove it; K7
        needs the recorded inode and sha256 of the temp file to match.
"""
import json
import os
import shutil
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from fixture import CC, LAM, uid, v5  # noqa: E402
from test_cli import A, B, O1, O2, build_source  # noqa: E402
import test_adopt_a9_app_pair as a9  # noqa: E402
import test_adopt_orphans as ao  # noqa: E402
from test_adopt_orphans import (all_cards_naming, cards,  # noqa: E402
                                find_journal, make_template, state_files)
from test_adopt_r2 import inproc, loaded, plain  # noqa: E402

from claude_account_move import adopt_orphans, auto_move  # noqa: E402

EXIT_FAIL = 3
PHRASE_BY_MENTION = "pair not seen in the app log, judged by the last mention"
ORG_Y = uid(21)            # the pair the app loaded for account B
ACC_C = a9.ACC_C           # another account whose pair is loaded later
ORG_C = a9.ORG_C
ORG_ALIAS = uid(22)        # a symlinked pair of account B


def no_loss_claims(test, ids, text):
    """Nothing in a finish report claims a loss about the target pair, the
    lagging directories or the scheduled tasks."""
    for cid, chk in ids.items():
        if cid.endswith(":target_names") or cid.endswith(":lagging") or cid == "tasks":
            test.assertTrue(chk["ok"], "a loss is claimed by check %s: %s" % (cid, chk))
    test.assertNotRegex(text, r"names missing in the target pair")
    test.assertNotRegex(text, r"cannot be delivered")


# --- K1: one open of the pair, account guard on every path ------------------------

class K1PairOpenedOnce(ao.Base):
    def test_k1_a_pair_swapped_between_the_check_and_the_open_is_not_followed(self):
        """Red on 1a7cbcc: active_pair checks the pair path, open_pair then resolves
        realpath a second time; a pair swapped for a link to another account's
        pair in between is opened and the cards are written there."""
        h = self.h
        build_source(h)
        d_a = ao.active_dir(h)
        make_template(h, d_a)
        d_c = h.pair(CC, ACC_C, ORG_C)
        make_template(h, d_c)
        h.log_file("main.log", ["2026-11-22 09:59:00 info: app starting",
                                loaded(h, CC, A, O1, n=4)])
        sid = v5(401)
        h.orphan(sid, cwd="/work/k1", prompts=["k1 prompt"])
        before_c = cards(d_c)
        real_check = auto_move.real_pair_problem
        swapped = []

        def check_then_swap(paths, store, pair):
            res = real_check(paths, store, pair)
            if not swapped and store == CC and pair == (A, O1):
                swapped.append(1)
                os.rename(d_a, d_a + ".moved")
                os.symlink(d_c, d_a)          # now a link to account C's pair
            return res
        code, out = inproc(h, "adopt", "--apply",
                           patches=[(auto_move, "real_pair_problem", check_then_swap)])
        self.assertEqual(cards(d_c), before_c,
                         "nothing may be written into another account's pair "
                         "(the pair is opened once, with every path component "
                         "checked by lstat): " + out)
        self.assertEqual([c for c in all_cards_naming(h, sid)
                          if os.path.dirname(c) == d_c], [], out)

    def test_k1_an_explicit_pair_with_the_app_on_another_account_refuses(self):
        """Red on 1a7cbcc: the app/CLI account mismatch check sits in the `else` of
        --active-pair; with CLI = B, app = A and --active-pair B/O2 the cards are
        written."""
        h = self.h
        build_source(h)
        d_b = h.pair(CC, B, O2)
        make_template(h, d_b)
        h.accounts(cli=B, app=A, org=O2)
        h.log_file("main.log", ["2026-11-22 09:59:00 info: app starting",
                                loaded(h, CC, A, O1, n=4)])
        sid = v5(402)
        h.orphan(sid, cwd="/work/k1", prompts=["k1 explicit"])
        before = cards(d_b)
        r = h.run("adopt", "--apply", "--active-pair", "%s/%s" % (B, O2))
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, EXIT_FAIL,
                         "the app is on account A, the CLI on B: refused even "
                         "with --active-pair: " + out)
        self.assertEqual(all_cards_naming(h, sid), [], "no write at all: " + out)
        self.assertEqual(cards(d_b), before)


# --- K2: one chooser of the pair (latest_loaded_pair) for verify-start too --------

class K2OneChooserEverywhere(a9.FinishBase):
    # Ruling K2' (CLOSURE.md): ONE chooser with C5 semantics. The latest Loaded
    # line of the store after the latest launch line, of ANY account, is the
    # pair when its account is the CLI account; otherwise there is no pair.

    def two_accounts_loaded(self, cli_last):
        """Log after the launch line. cli_last False: the app loaded B/ORG_Y,
        then another account's pair C/ORG_C (no pair). cli_last True: the app
        loaded C/ORG_C, then B/ORG_Y (the pair is B/ORG_Y)."""
        h = self.h
        self.d_y = h.pair(CC, B, ORG_Y)
        self.d_c = h.pair(CC, ACC_C, ORG_C)
        ly = loaded(h, CC, B, ORG_Y, n=3, ts="2026-11-22 10:00:00")
        lc = loaded(h, CC, ACC_C, ORG_C, n=2, ts="2026-11-22 10:01:00")
        if cli_last:
            ly = loaded(h, CC, B, ORG_Y, n=3, ts="2026-11-22 10:01:00")
            lc = loaded(h, CC, ACC_C, ORG_C, n=2, ts="2026-11-22 10:00:00")
            lines = [lc, ly]
        else:
            lines = [ly, lc]
        h.log_file("main.log", ["2026-11-22 09:59:00 info: app starting"] + lines)

    def verify_start(self, cli_last):
        # build the world FIRST: build_source rewrites main.log (mode "w")
        build_source(self.h)
        self.h.accounts(cli=B, app=B, org=O2)
        self.h.pair(CC, B, O2)           # the active organization folder exists
        self.two_accounts_loaded(cli_last=cli_last)
        return self.h.run("verify-start", "--wait-start", "2", "--json",
                          CLAUDE_ACCOUNT_MOVE_PGREP=ao.STUB_OPEN)

    def adopt_world(self, sid):
        h = self.h
        build_source(h)
        make_template(h, ao.active_dir(h))
        h.accounts(cli=B, app=B, org=O2)
        return sid

    def orphan_for(self, n):
        sid = v5(n)
        self.h.orphan(sid, cwd="/work/k2", prompts=["k2 prompt %d" % n])
        return sid

    # -- negative: another account loaded last: no pair anywhere --------------

    def test_k2_verify_start_other_account_last_is_app_not_started(self):
        """Red on 3b1f1ca: verify-start picks B/ORG_Y out of a store whose latest
        load is C/ORG_C (a hidden earlier load of the CLI account wins). By K2'
        the app now runs account C: app_not_started, exit 3."""
        r = self.verify_start(cli_last=False)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, EXIT_FAIL, out)
        self.assertEqual(json.loads(r.stdout).get("verdict"), "app_not_started", out)

    def test_k2_adopt_apply_other_account_last_refuses_with_no_write(self):
        """Red on 3b1f1ca: adopt writes into the earlier B/ORG_Y. By K2' there is
        no pair: exit 3, no card anywhere."""
        h = self.h
        self.adopt_world(None)
        self.two_accounts_loaded(cli_last=False)
        make_template(h, self.d_y)
        make_template(h, self.d_c)
        sid = self.orphan_for(403)
        before_y, before_c = cards(self.d_y), cards(self.d_c)
        r = h.run("adopt", "--apply")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, EXIT_FAIL, out)
        self.assertEqual(all_cards_naming(h, sid), [], "no write at all: " + out)
        self.assertEqual(cards(self.d_y), before_y, out)
        self.assertEqual(cards(self.d_c), before_c, out)

    def test_k2_sync_apply_other_account_last_refuses_with_no_copy(self):
        """Red on 3b1f1ca: sync copies into the earlier B/ORG_Y. By K2' there is
        no pair: exit 3, nothing copied into any pair."""
        h = self.h
        src = a9.FinishBase.moved_world(self, with_sync_to_loaded=False)
        self.assertTrue(os.path.isdir(src))
        self.two_accounts_loaded(cli_last=False)
        before = (cards(self.d_y), cards(self.d_c), cards(self.d_o2))
        r = h.run("sync", "--apply")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, EXIT_FAIL, out)
        self.assertEqual((cards(self.d_y), cards(self.d_c), cards(self.d_o2)),
                         before, "no copy anywhere: " + out)

    def test_k2_finish_other_account_last_is_case_ii_no_loss_claim(self):
        """Red on 3b1f1ca: finish judges the earlier B pair and claims the names
        or tasks missing in it. By K2' it is case (ii): the phrase "pair not
        seen in the app log", no loss claim, the move is not proven."""
        a9.FinishBase.moved_world(self, with_sync_to_loaded=False)
        self.two_accounts_loaded(cli_last=False)
        r, ids = self.finish_json()
        t = self.h.run("finish", "--wait-login", "0")
        text = t.stdout + t.stderr
        self.assertIn(a9.PHRASE_NOT_SEEN, text)
        self.assertNotIn(PHRASE_BY_MENTION, text,
                         "case (ii), not (iii): " + text)
        no_loss_claims(self, ids, r.stdout + r.stderr)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)

    # -- mirror positive: the CLI account loaded last: all four act on B/ORG_Y --

    def test_k2_verify_start_cli_account_last_acts_on_its_pair(self):
        """Mirror: log C/ORG_C then B/ORG_Y: verify-start ok on B/ORG_Y."""
        r = self.verify_start(cli_last=True)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        rep = json.loads(r.stdout)
        self.assertIn("%s/%s/%s" % (CC, B, ORG_Y), rep.get("loads") or [], out)
        self.assertFalse([x for x in rep.get("loads") or [] if ACC_C in x], out)

    def test_k2_adopt_cli_account_last_writes_into_its_pair(self):
        """Mirror: adopt acts on B/ORG_Y, never on C/ORG_C."""
        h = self.h
        self.adopt_world(None)
        self.two_accounts_loaded(cli_last=True)
        make_template(h, self.d_y)
        make_template(h, self.d_c)
        sid = self.orphan_for(404)
        before_c = cards(self.d_c)
        r = h.run("adopt", "--apply")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(cards(self.d_c), before_c, out)
        self.assertEqual(len([c for c in all_cards_naming(h, sid)
                              if os.path.dirname(c) == self.d_y]), 1, out)

    def test_k2_sync_cli_account_last_copies_into_its_pair(self):
        """Mirror: sync copies into B/ORG_Y only."""
        h = self.h
        a9.FinishBase.moved_world(self, with_sync_to_loaded=False)
        self.two_accounts_loaded(cli_last=True)
        before_c = cards(self.d_c)
        r = h.run("sync", "--apply")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(cards(self.d_c), before_c, out)
        self.assertTrue(cards(self.d_y), "cards copied into B/ORG_Y: " + out)

    def test_k2_finish_cli_account_last_judges_its_pair(self):
        """Mirror: finish judges B/ORG_X (holds every card and the tasks) loaded
        after C/ORG_C: green, no case (ii) phrase."""
        h = self.h
        a9.FinishBase.moved_world(self, with_sync_to_loaded=True)  # B/ORG_X full
        h.pair(CC, ACC_C, ORG_C)
        h.log_file("main.log", [
            "2026-11-22 09:59:00 info: app starting",
            loaded(h, CC, ACC_C, ORG_C, n=2, ts="2026-11-22 10:00:00"),
            loaded(h, CC, B, a9.ORG_X, n=3, ts="2026-11-22 10:01:00"),
            loaded(h, LAM, B, O2, n=1, ts="2026-11-22 10:01:00"),
        ])
        r, ids = self.finish_json()
        out = r.stdout + r.stderr
        self.assertTrue(ids[CC + ":target_names"]["ok"], ids[CC + ":target_names"])
        self.assertEqual(r.returncode, 0, out)
        t = h.run("finish", "--wait-login", "0")
        self.assertNotIn(a9.PHRASE_NOT_SEEN, t.stdout + t.stderr)


# --- K3: the case is decided only by events after the latest launch line ----------

class K3CaseAfterLaunch(a9.FinishBase):
    def test_k3_old_loaded_new_launch_plain_mention_is_case_iii(self):
        """Red on 1a7cbcc: any_root_load reads Loaded lines BEFORE the latest launch
        line, so "old Loaded, new `app starting`, plain mention" is judged as case
        (ii): the phrase "judged by the last mention" is never printed and no
        loss claim is made. By R2-C it is case (iii): the phrase on its own line,
        the last-mention pair (empty) judged as before, the loss claim stays."""
        self.moved_world(with_sync_to_loaded=False)      # B/O2 holds nothing
        h = self.h
        h.log_file("main.log", [
            loaded(h, CC, B, a9.ORG_X, n=3, ts="2026-11-22 08:00:00"),   # old process
            "2026-11-22 09:59:00 info: app starting",                    # new launch
            plain(CC, B, O2),
        ])
        r, ids = self.finish_json()
        t = h.run("finish", "--wait-login", "0")
        text = t.stdout + t.stderr
        self.assertTrue([ln for ln in text.splitlines() if PHRASE_BY_MENTION in ln],
                        "the case (iii) phrase is missing (the old Loaded line "
                        "decided the case): " + text)
        chk = ids.get(CC + ":target_names")
        self.assertIsNotNone(chk, "target_names is judged on the last-mention pair: "
                             "%s" % sorted(ids))
        self.assertFalse(chk["ok"], "the loss claim stays in case (iii): %s" % chk)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_k3_case_ii_claims_no_loss_even_when_the_target_has_no_dirs(self):
        """Red on 1a7cbcc: in case (ii) (a Loaded line after the launch names only
        another account) the suppression of the loss claim depends on the
        target directory existing; with no target directory for claude-code-
        sessions finish still claims the names "cannot be delivered" and the
        missing tasks."""
        src = self.moved_world(with_sync_to_loaded=False)
        self.assertTrue(os.path.isdir(src))
        os.rmdir(self.d_o2)                              # the target pair is gone
        h = self.h
        h.log_file("main.log", [plain(CC, A, O1, ts="2026-11-22 08:00:00"),
                                "2026-11-22 09:59:00 info: app starting",
                                loaded(h, CC, A, O1, n=3),
                                plain(CC, B, O2)])
        r, ids = self.finish_json()
        t = h.run("finish", "--wait-login", "0")
        text = t.stdout + t.stderr
        self.assertIn(a9.PHRASE_NOT_SEEN, text)
        no_loss_claims(self, ids, r.stdout + r.stderr)
        self.assertFalse(ids.get("app_pair_seen", {"ok": False})["ok"],
                         "the move is not proven: app_pair_seen fails: %s"
                         % sorted(ids))


# --- K4: the app check is repeated right before each card write -------------------

class K4AppCheckBeforeEachWrite(ao.Base):
    def test_k4_an_app_started_after_the_scan_stops_the_run_before_the_first_write(self):
        """Red on 1a7cbcc: the app is checked at the lock and before the scan only;
        an app that starts after the scan does not stop the write."""
        self.world()
        sids = [v5(411), v5(412)]
        for s in sids:
            self.h.orphan(s, cwd="/work/k4", prompts=["k4 " + s])
        started = [False]
        real_find = adopt_orphans.find

        def find_then_start(paths, index):
            res = real_find(paths, index)
            started[0] = True                  # the app starts right after the scan
            return res
        before = cards(self.d)
        code, out = inproc(self.h, "adopt", "--apply",
                           app_running=lambda: started[0],
                           patches=[(adopt_orphans, "find", find_then_start)])
        self.assertTrue(started[0], "the oracle hook ran: " + out)
        self.assertNotEqual(code, 0, out)
        self.assertNotEqual(code, ao.PARSE_ERROR, out)
        self.assertEqual(cards(self.d), before,
                         "the app is running before the first write: 0 cards "
                         "written: " + out)
        for s in sids:
            self.assertEqual(all_cards_naming(self.h, s), [])

    def test_k4_an_app_started_after_the_first_card_stops_before_the_second(self):
        """Red on 1a7cbcc: the check is not repeated between cards, so both cards
        are written although the app started after the first one."""
        self.world()
        sids = [v5(413), v5(414)]
        for s in sids:
            self.h.orphan(s, cwd="/work/k4", prompts=["k4 " + s])
        started = [False]
        real_link = os.link

        def link_then_start(*a, **kw):
            res = real_link(*a, **kw)
            started[0] = True                  # the app starts after a card landed
            return res
        before = cards(self.d)
        code, out = inproc(self.h, "adopt", "--apply",
                           app_running=lambda: started[0],
                           patches=[(os, "link", link_then_start)])
        self.assertTrue(started[0], "the oracle hook ran: " + out)
        self.assertNotEqual(code, 0, out)
        self.assertNotEqual(code, ao.PARSE_ERROR, out)
        new = sorted(set(cards(self.d)) - set(before))
        self.assertEqual(len(new), 1,
                         "exactly one card written, the run stops before the "
                         "second write: %s %s" % (new, out))


# --- K5: tasks of the loaded pair only (A9 b) --------------------------------------

class K5TasksOfTheLoadedPairOnly(a9.FinishBase):
    def test_k5_tasks_that_lie_in_another_org_do_not_hide_the_missing_ones(self):
        """Red on 1a7cbcc: compare_tasks unions the tasks of every directory of the
        target account (here a symlinked pair B/%s that holds them) with the
        loaded pair B/%s, so tasks missing in the loaded pair are not found and
        the check `tasks` is ok.""" % (ORG_ALIAS, ORG_Y)
        src = self.moved_world(with_sync_to_loaded=True)   # B/ORG_X holds all + tasks
        h = self.h
        os.symlink(self.d_x, os.path.join(h.support, CC, B, ORG_ALIAS))
        d_y = h.pair(CC, B, ORG_Y)                         # the pair the app loaded
        for n in os.listdir(src):
            if n.startswith("local_"):
                shutil.copy2(os.path.join(src, n), os.path.join(d_y, n))
        self.assertFalse(os.path.exists(os.path.join(d_y, "scheduled-tasks.json")))
        h.log_file("main.log", [
            plain(CC, A, O1, ts="2026-11-22 08:00:00"),
            "2026-11-22 09:59:00 info: app starting",
            loaded(h, CC, B, ORG_Y, n=3),
            loaded(h, LAM, B, O2, n=1),
        ])
        r, ids = self.finish_json()
        out = r.stdout + r.stderr
        self.assertIn("tasks", ids, out)
        self.assertFalse(ids["tasks"]["ok"],
                         "the scheduled tasks are missing in the loaded pair "
                         "B/%s (they exist only in another org): %s"
                         % (ORG_Y, ids["tasks"]))
        self.assertNotEqual(r.returncode, 0, out)


# --- K6: an unreadable card makes apply refuse ------------------------------------

class K6UnreadableCard(ao.Base):
    BAD = "local_%s.json" % uid(61, 9)

    def bad_card(self):
        with open(os.path.join(self.d, self.BAD), "w") as fh:
            fh.write("{ this is not json")

    def test_k6_apply_refuses_when_a_card_is_unreadable(self):
        """Red on 1a7cbcc: an unreadable card is left out of the index, adopt prints
        a warning and goes on, so a session whose card is the unreadable one gets
        a second card."""
        self.world()
        self.bad_card()
        sid = v5(421)
        self.h.orphan(sid, cwd="/work/k6", prompts=["k6 prompt"])
        before = cards(self.d)
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, EXIT_FAIL,
                         "apply refuses on an incomplete index, like sync: " + out)
        self.assertEqual(cards(self.d), before, "no write at all: " + out)
        self.assertEqual(all_cards_naming(self.h, sid), [])

    def test_k6_the_dry_run_lists_the_unreadable_card(self):
        """Red on 1a7cbcc: the dry run only counts the unreadable cards in a
        warning, it does not name them."""
        self.world()
        self.bad_card()
        self.h.orphan(v5(422), cwd="/work/k6", prompts=["k6 dry"])
        before = ao.tree_digest(self.h.root)
        r, out = self.run_adopt()
        self.assertEqual(r.returncode, 0, out)
        self.assertIn(self.BAD, out, "the dry run names the unreadable card: " + out)
        self.assertEqual(ao.tree_digest(self.h.root), before)


# --- K7: a temp file is removed only when this run created it ---------------------

class K7TempOwnership(ao.Base):
    def test_k7_an_existing_temp_name_is_refused_and_the_foreign_file_survives(self):
        """Red on 1a7cbcc: the O_EXCL open fails on the foreign file, then the
        `finally` removes it although this run never created it."""
        self.world()
        sid = v5(431)
        self.h.orphan(sid, cwd="/work/k7", prompts=["k7 prompt"])
        foreign = os.path.join(self.d, adopt_orphans.TMP_PREFIX + "foreign-owner")
        with open(foreign, "wb") as fh:
            fh.write(b"someone else's data")
        called = []

        def fixed_name(pair_dir, n):
            called.append(n)
            return foreign
        code, out = inproc(self.h, "adopt", "--apply",
                           patches=[(adopt_orphans, "_tmp_name", fixed_name)])
        self.assertTrue(called, "the oracle hook ran (the temp name is chosen by "
                        "adopt_orphans._tmp_name): " + out)
        self.assertTrue(os.path.exists(foreign),
                        "a temp file this run did not create is never removed: " + out)
        with open(foreign, "rb") as fh:
            self.assertEqual(fh.read(), b"someone else's data")

    def test_k7_undo_removes_a_genuine_leftover_the_journal_recorded(self):
        """Positive pin (green on 1a7cbcc, must stay green after K7): a write of
        this operation fails after its temp file was created (chmod fails) and
        the temp file cannot be removed either (a crash is simulated by making
        the unlink of temp names fail). The journal recorded that temp file
        (sha256 and inode); undo removes it."""
        self.world()
        sid = v5(433)
        self.h.orphan(sid, cwd="/work/k7", prompts=["k7 leftover"])
        real_chmod, real_unlink = os.chmod, os.unlink
        hit = []

        def chmod(p, *a, **kw):
            if os.path.basename(str(p)).startswith(adopt_orphans.TMP_PREFIX):
                hit.append(str(p))
                raise OSError(5, "injected write failure")
            return real_chmod(p, *a, **kw)

        def unlink(p, *a, **kw):
            if os.path.basename(str(p)).startswith(adopt_orphans.TMP_PREFIX):
                raise OSError(5, "injected: the process died before the cleanup")
            return real_unlink(p, *a, **kw)
        before = state_files(self.h)
        code, out = inproc(self.h, "adopt", "--apply",
                           patches=[(os, "chmod", chmod), (os, "unlink", unlink)])
        self.assertTrue(hit, "the failure was injected on a temp file: " + out)
        left = sorted(f for f in os.listdir(self.d)
                      if f.startswith(adopt_orphans.TMP_PREFIX))
        self.assertEqual(len(left), 1, "a real leftover stays in the pair: " + out)
        self.assertEqual(all_cards_naming(self.h, sid), [])
        journal = find_journal(self.h, out, before)
        self.assertIsNotNone(journal, out)
        r2, out2 = self.run_adopt("--undo", journal)
        self.assertFalse(os.path.lexists(os.path.join(self.d, left[0])),
                         "undo removes the leftover of its own operation that the "
                         "journal recorded (sha256 and inode): " + out2)

    def test_k7_undo_keeps_a_foreign_file_that_has_the_temp_prefix(self):
        """Red on 1a7cbcc: undo removes any regular file at the recorded temp path
        by its prefix, with no check of the sha256 and the inode; it must keep it
        and report it as ambiguous."""
        self.world()
        sid = v5(432)
        self.h.orphan(sid, cwd="/work/k7", prompts=["k7 undo"])
        before = state_files(self.h)
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, 0, out)
        journal = find_journal(self.h, out, before)
        self.assertIsNotNone(journal, out)
        with open(journal) as fh:
            recs = [json.loads(ln) for ln in fh if ln.strip()]
        tmp = [x["tmp"] for x in recs if x.get("op") == "intent"][0]
        self.assertFalse(os.path.lexists(tmp), "the temp file of a finished write is gone")
        with open(tmp, "wb") as fh:           # a file of someone else, same prefix
            fh.write(b"not ours, not the recorded bytes")
        r2, out2 = self.run_adopt("--undo", journal)
        self.assertTrue(os.path.exists(tmp),
                        "undo removes a temp file only when the journal recorded "
                        "its sha256 and inode and both match: " + out2)
        self.assertIn(tmp, out2, "the unproven temp file is reported as ambiguous: "
                      + out2)


if __name__ == "__main__":
    unittest.main()
