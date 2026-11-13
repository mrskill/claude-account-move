"""CAM-START-DETECT oracle round 3: one red test per Codex r1 finding F1-F6 on
head 9b38247 (verdict REWORK). ASCII only (Cyrillic as \\u escapes). Fixture Home
only; nothing real is read.

  F1 verify-start: the NEWEST load in main.log decides which account the app
     runs; a later load of another account is not ok (exit 3).
  F2 verify-start: app_running False is never ok, whatever the log still holds
     (verdict app_not_started, exit 3).
  F3 the pair check lstat-walks every component from the store root: a symlinked
     account dir or store dir is bad_pair; fresh_card_pair skips such a tree.
  F4 a card is a start signal only if its mtime CHANGED against a snapshot taken
     at call start (a future mtime that stays the same is not a start).
  F5 the second signal is read only in the active org pair of the CLI account;
     a touched card of an inactive org that the active org links to is no start.
  F6 APP_TIMEOUT emitted after the changes (repair branch) must neither say
     nothing changed nor advise to run the file again.
"""
import contextlib
import io
import json
import os
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fixture import CC, LAM  # noqa: E402
from fixture import uid  # noqa: E402
from test_cli import A, B, O1, O2, build_source  # noqa: E402
from test_auto import Base  # noqa: E402
import test_auto_poststart as tap  # noqa: E402
from test_start_detect import Say, append_log, loaded_line  # noqa: E402

from claude_account_move import auto_move, cli, common, heal_active  # noqa: E402
from claude_account_move import panel_snapshot as snap  # noqa: E402
from claude_account_move import ru_text as ru  # noqa: E402
from claude_account_move.common import Paths  # noqa: E402

NOTHING_CHANGED = u"\u043d\u0438\u0447\u0435\u0433\u043e \u043d\u0435 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u043e"
RUN_FILE_AGAIN = (u"\u0437\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u0435 \u044d\u0442\u043e\u0442 "
                  u"\u0444\u0430\u0439\u043b \u0435\u0449\u0435 \u0440\u0430\u0437")


class VerifyBase(Base):
    def finished_move(self):
        build_source(self.h)
        code, out, _, _ = self.run_auto()
        self.assertEqual(code, 0, out)
        names = sorted(os.listdir(snap.moves_dir(Paths(self.h.home, self.h.state))))
        self.mdir = os.path.join(snap.moves_dir(Paths(self.h.home, self.h.state)), names[-1])

    def run_verify(self, running=True, *extra):
        argv = ["verify-start", "--home", self.h.home, "--state-dir", self.h.state,
                "--json", "--wait-start", "0.5"] + list(extra)
        forbidden = mock.Mock(side_effect=AssertionError("verify-start must not call this"))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(common, "claude_app_running", lambda: running))
            es.enter_context(mock.patch.object(auto_move, "run_cli_login", forbidden))
            es.enter_context(mock.patch.object(auto_move, "wait_app_switch", forbidden))
            for n in ("cmd_prepare", "cmd_stamp", "cmd_sync", "cmd_heal_active"):
                es.enter_context(mock.patch.object(cli, n, forbidden))
            es.enter_context(contextlib.redirect_stdout(out))
            es.enter_context(contextlib.redirect_stderr(err))
            try:
                code = cli.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def verdict(self):
        with open(os.path.join(self.mdir, "report-verify-start.json")) as fh:
            return json.load(fh)


class F1F2VerifyStart(VerifyBase):
    def test_f1_a_later_load_of_another_account_is_not_ok(self):
        """CLI stays on B; the log holds a load of B, then a later load of A: the
        app runs A now. Old code filters the history by B and says ok."""
        self.finished_move()
        append_log(self.h, loaded_line(self.h, CC, acc=A, org=O1))
        code, out, err = self.run_verify(True)
        self.assertEqual(code, 3, "the newest load is another account: " + out + err)
        self.assertNotEqual(self.verdict()["verdict"], "ok")

    def test_f2_app_not_running_is_never_ok(self):
        """A Loaded line and the real pair remain after the app was closed.
        Old code ignores running in the decision: exit 0, ok, app_running false."""
        self.finished_move()
        code, out, err = self.run_verify(False)
        self.assertEqual(code, 3, "app closed: " + out + err)
        rep = self.verdict()
        self.assertEqual(rep["verdict"], "app_not_started")
        self.assertIs(rep.get("app_running"), False)


class F3SymlinkedParents(VerifyBase):
    def test_f3_symlinked_account_dir_is_bad_pair(self):
        """<store>/B is a link to another directory, the org below is real."""
        self.finished_move()
        acc_dir = os.path.join(self.h.support, CC, B)
        moved = acc_dir + ".real"
        os.rename(acc_dir, moved)
        os.symlink(moved, acc_dir)
        code, out, err = self.run_verify(True)
        self.assertEqual(code, 3, "a link above the org dir must be refused: " + out + err)
        self.assertEqual(self.verdict()["verdict"], "bad_pair")

    def test_f3_symlinked_store_dir_is_bad_pair(self):
        self.finished_move()
        store = os.path.join(self.h.support, CC)
        moved = store + ".real"
        os.rename(store, moved)
        os.symlink(moved, store)
        code, out, err = self.run_verify(True)
        self.assertEqual(code, 3, "a linked store root must be refused: " + out + err)
        self.assertEqual(self.verdict()["verdict"], "bad_pair")

    def test_f3_fresh_card_under_a_symlinked_account_dir_is_no_start(self):
        build_source(self.h)
        self.h.pair(CC, B, O2)
        self.h.accounts(cli=B, app=B)
        acc_dir = os.path.join(self.h.support, CC, B)
        old = time.time() - 7200
        for n in os.listdir(os.path.join(acc_dir, O2)):
            os.utime(os.path.join(acc_dir, O2, n), (old, old))
        moved = acc_dir + ".real"
        os.rename(acc_dir, moved)
        os.symlink(moved, acc_dir)
        paths = Paths(self.h.home, self.h.state)
        import threading

        def touch():
            time.sleep(0.1)
            self.h.card(os.path.join(moved, O2), 77, "cid-77")  # written during the wait
        t = threading.Thread(target=touch, daemon=True)
        t.start()
        res = auto_move.wait_app_start(paths, B, heal_active.log_mark(paths), 0.6, 0.02, Say())
        t.join(5)
        self.assertIsNone(res, "a card behind a symlinked account dir is not the new pair")


class F4F5CardSignal(Base):
    def setUp(self):
        Base.setUp(self)
        self.paths = Paths(self.h.home, self.h.state)

    def test_f4_an_unchanged_card_with_a_future_mtime_is_no_start(self):
        build_source(self.h)
        pair = self.h.pair(CC, B, O2)
        self.h.pair(LAM, B, O2)
        self.h.card(pair, 98, "cid-98")
        self.h.accounts(cli=B, app=B)
        future = time.time() + 3600          # another computer's clock; never changes
        for n in os.listdir(pair):
            os.utime(os.path.join(pair, n), (future, future))
        res = auto_move.wait_app_start(self.paths, B, heal_active.log_mark(self.paths),
                                       0.3, 0.02, Say())
        self.assertIsNone(res, "a card that did not change since the call began is no start")

    def test_f5_a_touched_card_of_an_inactive_org_is_no_start(self):
        """B has a real O_X and an active O2 that is a link to it. The app is
        silent; a background sync touches the card of O_X. The active O2 is not
        a real pair, so this is no start of the active org."""
        build_source(self.h)
        ox = uid(5)
        real = self.h.pair(CC, B, ox)
        self.h.card(real, 96, "cid-96")
        os.symlink(ox, os.path.join(self.h.support, CC, B, O2))
        os.makedirs(self.h.support, exist_ok=True)
        with open(os.path.join(self.h.home, ".claude.json"), "w") as fh:
            json.dump({"oauthAccount": {"accountUuid": B, "organizationUuid": O2}}, fh)
        with open(os.path.join(self.h.support, "config.json"), "w") as fh:
            json.dump({"lastKnownAccountUuid": B}, fh)
        old = time.time() - 7200
        for n in os.listdir(real):
            os.utime(os.path.join(real, n), (old, old))
        mark = heal_active.log_mark(self.paths)
        import threading

        def touch():
            time.sleep(0.1)
            self.h.card(real, 97, "cid-97")
        t = threading.Thread(target=touch, daemon=True)
        t.start()
        res = auto_move.wait_app_start(self.paths, B, mark, 0.6, 0.02, Say())
        t.join(5)
        self.assertIsNone(res, "the changed card is in an org that is not the active pair")


class F6TimeoutAfterChanges(tap.PostStart):
    def test_f6_app_that_does_not_close_for_the_repair_gets_no_rerun_advice(self):
        """The app loads a symlinked pair and never closes: the repair branch
        times out AFTER prepare, login and finish changed the data (auto_move.py
        APP_TIMEOUT at the second site, 'after finish'). The text must not say
        nothing changed and must not tell to run the file again."""
        build_source(self.h)
        self.login_hooks.append(self.symlinked_post_pair)
        with mock.patch.object(tap, "QUIT_AFTER_POLLS", 10 ** 9):
            code, out, err, _ = self.run_ps("--wait-close", "0.3")
        text = (out + err)
        self.assertEqual(code, 3, text)
        self.assertIn(ru.START_REPAIR.strip()[:20], text, "premise: the repair branch ran")
        low = text.lower().replace(u"\u0451", u"\u0435")
        self.assertNotIn(NOTHING_CHANGED, low, "data was changed before this stop")
        self.assertNotIn(RUN_FILE_AGAIN, low, "a rerun of the move is not the advice here")


if __name__ == "__main__":
    unittest.main()
