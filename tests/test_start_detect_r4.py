"""CAM-START-DETECT oracle round 4: the judge's final contract C1-C6 after the
Codex r2 REWORK on 69f1843. ASCII only. Fixture Home only.

  C1 verify-start: app account (config.json) must equal the CLI account, else
     wrong_account / exit 3, even when the last Loaded line names the CLI pair.
  C2 verify-start: claude_app_running is polled again after every other check;
     ok needs True there (sequence True then False -> app_not_started).
  C3 only claude-code-sessions decides: old or foreign LAM lines are a note in
     missing_stores, never wrong_account; an old CC line of another account with
     no fresh card is app_not_started; a fresh card in the CLI pair is ok even
     when the latest CC line names another account (identity is the CLI's).
  C4 no fallback: the CLI org dir from .claude.json absent -> bad_pair with the
     reason "no active organization folder"; the lone other org is never used.
  C5 wait_app_start: per store only the LATEST Loaded line after the mark counts
     (no account filter); a start also needs the app running at decision time.
     (The poststart tests for the running condition are in test_auto_poststart.)
  C6 APP_TIMEOUT_AFTER lists: close, heal-active --apply, open, verify-start.
"""
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fixture import CC, LAM, uid  # noqa: E402
from test_cli import A, B, O1, O2, build_source  # noqa: E402
from test_start_detect import Say, append_log, loaded_line  # noqa: E402
from test_start_detect_r3 import VerifyBase  # noqa: E402

from claude_account_move import auto_move, common, heal_active  # noqa: E402
from claude_account_move import ru_text as ru  # noqa: E402
from claude_account_move.common import Paths  # noqa: E402


def age_cards(h, store=CC, acc=B, org=O2, secs=7200):
    pair = os.path.join(h.support, store, acc, org)
    old = time.time() - secs
    for n in os.listdir(pair):
        os.utime(os.path.join(pair, n), (old, old))


class C1C2C3VerifyStart(VerifyBase):
    def test_c1_app_on_another_account_is_wrong_account(self):
        """CLI on B, app config on A, the last historical load is B's."""
        self.finished_move()
        self.h.accounts(app=A)
        code, out, err = self.run_verify(True)
        self.assertEqual(code, 3, "app account != CLI account: " + out + err)
        self.assertEqual(self.verdict()["verdict"], "wrong_account")

    def test_c2_the_app_quits_after_the_other_checks(self):
        """running: True on the first poll, False afterwards -> not ok."""
        self.finished_move()
        class Seq(object):
            n = 0

            def __bool__(self):
                Seq.n += 1
                return Seq.n == 1
        code, out, err = self.run_verify(Seq())
        self.assertEqual(code, 3, "the final running poll was False: " + out + err)
        rep = self.verdict()
        self.assertEqual(rep["verdict"], "app_not_started")
        self.assertIs(rep.get("app_running"), False)

    def test_c3a_a_foreign_agent_store_line_is_a_note_not_wrong_account(self):
        """CC loaded B; the newest LAM line names A (lazy store, old line)."""
        self.finished_move()
        append_log(self.h, loaded_line(self.h, LAM, acc=A, org=O1))
        code, out, err = self.run_verify(True)
        self.assertEqual(code, 0, out + err)
        rep = self.verdict()
        self.assertEqual(rep["verdict"], "ok")
        self.assertIn(LAM, rep.get("missing_stores") or [])

    def test_c3b_an_old_cc_line_of_another_account_is_app_not_started(self):
        self.finished_move()
        append_log(self.h, loaded_line(self.h, CC, acc=A, org=O1))
        age_cards(self.h)
        code, out, err = self.run_verify(True)
        self.assertEqual(code, 3, out + err)
        self.assertEqual(self.verdict()["verdict"], "app_not_started")

    def test_c3c_a_fresh_card_in_the_cli_pair_is_ok_over_an_old_foreign_line(self):
        self.finished_move()
        append_log(self.h, loaded_line(self.h, CC, acc=A, org=O1))
        age_cards(self.h)
        pair = os.path.join(self.h.support, CC, B, O2)

        def touch():
            time.sleep(0.15)
            self.h.card(pair, 95, "cid-95")
        t = threading.Thread(target=touch, daemon=True)
        t.start()
        code, out, err = self.run_verify(True)
        t.join(5)
        self.assertEqual(code, 0, "identity is B, the CLI pair changed: " + out + err)
        self.assertEqual(self.verdict()["verdict"], "ok")


class C4NoFallback(VerifyBase):
    def cli_org_gone(self):
        with open(os.path.join(self.h.home, ".claude.json"), "w") as fh:
            json.dump({"oauthAccount": {"accountUuid": B,
                                        "organizationUuid": uid(0x99)}}, fh)

    def test_c4_verify_start_missing_cli_org_dir_is_bad_pair(self):
        self.finished_move()
        self.cli_org_gone()                 # O2 is the lone org dir, it must not be used
        code, out, err = self.run_verify(True)
        self.assertEqual(code, 3, out + err)
        self.assertEqual(self.verdict()["verdict"], "bad_pair")
        low = (out + err).lower().replace(u"\u0451", u"\u0435")
        self.assertIn(u"\u043d\u0435\u0442 \u043f\u0430\u043f\u043a\u0438 \u0430\u043a\u0442\u0438\u0432"
                      u"\u043d\u043e\u0439 \u043e\u0440\u0433\u0430\u043d\u0438\u0437\u0430\u0446\u0438\u0438", low)

    def test_c4_wait_app_start_never_watches_the_lone_other_org(self):
        build_source(self.h)
        self.h.pair(CC, B, O2)
        self.h.accounts(cli=B, app=B, org=uid(0x99))      # CLI org dir does not exist
        age_cards(self.h)
        paths = Paths(self.h.home, self.h.state)
        mark = heal_active.log_mark(paths)

        def touch():
            time.sleep(0.1)
            self.h.card(os.path.join(self.h.support, CC, B, O2), 94, "cid-94")
        t = threading.Thread(target=touch, daemon=True)
        t.start()
        with mock.patch.object(common, "claude_app_running", lambda: True):
            res = auto_move.wait_app_start(paths, B, mark, 0.6, 0.02, Say())
        t.join(5)
        self.assertIsNone(res, "a card of the lone other org is not the CLI pair")


class C5LatestLineWins(VerifyBase):
    def test_c5_after_the_mark_the_latest_load_counts_without_an_account_filter(self):
        build_source(self.h)
        self.h.pair(CC, B, O2)
        self.h.pair(LAM, B, O2)
        self.h.accounts(cli=B, app=B, org=O2)
        age_cards(self.h)
        paths = Paths(self.h.home, self.h.state)
        mark = heal_active.log_mark(paths)
        append_log(self.h, loaded_line(self.h, CC, acc=B, org=O2),
                   loaded_line(self.h, CC, acc=A, org=O1))
        with mock.patch.object(common, "claude_app_running", lambda: True):
            res = auto_move.wait_app_start(paths, B, mark, 0.3, 0.02, Say())
        self.assertIsNone(res, "Loaded B then Loaded A: the app runs A, not a start of B")


class C6RecoveryText(unittest.TestCase):
    def test_c6_steps_in_order_close_heal_open_verify(self):
        low = ru.APP_TIMEOUT_AFTER.lower().replace(u"\u0451", u"\u0435")
        close = low.find(u"\u0437\u0430\u043a\u0440\u043e\u0439\u0442\u0435")
        heal = low.find("heal-active --apply")
        opn = low.find(u"\u043e\u0442\u043a\u0440\u043e\u0439\u0442\u0435")
        ver = low.find("verify-start")
        self.assertTrue(0 <= close < heal < ver, "close, heal-active, verify-start: %r" % low)
        self.assertTrue(heal < opn < ver, "the open-Claude step must sit between heal-active "
                                          "and verify-start: %r" % low)


if __name__ == "__main__":
    unittest.main()
