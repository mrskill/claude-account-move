"""CAM-START-DETECT oracle (written before the fix, red on release 1002377).

Case 13.11.2026: `auto` ended `app_not_started` (exit 3) after a full, successful
move (login ok, heal ok, finish all ok); wait_app_start timed out because
heal_active.fresh_root_loads wanted a "Loaded N sessions from <store>/<acc>/<org>"
line for EVERY store after the log mark. 15 minutes later the app was running
under the new account and a read-only check was green. The final text told the
owner to run the move again, which is wrong: everything had moved.

Contract (names chosen here, the implementer must follow them):
  T1 wait_app_start returns the loads it has when the claude-code-sessions pair
     of the new account is loaded; a store without a Loaded line (local-agent-
     mode-sessions loads lazily) is named in a printed note, not a failure.
  T2 a second signal counts without any Loaded line: a card file in the new
     account's claude-code-sessions pair dir with an mtime after the wait began.
  T3 the auto report (--json) carries `app_running` (bool, from
     common.claude_app_running) and `missing_stores` (stores with no signal), and
     the reason text differs between "process not running" and "running, no
     signal for store X" (the second names the store).
  T4 app_not_started after a green finish never says "run the move again"; it
     gives `claude-account-move verify-start --state-dir <dir>` and says the data
     already moved (Russian "already moved" wording).
  T5 `verify-start --state-dir D [--wait-start N]`: no login, no prepare, no
     stamp, no sync; waits for start (a load already in main.log counts, the app
     may have started long ago), checks the active pair of the CLI account is a
     real dir (not a symlink), writes report-verify-start.json into the newest
     move dir, verdict "ok" and exit 0 on ok; exit 3 and a non-ok verdict else.
  T6 class guard: no user-facing string of the package tells the owner to run the
     move again.
Fake HOME only; nothing real is read."""
import ast
import contextlib
import json
import os
import re
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fixture import CC, LAM, OwnerApp  # noqa: E402
from test_cli import B, O2, build_source  # noqa: E402
from test_auto import Base  # noqa: E402  (its fixtures only)

from claude_account_move import auto_move, cli, common, heal_active  # noqa: E402
from claude_account_move import panel_snapshot as snap  # noqa: E402
from claude_account_move.common import Paths  # noqa: E402


class Say(object):
    def __init__(self):
        self.lines = []

    def say(self, text=""):
        self.lines.append(text)


def loaded_line(h, store, acc=B, org=O2):
    return "%s info: Loaded 3 persisted sessions from %s\n" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), os.path.join(h.support, store, acc, org))


def append_log(h, *lines):
    d = os.path.join(h.home, "Library", "Logs", "Claude")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "main.log"), "a") as fh:
        fh.write("".join(lines))


class WaitAppStart(Base):
    def setUp(self):
        Base.setUp(self)
        build_source(self.h)
        self.h.pair(CC, B, O2)
        self.h.pair(LAM, B, O2)
        self.h.accounts(cli=B, app=B)
        self.paths = Paths(self.h.home, self.h.state)

    def test_t1_cc_loaded_lam_silent_is_a_start_with_a_note(self):
        mark = heal_active.log_mark(self.paths)
        append_log(self.h, loaded_line(self.h, CC))        # no line for LAM
        out = Say()
        res = auto_move.wait_app_start(self.paths, B, mark, 0.5, 0.01, out)
        self.assertIsNotNone(
            res, "claude-code-sessions of the new account is loaded; a store that "
                 "has not loaded yet (local-agent-mode-sessions) must not fail the start")
        self.assertEqual(res.get(CC), (B, O2))
        self.assertNotIn(LAM, res, "no signal for the agent store: it is not in the loads")
        self.assertTrue([ln for ln in out.lines if LAM in ln],
                        "the store without a signal must be named in a note: %r" % out.lines)

    def test_t1b_only_the_agent_store_loaded_is_still_not_a_start(self):
        """Control: claude-code-sessions is the store that decides the start."""
        mark = heal_active.log_mark(self.paths)
        append_log(self.h, loaded_line(self.h, LAM))
        res = auto_move.wait_app_start(self.paths, B, mark, 0.2, 0.01, Say())
        self.assertIsNone(res)

    def test_t2_a_fresh_card_in_the_new_pair_is_a_start_signal(self):
        pair = os.path.join(self.h.support, CC, B, O2)
        old = time.time() - 7200
        for n in os.listdir(pair):
            os.utime(os.path.join(pair, n), (old, old))
        mark = heal_active.log_mark(self.paths)
        # control: only stale cards and no Loaded line: no start
        self.assertIsNone(auto_move.wait_app_start(self.paths, B, mark, 0.2, 0.01, Say()))
        # the app starts and rewrites a card (no Loaded line is ever logged)

        def touch():
            time.sleep(0.15)
            self.h.card(pair, 99, "cid-99")
        t = threading.Thread(target=touch, daemon=True)
        t.start()
        out = Say()
        res = auto_move.wait_app_start(self.paths, B, mark, 3.0, 0.02, out)
        t.join(5)
        self.assertIsNotNone(res, "a card with an mtime after the mark is a start signal")
        self.assertEqual(res.get(CC), (B, O2))


class TimeoutReport(Base):
    """T3 and T4: the app_not_started report and its final text."""

    def auto_json(self, app_started):
        build_source(self.h)

        def start(app):
            app.open = app_started
        with mock.patch.object(OwnerApp, "start", start), \
                mock.patch.object(OwnerApp, "QUIT_AFTER_POLLS", 10 ** 9):
            code, out, err, _ = self.run_auto("--json", "--wait-start", "0.4")
        return code, json.loads(out), err

    def test_t3_process_not_running_differs_from_running_without_signal(self):
        code_a, rep_a, _ = self.auto_json(app_started=False)
        self.assertEqual((code_a, rep_a["verdict"]), (3, "app_not_started"))
        self.tearDown_and_reset()
        code_b, rep_b, err_b = self.auto_json(app_started=True)
        self.assertEqual((code_b, rep_b["verdict"]), (3, "app_not_started"))
        self.assertIs(rep_a.get("app_running"), False,
                      "the report must say the app process is not running")
        self.assertIs(rep_b.get("app_running"), True,
                      "the report must say the app is running but gave no load signal")
        self.assertEqual(sorted(rep_b.get("missing_stores") or []), sorted([CC, LAM]))
        self.assertNotEqual(rep_a.get("reason"), rep_b.get("reason"),
                            "one text for two different causes")
        self.assertIn(CC, rep_b.get("reason", "") + err_b,
                      "running without a signal: name the store with no signal")

    def tearDown_and_reset(self):
        import shutil
        import tempfile
        from fixture import Home
        shutil.rmtree(self.tmp, True)
        self.tmp = tempfile.mkdtemp(prefix="cam-auto-")
        self.h = Home(self.tmp)

    def test_t4_after_a_green_finish_the_text_gives_verify_start_not_a_rerun(self):
        build_source(self.h)
        with mock.patch.object(OwnerApp, "start", lambda self: None):
            code, text, err, _ = self.run_auto("--wait-start", "0.4")
        text += err
        self.assertEqual(code, 3, text)
        rep = None
        for dp, _dn, fns in os.walk(os.path.join(self.h.state, "moves")):
            if "report-auto.json" in fns:
                with open(os.path.join(dp, "report-auto.json")) as fh:
                    rep = json.load(fh)
        self.assertIsNotNone(rep, "no report-auto.json written")
        steps = {s["step"]: s for s in rep["steps"]}
        self.assertTrue(steps["finish_before_start"]["ok"], "premise: the data moved")
        low = text.lower().replace(u"\u0451", u"\u0435")
        self.assertNotIn(u"\u0437\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u0435 "
                         u"\u043f\u0435\u0440\u0435\u043d\u043e\u0441 \u0435\u0449\u0435 \u0440\u0430\u0437",
                         low, "the move already finished: never tell to run it again")
        self.assertIn("claude-account-move verify-start --state-dir %s" % self.h.state, text)
        self.assertTrue(re.search(u"\u0443\u0436\u0435 \u043f\u0435\u0440\u0435\u043d\u0435\u0441", low),
                        "say plainly that the data already moved")


class VerifyStart(Base):
    """T5. A finished move first, then the read-only command."""

    def finished_move(self):
        build_source(self.h)
        code, out, _, _ = self.run_auto()
        self.assertEqual(code, 0, out)
        names = sorted(os.listdir(snap.moves_dir(Paths(self.h.home, self.h.state))))
        self.mdir = os.path.join(snap.moves_dir(Paths(self.h.home, self.h.state)), names[-1])

    def run_verify(self, *extra):
        argv = ["verify-start", "--home", self.h.home, "--state-dir", self.h.state,
                "--json"] + list(extra)
        forbidden = mock.Mock(side_effect=AssertionError("verify-start must not call this"))
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(common, "claude_app_running", lambda: True))
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

    def test_t5_ok_when_the_app_loaded_the_new_pair_long_ago(self):
        self.finished_move()
        code, out, err = self.run_verify("--wait-start", "2")
        self.assertEqual(code, 0, out + err)
        rep_path = os.path.join(self.mdir, "report-verify-start.json")
        self.assertTrue(os.path.isfile(rep_path), "report-verify-start.json in the newest move dir")
        with open(rep_path) as fh:
            rep = json.load(fh)
        self.assertEqual((rep["command"], rep["verdict"], rep["exit_code"]),
                         ("verify-start", "ok", 0))

    def test_t5_a_symlinked_active_pair_is_not_ok(self):
        self.finished_move()
        pair = os.path.join(self.h.support, CC, B, O2)
        moved = pair + ".real"
        os.rename(pair, moved)
        os.symlink(moved, pair)
        code, out, err = self.run_verify("--wait-start", "1")
        self.assertEqual(code, 3, out + err)
        with open(os.path.join(self.mdir, "report-verify-start.json")) as fh:
            self.assertNotEqual(json.load(fh)["verdict"], "ok")

    def test_t5_no_start_signal_is_app_not_started(self):
        self.finished_move()
        log = os.path.join(self.h.home, "Library", "Logs", "Claude", "main.log")
        with open(log) as fh:
            kept = [ln for ln in fh if "Loaded" not in ln]
        with open(log, "w") as fh:
            fh.writelines(kept)
        old = time.time() - 7200
        for store in (CC, LAM):
            pair = os.path.join(self.h.support, store, B, O2)
            for n in os.listdir(pair):
                os.utime(os.path.join(pair, n), (old, old))
        code, out, err = self.run_verify("--wait-start", "0.3")
        self.assertEqual(code, 3, out + err)
        with open(os.path.join(self.mdir, "report-verify-start.json")) as fh:
            self.assertEqual(json.load(fh)["verdict"], "app_not_started")


class NoRerunAdviceAnywhere(unittest.TestCase):
    """T6 class guard over every string constant of the package."""

    BAD = (u"\u0437\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u0435 \u043f\u0435\u0440\u0435\u043d\u043e\u0441 "
           u"\u0435\u0449\u0435 \u0440\u0430\u0437",       # run the move again (RU)
           u"\u043f\u0435\u0440\u0435\u043d\u043e\u0441 \u0435\u0449\u0435 \u0440\u0430\u0437",
           "run the move again", "run auto again", "run the transfer again",
           u"\u0437\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u0435 \u044d\u0442\u043e\u0442 "
           u"\u0444\u0430\u0439\u043b \u0435\u0449\u0435 \u0440\u0430\u0437")   # F6: run this file again
    ALLOWED = ()      # (module, fragment) pairs that are legit, none today

    def test_no_message_tells_to_run_the_move_again(self):
        pkg = os.path.join(ROOT, "claude_account_move")
        hits = []
        for name in sorted(os.listdir(pkg)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(pkg, name), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    low = node.value.lower().replace(u"\u0451", u"\u0435")
                    for bad in self.BAD:
                        if bad in low and (name, bad) not in self.ALLOWED:
                            hits.append("%s:%d %r" % (name, node.lineno, node.value[:90]))
                            break
        self.assertEqual(hits, [], "a finished move must never be told to run again; "
                                   "give verify-start instead")


if __name__ == "__main__":
    unittest.main()
