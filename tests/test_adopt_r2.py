"""ORACLE for round 2 of CAM-ADOPT-ORPHANS: the judge rulings F1-F11 on round 1
(Zorkoslav, openai gpt-6.1-sol high, head 68b0d7a), file
_Deliverables/task-outputs/move-program/review-adopt/r1/RULINGS.md.

At least one test per row. Every test is RED on 68b0d7a for the reason its row
names (see the docstring of each test). Fake HOME only.

Conventions of this file
  * The app launch line of main.log is written as `info: app starting` (the same
    line the A9 oracle uses). A `Loaded N persisted sessions from <store>/<acc>/<org>`
    line BEFORE the latest launch line does not count (F2).
  * Orphans: every id called an orphan here has no card anywhere in the fixture
    (build_source gives cards to uid(11..14) only).

Assumptions left open (the judge may tighten them)
  * F6: the competing writer and the app start are simulated through the module
    attribute `cli.write_lock` (the lock is entered by `cmd_adopt`) and
    `common.claude_app_running`; an implementation that takes the lock by another
    name needs the test adapted, not the ruling weakened.
  * F7: the removal is observed through `os.unlink` / `os.remove`: a file that
    appears at the card's own name just before the removal must survive.
  * F8: the failure is injected through `os.chmod` on the temp file (the step the
    code takes between writing the temp file and linking it).
  * F11: the command is searched in the detail of the `sessions_no_card` check.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from fixture import CC, LAM, Home, iso, uid, v5  # noqa: E402
from test_cli import A, B, O1, O2, build_source, switch_to_target  # noqa: E402
import test_adopt_a9_app_pair as a9  # noqa: E402
import test_adopt_orphans as ao  # noqa: E402
from test_adopt_orphans import (all_cards_naming, card_for, cards,  # noqa: E402
                                find_journal, make_template, state_files)

from claude_account_move import adopt_orphans, cli, common  # noqa: E402

PHRASE_NOT_SEEN = "pair not seen in the app log"
EXIT_FAIL = 3
TMP_PREFIX = ".claude-account-move-adopt-"
ORG_Z = uid(10)            # a pair that is a symlink
ORG_X = a9.ORG_X           # the pair the app loaded after the move
ORG_LOADED = a9.ORG_LOADED


def loaded(h, store, acc, org, n=3, ts="2026-11-22 10:00:00"):
    return a9.loaded(h, store, acc, org, n=n, ts=ts)


def plain(store, acc, org, ts="2026-11-22 10:05:00"):
    return a9.plain(store, acc, org, ts=ts)


def inproc(h, *argv, app_running=lambda: False, patches=()):
    """cli.main in this process, stdout and stderr captured. `patches` is a list
    of (object, attribute, value). Returns (exit code, text)."""
    out = io.StringIO()
    with contextlib.ExitStack() as es:
        es.enter_context(mock.patch.object(common, "claude_app_running", app_running))
        for obj, attr, val in patches:
            es.enter_context(mock.patch.object(obj, attr, val))
        es.enter_context(contextlib.redirect_stdout(out))
        es.enter_context(contextlib.redirect_stderr(out))
        try:
            code = cli.main(list(argv) + ["--home", h.home, "--state-dir", h.state])
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue()


def no_loss_claims(test, ids, text):
    """Nothing in a finish report claims a loss about the target pair, the
    lagging directories or the scheduled tasks."""
    for cid, chk in ids.items():
        if cid.endswith(":target_names") or cid.endswith(":lagging") or cid == "tasks":
            test.assertTrue(chk["ok"], "a loss is claimed by check %s: %s" % (cid, chk))
    test.assertNotRegex(text, r"names missing in the target pair")


# --- F1: one chooser of the pair, heal_active.latest_loaded_pair -----------------

class F1OneChooser(a9.FinishBase):
    def test_f1_symlink_guard_judges_the_loaded_pair_not_the_last_mention(self):
        """Red on 68b0d7a: finish fails the check active_pair_real because
        _symlink_guard takes the LAST plain mention (a symlinked pair of another
        org) instead of the pair the app loaded."""
        self.moved_world(with_sync_to_loaded=True)
        h = self.h
        os.symlink(self.d_x, os.path.join(h.support, CC, B, ORG_Z))
        h.log_file("main.log", [
            plain(CC, A, O1, ts="2026-11-22 08:00:00"),
            "2026-11-22 09:59:00 info: app starting",
            loaded(h, CC, B, ORG_X, n=3),
            loaded(h, LAM, B, O2, n=1),
            plain(CC, B, ORG_Z),               # a later mention of a symlinked pair
        ])
        r, ids = self.finish_json()
        out = r.stdout + r.stderr
        bad = ids.get("active_pair_real")
        self.assertTrue(bad is None or bad["ok"],
                        "the symlink guard judged the last mention, not the pair "
                        "the app loaded: %s" % bad)
        self.assertEqual(r.returncode, 0, out)

    def test_f1_verify_start_accepts_the_pair_the_app_loaded(self):
        """Red on 68b0d7a: verify-start refuses before it reads the Loaded line
        because the directory of the CLI organization (O2) does not exist, though
        the app loaded another existing organization of the same account."""
        h = self.h
        build_source(h)
        h.pair(CC, B, ORG_X)
        h.accounts(cli=B, app=B, org=O2)             # CLI org O2: no such directory
        h.log_file("main.log", [
            "2026-11-22 09:59:00 info: app starting",
            loaded(h, CC, B, ORG_X, n=3, ts="2026-11-22 10:00:00"),
        ])
        self.assertFalse(os.path.lexists(os.path.join(h.support, CC, B, O2)))
        r = h.run("verify-start", "--wait-start", "3",
                  CLAUDE_ACCOUNT_MOVE_PGREP=ao.STUB_OPEN)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0,
                         "verify-start must judge the pair the app loaded (%s), "
                         "not the CLI organization: %s" % (ORG_X, out))

    def test_f1_auto_sync_delivers_into_the_loaded_pair(self):
        """Red on 68b0d7a: sync plans the copy into the pair of the LAST plain
        mention (O2), while the app loaded ORG_X."""
        self.moved_world(with_sync_to_loaded=False)
        h = self.h
        h.pair(CC, B, ORG_X)
        h.log_file("main.log", [
            plain(CC, A, O1, ts="2026-11-22 08:00:00"),
            "2026-11-22 09:59:00 info: app starting",
            loaded(h, CC, B, ORG_X, n=0),
            plain(CC, B, O2),                  # a later mention of the empty pair
        ])
        r = h.run("sync")
        out = r.stdout + r.stderr
        copies = [ln for ln in out.splitlines()
                  if "would copy" in ln and CC in ln]
        self.assertTrue(copies, "sync plans the copies of claude-code-sessions: " + out)
        self.assertTrue(all(os.path.join(CC, B, ORG_X) in ln for ln in copies),
                        "every claude-code-sessions copy goes into the loaded pair "
                        "%s: %s" % (ORG_X, "\n".join(copies)))
        self.assertFalse([ln for ln in copies if os.path.join(CC, B, O2) in ln],
                         "nothing is planned into the last-mentioned pair: " + out)


# --- F2: only events after the latest app launch line ----------------------------

class F2LaunchBoundary(a9.FinishBase):
    def test_f2_adopt_ignores_a_loaded_line_older_than_the_launch(self):
        """Red on 68b0d7a: adopt --apply writes the card into ORG_LOADED, named
        only by a Loaded line that is older than the latest `app starting` line."""
        self.adopt_world()
        h = self.h
        h.log_file("main.log", [
            loaded(h, CC, A, ORG_LOADED, n=4, ts="2026-11-22 08:00:00"),
            "2026-11-22 09:59:00 info: app starting",     # a new launch, no Loaded yet
            plain(CC, A, O1),
        ])
        before = cards(self.d_l)
        r = h.run("adopt", "--apply")
        out = r.stdout + r.stderr
        self.assertEqual(self.new_cards(self.d_l, before), [],
                         "a Loaded line older than the latest launch must not "
                         "decide the pair: " + out)
        self.assertFalse([ln for ln in out.splitlines()
                          if ORG_LOADED in ln and "oaded" in ln],
                         "the stale pair is never presented as the pair used: " + out)

    def test_f2_finish_ignores_a_loaded_line_older_than_the_launch(self):
        """Red on 68b0d7a: finish judges the pair of the stale Loaded line (it
        holds every card, so no problem is found) and never says the pair is not
        seen in the app log. Aligned to ruling K3 (round 3): with only a stale
        Loaded line before the newest launch, this is case (iii): the phrase
        "judged by the last mention" and the last-mention pair (empty) judged as
        before, so the loss claim stays."""
        self.moved_world(with_sync_to_loaded=True)
        h = self.h
        h.log_file("main.log", [
            loaded(h, CC, B, ORG_X, n=3, ts="2026-11-22 08:00:00"),
            loaded(h, LAM, B, O2, n=1, ts="2026-11-22 08:00:00"),
            "2026-11-22 09:59:00 info: app starting",
            plain(CC, B, O2),
        ])
        r, ids = self.finish_json()
        t = h.run("finish", "--wait-login", "0")
        text = t.stdout + t.stderr
        self.assertIn("pair not seen in the app log, judged by the last mention",
                      text)
        chk = ids.get(CC + ":target_names")
        self.assertIsNotNone(chk, sorted(ids))
        self.assertFalse(chk["ok"], "the stale Loaded line must not decide the "
                         "pair (it holds every card): %s" % chk)


# --- F3: A9(c) exactly: no Loaded line, no loss claim, whatever the pair holds ---

class F3NoLossWithoutLoaded(a9.FinishBase):
    def only_plain_mentions(self):
        # ruling R2-C case (ii): after the launch line a Loaded line names a pair
        # of ANOTHER account (A), none names the CLI account (B); the last plain
        # mention is the empty pair of B
        self.h.log_file("main.log", [plain(CC, A, O1, ts="2026-11-22 08:00:00"),
                                     "2026-11-22 09:59:00 info: app starting",
                                     loaded(self.h, CC, A, O1, n=3),
                                     plain(CC, B, O2)])

    def test_f3_partial_pair_claims_no_loss(self):
        """Red on 68b0d7a: a target pair holding SOME of the cards gets «names
        missing in the target pair», a lagging claim and a missing-tasks claim,
        though no Loaded line says it is the pair the app loads."""
        src = self.moved_world(with_sync_to_loaded=False)
        names = sorted(n for n in os.listdir(src) if n.startswith("local_"))
        self.assertGreaterEqual(len(names), 3, names)
        for n in names[:2]:
            shutil.copy2(os.path.join(src, n), os.path.join(self.d_o2, n))
        self.only_plain_mentions()
        r, ids = self.finish_json()
        t = self.h.run("finish", "--wait-login", "0")
        text = t.stdout + t.stderr
        no_loss_claims(self, ids, r.stdout + r.stderr)
        self.assertIn(PHRASE_NOT_SEEN, text)

    def test_f3_full_pair_still_says_pair_not_seen(self):
        """Red on 68b0d7a: a target pair holding EVERY card passes with no word
        about the missing Loaded line; the pair is not proven by being full."""
        self.moved_world(with_sync_to_loaded=False)
        r0 = self.h.run("sync", "--apply")
        self.assertEqual(r0.returncode, 0, r0.stdout + r0.stderr)
        self.only_plain_mentions()
        r, ids = self.finish_json()
        t = self.h.run("finish", "--wait-login", "0")
        text = t.stdout + t.stderr
        no_loss_claims(self, ids, r.stdout + r.stderr)
        self.assertIn(PHRASE_NOT_SEEN, text)
        self.assertNotEqual(r.returncode, 0,
                            "an unproven pair is not a proven move: " + r.stdout)


# --- R2-C case (iii): no Loaded line at all, the last mention is judged ----------

class F3IiiNoLoadedAtAll(a9.FinishBase):
    PHRASE_BY_MENTION = "pair not seen in the app log, judged by the last mention"

    def test_r2c_iii_plain_mentions_only_judged_by_last_mention_loss_stays(self):
        """Ruling R2-C (iii): an app log with plain mentions and no Loaded line at
        all. Finish prints the phrase on its own line and judges the pair of the
        last mention as before: that pair (B/O2) is empty, so the loss claim
        stays (target_names not ok, exit not 0)."""
        self.moved_world(with_sync_to_loaded=False)      # B/O2 holds nothing
        self.h.log_file("main.log", [plain(CC, A, O1, ts="2026-11-22 08:00:00"),
                                     plain(CC, B, O2)])
        r, ids = self.finish_json()
        t = self.h.run("finish", "--wait-login", "0")
        text = t.stdout + t.stderr
        own_line = [ln for ln in text.splitlines()
                    if self.PHRASE_BY_MENTION in ln]
        self.assertTrue(own_line, "the case (iii) phrase is missing: " + text)
        chk = ids.get(CC + ":target_names")
        self.assertIsNotNone(chk, "target_names is judged on the last-mention "
                             "pair: %s" % sorted(ids))
        self.assertFalse(chk["ok"], "the loss claim stays for an empty last-mention "
                         "pair: %s" % chk)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)


# --- F4: title ------------------------------------------------------------------

class F4Title(ao.Base):
    def adopt_one(self, sid):
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, 0, out)
        hit = card_for(self.d, sid)
        self.assertIsNotNone(hit, out)
        return hit[1]

    def test_f4_the_latest_title_line_of_either_kind_wins(self):
        """Red on 68b0d7a: a late ai-title loses to an earlier custom-title
        (custom is always preferred)."""
        self.world()
        s1, s2 = v5(141), v5(142)
        self.h.orphan(s1, prompts=["one"], titles=[("custom-title", "Early custom"),
                                                   ("ai-title", "Late AI title")])
        self.h.orphan(s2, prompts=["two"], titles=[("ai-title", "Early AI"),
                                                   ("custom-title", "Late custom")],
                      first_ms=1760100000000)
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(card_for(self.d, s1)[1]["title"], "Late AI title")
        self.assertEqual(card_for(self.d, s2)[1]["title"], "Late custom")

    def test_f4_a_ready_title_is_never_cut_only_the_prompt_fallback_is(self):
        """Red on 68b0d7a: a ready title of 90 characters is cut to 60."""
        self.world()
        ai, custom, fallback = v5(143), v5(144), v5(145)
        long_ai = "Ready AI title " + "w" * 75
        long_custom = "Ready custom title " + "c" * 71
        self.h.orphan(ai, prompts=["a"], titles=[("ai-title", long_ai)])
        self.h.orphan(custom, prompts=["b"], titles=[("custom-title", long_custom)],
                      first_ms=1760100000000)
        self.h.orphan(fallback, prompts=["P" * 100], first_ms=1760200000000)
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(card_for(self.d, ai)[1]["title"], long_ai)
        self.assertEqual(card_for(self.d, custom)[1]["title"], long_custom)
        self.assertEqual(card_for(self.d, fallback)[1]["title"], "P" * 60)


# --- F5: no readable timestamp: times from the transcript file mtime -------------

class F5NoTimestamp(ao.Base):
    MTIME = 1760500000                    # seconds, a whole second on purpose

    def orphan_without_time(self, sid):
        recs = [{"type": "user", "cwd": "/work/no-time", "sessionId": sid,
                 "message": {"role": "user", "content": "a prompt with no time"}},
                {"type": "assistant", "cwd": "/work/no-time", "sessionId": sid,
                 "message": {"role": "assistant",
                             "content": [{"type": "text", "text": "ok"}]}}]
        return self.h.orphan_raw(sid, recs, mtime=self.MTIME)

    def check(self, data):
        for key in ("createdAt", "lastActivityAt"):
            self.assertIs(type(data[key]), int, "%s is an int: %r" % (key, data[key]))
            self.assertEqual(data[key], self.MTIME * 1000,
                             "%s comes from the transcript file mtime in ms, never "
                             "from the template: %r" % (key, data[key]))

    def test_f5_times_come_from_the_file_mtime_not_the_template(self):
        """Red on 68b0d7a: createdAt and lastActivityAt keep the template's values."""
        self.world()
        sid = v5(151)
        self.orphan_without_time(sid)
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, 0, out)
        self.check(card_for(self.d, sid)[1])

    def test_f5_a_template_with_a_string_time_gives_int_times(self):
        """Red on 68b0d7a: the template's digit string createdAt is copied over
        (the template keeps an int lastActivityAt so that it stays the newest
        card of the pair and is the one picked)."""
        build_source(self.h)
        self.d = ao.active_dir(self.h)
        make_template(self.h, self.d, extra={"createdAt": "1700000000000"})
        sid = v5(152)
        self.orphan_without_time(sid)
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, 0, out)
        self.check(card_for(self.d, sid)[1])


# --- F6: scan and app check inside the write lock --------------------------------

class F6InsideTheLock(ao.Base):
    def competing_card(self, sid):
        name = "local_%s.json" % uid(98, 9)
        with open(os.path.join(self.d, name), "w") as fh:
            json.dump({"cliSessionId": sid, "lastActivityAt": 1760000000000,
                       "completedTurns": 1, "title": "written by the other apply",
                       "cwd": "/work/race"}, fh)

    def test_f6_a_second_apply_writes_nothing_twice(self):
        """Red on 68b0d7a: the orphan list is made before the lock; a card written
        by another apply meanwhile is not seen and a second card is created."""
        self.world()
        sid = v5(161)
        self.h.orphan(sid, cwd="/work/race", prompts=["race me"])
        real = cli.write_lock
        raced = []

        @contextlib.contextmanager
        def racing(paths):
            if not raced:
                raced.append(1)
                self.competing_card(sid)      # the other process finished first
            with real(paths):
                yield
        code, out = inproc(self.h, "adopt", "--apply",
                           patches=[(cli, "write_lock", racing)])
        self.assertTrue(raced, "the oracle hook ran: " + out)
        self.assertEqual(len(all_cards_naming(self.h, sid)), 1,
                         "the scan must be repeated inside the lock: " + out)

    def test_f6_an_app_started_before_the_write_is_refused(self):
        """Red on 68b0d7a: the app-running check happens once, before the scan; an
        app that is running when the lock is held does not stop the write."""
        self.world()
        sid = v5(162)
        self.h.orphan(sid, prompts=["app starts meanwhile"])
        real = cli.write_lock
        held = [False]

        @contextlib.contextmanager
        def locked(paths):
            with real(paths):
                held[0] = True
                try:
                    yield
                finally:
                    held[0] = False
        before = cards(self.d)
        code, out = inproc(self.h, "adopt", "--apply",
                           app_running=lambda: held[0],
                           patches=[(cli, "write_lock", locked)])
        self.assertNotEqual(code, 0, out)
        self.assertNotEqual(code, ao.PARSE_ERROR, out)
        self.assertEqual(cards(self.d), before,
                         "the app is running inside the lock: nothing is written: "
                         + out)
        self.assertEqual(all_cards_naming(self.h, sid), [])


# --- F7: undo removes a file only by an atomic check-and-remove -------------------

class F7AtomicUndo(ao.Base):
    def test_f7_a_file_that_appears_at_the_name_before_the_removal_survives(self):
        """Red on 68b0d7a: undo checks the file, then unlinks the PATH; a new file
        put at that name in between is deleted (check and removal are two steps)."""
        self.world()
        sid = v5(171)
        self.h.orphan(sid, prompts=["undo race"])
        before = state_files(self.h)
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, 0, out)
        journal = find_journal(self.h, out, before)
        self.assertIsNotNone(journal, out)
        name = card_for(self.d, sid)[0]
        path = os.path.join(self.d, name)
        foreign = b"FOREIGN-FILE-PUT-AT-THE-NAME-AFTER-THE-CHECK"
        real_unlink, real_remove = os.unlink, os.remove
        fired = []

        def swap_then(real):
            def wrapper(p, *a, **kw):
                inside = (os.path.dirname(os.path.realpath(str(p)))
                          == os.path.realpath(self.d))
                if inside and not fired:
                    fired.append(str(p))
                    tmp = path + ".foreign"
                    with open(tmp, "wb") as fh:
                        fh.write(foreign)
                    os.replace(tmp, path)     # a new file at the card's own name
                return real(p, *a, **kw)
            return wrapper
        code, out2 = inproc(self.h, "adopt", "--undo", journal,
                            patches=[(os, "unlink", swap_then(real_unlink)),
                                     (os, "remove", swap_then(real_remove))])
        self.assertTrue(fired, "undo removed a card of the pair: " + out2)
        self.assertTrue(os.path.exists(path),
                        "the file that appeared at the card's name after the check "
                        "was deleted by undo: " + out2)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), foreign)


# --- F8: temp files -------------------------------------------------------------

class F8TempFiles(ao.Base):
    def leftovers(self):
        return sorted(f for f in os.listdir(self.d) if f.startswith(TMP_PREFIX))

    def test_f8_a_failed_write_removes_its_temp_file(self):
        """Red on 68b0d7a: when the write fails after the temp file exists and
        before the link (here chmod), the temp file stays in the pair."""
        self.world()
        sid = v5(181)
        self.h.orphan(sid, prompts=["write fails"])
        real_chmod = os.chmod
        hit = []

        def chmod(p, *a, **kw):
            if os.path.basename(str(p)).startswith(TMP_PREFIX):
                hit.append(str(p))
                raise OSError(5, "injected write failure")
            return real_chmod(p, *a, **kw)
        code, out = inproc(self.h, "adopt", "--apply", patches=[(os, "chmod", chmod)])
        self.assertTrue(hit, "the failure was injected on a temp file: " + out)
        self.assertEqual(all_cards_naming(self.h, sid), [])
        self.assertEqual(self.leftovers(), [],
                         "a failed write leaves no temp file: " + out)

    def test_f8_undo_removes_leftover_temps_of_its_operation(self):
        """Red on 68b0d7a: undo looks only at the final card paths and leaves the
        temp file that a crashed write of the same operation left behind."""
        self.world()
        sid = v5(182)
        self.h.orphan(sid, prompts=["crash leftover"])
        pre = ao.tree_digest(self.d)
        before = state_files(self.h)
        r, out = self.run_adopt("--apply")
        self.assertEqual(r.returncode, 0, out)
        journal = find_journal(self.h, out, before)
        self.assertIsNotNone(journal, out)
        with open(journal) as fh:
            recs = [json.loads(ln) for ln in fh if ln.strip()]
        tmp = [x["tmp"] for x in recs if x.get("op") == "intent"][0]
        name = card_for(self.d, sid)[0]
        with open(os.path.join(self.d, name), "rb") as fh:
            data = fh.read()
        with open(tmp, "wb") as fh:           # same bytes, but a NEW inode
            fh.write(data)
        r2, out2 = self.run_adopt("--undo", journal)
        # aligned to ruling K7 (round 3): a leftover whose inode is not the one
        # the journal recorded is kept and reported ambiguous, never removed;
        # the genuine leftover is removed (test_adopt_r3, K7 positive test)
        self.assertNotEqual(r2.returncode, 0, out2)
        self.assertTrue(os.path.exists(tmp),
                        "a temp file with an unrecorded inode is kept: " + out2)
        self.assertIn(tmp, out2, "and reported as ambiguous: " + out2)
        self.assertEqual(card_for(self.d, sid), None, "the card itself is removed")


# --- F9: the account of the CLI decides, and the real path is used once -----------

class F9AccountGuard(a9.AdoptBase):
    def one_orphan(self, n):
        sid = v5(n)
        self.h.orphan(sid, cwd="/work/f9", prompts=["f9 prompt"])
        return sid

    def test_f9_app_account_differs_from_the_cli_account_refuses(self):
        """Red on 68b0d7a: with the CLI on account B and the app on account A,
        adopt --apply writes the cards into A's pair (the app's Loaded line)."""
        h = self.h
        build_source(h)
        d_a = ao.active_dir(h)
        make_template(h, d_a)
        h.accounts(cli=B, app=A, org=O2)
        h.log_file("main.log", ["2026-11-22 09:59:00 info: app starting",
                                loaded(h, CC, A, O1, n=4)])
        sid = self.one_orphan(191)
        before = cards(d_a)
        r = h.run("adopt", "--apply")
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, EXIT_FAIL, out)
        self.assertEqual(all_cards_naming(h, sid), [], "no write at all: " + out)
        self.assertEqual(cards(d_a), before)

    def test_f9_an_explicit_pair_of_another_account_is_refused(self):
        """Red on 68b0d7a: --active-pair is parsed and used without checking that
        it belongs to the CLI account."""
        h = self.h
        build_source(h)
        d_a = ao.active_dir(h)
        make_template(h, d_a)
        h.pair(CC, B, O2)
        h.accounts(cli=B, app=B, org=O2)
        h.log_file("main.log", ["2026-11-22 09:59:00 info: app starting",
                                loaded(h, CC, B, O2, n=0)])
        sid = self.one_orphan(192)
        before = cards(d_a)
        r = h.run("adopt", "--apply", "--active-pair", "%s/%s" % (A, O1))
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, EXIT_FAIL, out)
        self.assertEqual(all_cards_naming(h, sid), [], "no write at all: " + out)
        self.assertEqual(cards(d_a), before)

    def test_f9_the_pair_swapped_for_a_link_after_the_check_is_not_written_into(self):
        """Red on 68b0d7a: apply_plan resolves the pair path a second time, after
        the checks; a pair replaced by a symlink to another account's pair inside
        the support folder is accepted and the cards go there."""
        h = self.h
        build_source(h)
        d_a = ao.active_dir(h)
        make_template(h, d_a)
        d_c = h.pair(CC, a9.ACC_C, a9.ORG_C)
        make_template(h, d_c)
        h.log_file("main.log", ["2026-11-22 09:59:00 info: app starting",
                                loaded(h, CC, A, O1, n=4)])
        sid = self.one_orphan(193)
        before_c = cards(d_c)
        real_pick = adopt_orphans.pick_template

        def pick_then_swap(pair_dir):
            res = real_pick(pair_dir)
            os.rename(d_a, d_a + ".moved")
            os.symlink(d_c, d_a)              # the pair is now a link to account C
            return res
        code, out = inproc(h, "adopt", "--apply",
                           patches=[(adopt_orphans, "pick_template", pick_then_swap)])
        self.assertTrue(os.path.islink(d_a), "the oracle hook ran: " + out)
        self.assertEqual(cards(d_c), before_c,
                         "nothing may be written into another account's pair: " + out)
        self.assertEqual([c for c in all_cards_naming(h, sid)
                          if os.path.dirname(c) == d_c], [])


# --- F10: the session-owner line anywhere in the transcript ----------------------

class F10OwnerAnywhere(ao.Base):
    def test_f10_an_owner_line_after_the_first_64k_still_makes_an_orphan(self):
        """Red on 68b0d7a: only the first 64 KiB are searched, so a v4 session
        whose owner line comes later is not found by adopt."""
        self.world()
        sid = uid(201)                         # not a version-5 id: the owner line decides
        pad = "x" * 200000
        recs = [{"type": "user", "timestamp": iso(1760000000000), "cwd": "/work/late",
                 "sessionId": sid, "message": {"role": "user", "content": "late owner"}},
                {"type": "assistant", "timestamp": iso(1760000030000),
                 "message": {"role": "assistant",
                             "content": [{"type": "text", "text": pad}]}},
                {"type": "session-owner", "accountUuid": uid(1),
                 "organizationUuid": uid(2)}]
        self.h.orphan_raw(sid, recs)
        r, out = self.run_adopt()
        self.assertEqual(r.returncode, 0, out)
        self.assertIn(sid, out, "the orphan with a late owner line is listed: " + out)


# --- F11: the finish problem line names the exact command ------------------------

class F11ReturnCommand(ao.Base):
    def test_f11_finish_names_the_command_that_returns_the_orphans(self):
        """Red on 68b0d7a: the problem detail of the sessions-without-card
        check only counts the sessions and names no command."""
        build_source(self.h)
        for i in range(3):
            self.h.orphan(v5(210 + i), prompts=["lost %d" % i])
        self.assertEqual(self.h.run("prepare").returncode, 0)
        switch_to_target(self.h)
        self.assertEqual(self.h.run("sync", "--apply").returncode, 0)
        r = self.h.run("finish", "--wait-login", "3", "--json")
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        rep = json.loads(r.stdout)
        chk = [c for c in rep["checks"] if c["id"] == "sessions_no_card"]
        self.assertTrue(chk and not chk[0]["ok"], rep["checks"])
        cmd = "./claude-account-move adopt --apply --state-dir %s" % self.h.state
        self.assertIn(cmd, chk[0]["detail"],
                      "the problem line names the exact command: " + chk[0]["detail"])


if __name__ == "__main__":
    unittest.main()
