"""Tests of `auto`: the one-run move. The app check, the login wait and the CLI
login are replaced; `claude /login` is never started. Fake HOME only."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fixture import CC, LAM, Home, tree_digest, uid  # noqa: E402
from test_cli import A, B, O1, O2, build_source, switch_to_target  # noqa: E402

from claude_account_move import __version__  # noqa: E402
from claude_account_move import auto_move, cli, common  # noqa: E402
from claude_account_move import ru_text as ru  # noqa: E402
from claude_account_move.common import read_identity, Paths  # noqa: E402

LAUNCHER = os.path.join(ROOT, "Claude Move.command")
ENVS = {"CLAUDE_ACCOUNT_MOVE_POLL_S": "0.01", "CLAUDE_ACCOUNT_MOVE_LOGIN_POLL_S": "0.01",
        "CLAUDE_ACCOUNT_MOVE_CLOSE_POLL_S": "0.01"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-auto-")
        self.h = Home(self.tmp)
        p = mock.patch.dict(os.environ, ENVS)
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("CLAUDE_ACCOUNT_MOVE_SYNC_HEARTBEAT", None)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.login_calls = []

    def user_logs_in(self, reason=None, also_sync=False):
        """Stand-in for wait_app_switch: the owner signs in to the app."""
        def fake(cli_mod, paths, source, allow_same, limit, poll, out):
            switch_to_target(self.h)
            if reason == "cli_lags":
                self.h.accounts(cli=A, app=B)
            if also_sync:
                r = self.h.run("sync", "--apply")
                assert r.returncode == 0, r.stdout + r.stderr
            self.after_login = tree_digest_outside_support(self.h)
            self.login_started = time.monotonic()
            return read_identity(Paths(self.h.home, self.h.state)), reason
        return fake

    def run_auto(self, *extra, app_running=None, wait=None, cli_login=None,
                 read_back=None, settle=0.05, real_login=False):
        """(exit code, stdout, stderr, mocks). Everything outside is replaced."""
        argv = ["auto", "--home", self.h.home, "--state-dir", self.h.state] + list(extra)
        running = app_running if app_running is not None else (lambda: False)
        wait = wait or self.user_logs_in()

        def fake_login(cmd, timeout=None):
            self.login_calls.append(tuple(cmd))
            return cli_login(cmd) if cli_login else 0

        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(common, "claude_app_running", running))
            es.enter_context(mock.patch.object(auto_move, "wait_app_switch", wait))
            if not real_login:
                es.enter_context(mock.patch.object(auto_move, "run_cli_login", fake_login))
            es.enter_context(mock.patch.object(auto_move, "CLI_SETTLE_MIN_S", settle))
            if read_back:
                es.enter_context(mock.patch.object(auto_move, "_read_back", read_back))
            m = {n: es.enter_context(mock.patch.object(cli, n, wraps=getattr(cli, n)))
                 for n in ("cmd_prepare", "cmd_stamp", "cmd_finish", "cmd_sync")}
            es.enter_context(contextlib.redirect_stdout(out))
            es.enter_context(contextlib.redirect_stderr(err))
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue(), m

    def support_leftovers(self):
        bad = []
        for dp, dns, fns in os.walk(self.h.support):
            for n in fns:
                if n.startswith((".claude-account-move", ".tmp-", ".stamp-old", ".undo-")):
                    bad.append(os.path.join(dp, n))
        return bad


def slurp(path, mode="rb"):
    with open(path, mode) as fh:
        return fh.read()


def tree_digest_outside_support(h):
    d = tree_digest(h.home)
    return {k: v for k, v in d.items() if not k.startswith(h.support)}


class Happy(Base):
    def test_full_move_with_sync(self):
        build_source(self.h)
        code, out, _, m = self.run_auto()
        self.assertEqual(code, 0, out)
        self.assertIn(ru.VERDICT_OK, out)
        self.assertEqual(out.strip().splitlines()[-1], ru.VERDICT_OK)
        for n in ("cmd_prepare", "cmd_stamp", "cmd_sync"):
            self.assertEqual(m[n].call_count, 1, n)
        self.assertEqual(m["cmd_finish"].call_count, 2)     # once again after sync
        self.assertIn(ru.OF % (3, 3), out)                  # numbers with denominators
        self.assertIn("stamp --undo", out)
        self.assertIn("sync --undo", out)
        self.assertEqual(self.login_calls, [])
        names = os.listdir(self.h.state)
        self.assertTrue(any(n.startswith("stamp-") for n in names))
        self.assertTrue(any(n.startswith("sync-") for n in names))
        self.assertEqual(self.support_leftovers(), [])

    def test_sync_only_when_needed(self):
        build_source(self.h)
        code, out, _, m = self.run_auto(wait=self.user_logs_in(also_sync=True))
        self.assertEqual(code, 0, out)
        self.assertEqual(m["cmd_sync"].call_count, 0)
        self.assertEqual(m["cmd_finish"].call_count, 1)
        self.assertNotIn("sync --undo", out)

    def test_no_stamp_flag(self):
        d = build_source(self.h)
        before = {f: slurp(os.path.join(d, f)) for f in os.listdir(d)}
        code, out, _, m = self.run_auto("--no-stamp")
        self.assertEqual(code, 0, out)
        self.assertEqual(m["cmd_stamp"].call_count, 0)
        after = {f: slurp(os.path.join(d, f)) for f in os.listdir(d)}
        self.assertEqual(before, after)
        self.assertNotIn("stamp --undo", out)

    def test_titles_are_stamped(self):
        d = build_source(self.h)
        code, out, _, _ = self.run_auto()
        self.assertEqual(code, 0, out)
        titles = [json.loads(slurp(os.path.join(d, f), "r"))["title"] for f in os.listdir(d)]
        self.assertTrue(all(chr(0xB7) in t for t in titles), titles)

    def test_json_report(self):
        build_source(self.h)
        code, out, err, _ = self.run_auto("--json")
        self.assertEqual(code, 0, err)
        rep = json.loads(out)
        self.assertEqual(rep["command"], "auto")
        self.assertEqual(rep["verdict"], "ok")
        self.assertEqual([s["step"] for s in rep["steps"]][:4],
                         ["app_closed", "prepare", "write_probe", "stamp"])
        self.assertIn(ru.VERDICT_OK, err)                 # human text goes to stderr

    def test_writes_only_state_dir_and_fake_storage(self):
        build_source(self.h)
        code, out, _, _ = self.run_auto()
        self.assertEqual(code, 0, out)
        # outside the Claude support folder nothing changed since the login
        self.assertEqual(tree_digest_outside_support(self.h), self.after_login)
        # only the two planned places were touched in the whole temp root
        top = set(os.listdir(self.tmp))
        self.assertEqual(top, {"home", "state"})
        self.assertEqual(self.support_leftovers(), [])


class AppRunning(Base):
    def test_waits_then_continues(self):
        build_source(self.h)
        calls = {"n": 0}

        def running():
            calls["n"] += 1
            return calls["n"] <= 3

        code, out, _, _ = self.run_auto(app_running=running)
        self.assertEqual(code, 0, out)
        self.assertIn("Cmd+Q", out)
        self.assertIn(ru.APP_CLOSED, out)
        self.assertGreaterEqual(calls["n"], 4)

    def test_never_closes_fails_cleanly(self):
        build_source(self.h)
        before = tree_digest(self.h.home)
        code, out, _, m = self.run_auto("--wait-close", "0.1", app_running=lambda: True)
        self.assertEqual(code, 3)
        self.assertIn("Cmd+Q", out)
        self.assertEqual(m["cmd_prepare"].call_count, 0)
        self.assertEqual(m["cmd_stamp"].call_count, 0)
        self.assertEqual(tree_digest(self.h.home), before)
        self.assertFalse(os.path.isdir(os.path.join(self.h.state, "moves")))


class PrepareNotReady(Base):
    def test_stops_before_stamp_with_plain_russian(self):
        build_source(self.h, empty_cid_live=True)
        before = tree_digest(self.h.home)
        code, out, _, m = self.run_auto()
        self.assertEqual(code, 3)
        self.assertIn(ru.PREPARE_FAIL, out)
        self.assertIn(ru.PREPARE_CHECKS["empty_cid_live"], out)   # explained in plain words
        self.assertEqual(m["cmd_stamp"].call_count, 0)
        self.assertEqual(m["cmd_finish"].call_count, 0)
        self.assertEqual(self.login_calls, [])
        self.assertEqual(tree_digest(self.h.home), before)   # storage untouched
        self.assertNotIn(ru.VERDICT_OK, out)


class Probe(Base):
    def test_cleans_up_on_read_mismatch(self):
        d = build_source(self.h)
        code, out, _, m = self.run_auto(read_back=lambda p: "not what was written")
        self.assertEqual(code, 3)
        self.assertIn(ru.PROBE_FAIL.split("%s")[0], out)
        self.assertFalse(os.path.lexists(os.path.join(d, auto_move.PROBE_NAME)))
        self.assertEqual(self.support_leftovers(), [])
        self.assertEqual(m["cmd_stamp"].call_count, 0)

    def test_unit_ok_and_cleanup(self):
        d = build_source(self.h)
        real = os.path.realpath(d)
        self.assertEqual(auto_move.probe_write(d, self.h.support)[0], "ok")
        self.assertFalse(os.path.lexists(os.path.join(real, auto_move.PROBE_NAME)))
        with mock.patch.object(auto_move, "_read_back", lambda p: "x"):
            status, _ = auto_move.probe_write(d, self.h.support)
        self.assertEqual(status, "failed")
        self.assertFalse(os.path.lexists(os.path.join(real, auto_move.PROBE_NAME)))
        with mock.patch.object(auto_move, "_read_back", side_effect=OSError("boom")):
            self.assertEqual(auto_move.probe_write(d, self.h.support)[0], "failed")
        self.assertFalse(os.path.lexists(os.path.join(real, auto_move.PROBE_NAME)))

    def test_refuses_outside_the_storage(self):
        outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(outside)
        build_source(self.h)
        self.assertEqual(auto_move.probe_write(outside, self.h.support)[0], "outside")
        self.assertEqual(os.listdir(outside), [])
        link = os.path.join(self.h.support, CC, "link-out")
        os.symlink(outside, link)
        self.assertEqual(auto_move.probe_write(link, self.h.support)[0], "outside")
        self.assertEqual(os.listdir(outside), [])

    def test_probe_name_is_not_a_card(self):
        self.assertFalse(auto_move.PROBE_NAME.startswith(common.CARD_PREFIX))
        self.assertFalse(auto_move.PROBE_NAME.endswith(".json"))

    def test_leftover_probe_is_replaced_and_removed(self):
        d = build_source(self.h)
        left = os.path.join(d, auto_move.PROBE_NAME)
        with open(left, "w") as fh:
            fh.write("old")
        self.assertEqual(auto_move.probe_write(d, self.h.support)[0], "ok")
        self.assertFalse(os.path.lexists(left))


class Login(Base):
    def test_timeout_keeps_stamp_and_never_runs_cli_login(self):
        build_source(self.h)
        code, out, _, m = self.run_auto(wait=lambda *a: (
            read_identity(Paths(self.h.home, self.h.state)), "app_not_switched"))
        self.assertEqual(code, 6)
        self.assertEqual(self.login_calls, [])
        self.assertEqual(m["cmd_finish"].call_count, 0)
        self.assertIn("stamp --undo", out)
        self.assertIn(ru.VERDICT_BAD.split("%s")[0], out)

    def test_cli_lag_runs_cli_login_in_this_window(self):
        build_source(self.h)

        def fix(cmd):
            self.h.accounts(cli=B, app=B)
            return 0

        code, out, _, _ = self.run_auto(wait=self.user_logs_in("cli_lags"), cli_login=fix)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.login_calls, [("claude", "auth", "login")])
        self.assertIn(ru.VERDICT_OK, out)

    def test_fallback_command_when_first_does_not_help(self):
        build_source(self.h)

        def fix_second(cmd):
            if cmd[-1] == "/login":
                self.h.accounts(cli=B, app=B)
            return 0

        code, out, _, _ = self.run_auto(wait=self.user_logs_in("cli_lags"),
                                        cli_login=fix_second, *["--wait-login", "5"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.login_calls, [("claude", "auth", "login"), ("claude", "/login")])

    def test_cli_never_matches_is_a_clean_timeout(self):
        build_source(self.h)
        code, out, _, _ = self.run_auto("--wait-login", "0.2",
                                        wait=self.user_logs_in("cli_lags"))
        self.assertEqual(code, 6)
        self.assertIn(ru.LOGIN_REASON["cli_lags"], out)
        self.assertEqual(len(self.login_calls), 2)


class Bounded(Base):
    """P1 fixes: a CLI login that never ends, and a move left unfinished."""

    def test_hanging_cli_login_stops_with_exit_6_in_budget(self):
        build_source(self.h)
        hang = ("python3", "-c", "import time; time.sleep(60)")
        t0 = time.monotonic()
        with mock.patch.object(auto_move, "LOGIN_COMMANDS", (hang,)):
            code, out, _, m = self.run_auto("--wait-login", "0.3", settle=0.2,
                                            real_login=True,
                                            wait=self.user_logs_in("cli_lags"))
        self.assertEqual(code, 6, out)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertIn(ru.LOGIN_REASON["cli_login_timeout"], out)
        self.assertEqual(m["cmd_finish"].call_count, 0)

    def test_unfinished_move_is_resumed_not_prepared_again(self):
        build_source(self.h)
        code, out, _, m = self.run_auto("--wait-login", "0.1", settle=0.05,
                                        wait=self.user_logs_in("cli_lags"))
        self.assertEqual(code, 6, out)           # app is on B now, CLI is not
        moves = os.path.join(self.h.state, "moves")
        first = [n for n in os.listdir(moves) if not n.endswith(".partial")]
        self.assertEqual(len(first), 1)

        def fix(cmd):
            self.h.accounts(cli=B, app=B)
            return 0

        code, out, _, m = self.run_auto(wait=self.user_logs_in(), cli_login=fix)
        self.assertEqual(code, 0, out)
        self.assertEqual(m["cmd_prepare"].call_count, 0)
        self.assertGreaterEqual(m["cmd_finish"].call_count, 1)
        self.assertEqual(os.listdir(moves), first)     # no new baseline
        self.assertIn(first[0], out)
        self.assertIn(ru.VERDICT_OK, out)


class Resume(Base):
    """Round 2: which move `auto` resumes, refuses, or leaves alone."""

    def left_unfinished(self):
        build_source(self.h)
        code, out, _, _ = self.run_auto("--wait-login", "0.1", settle=0.05,
                                        wait=self.user_logs_in("cli_lags"))
        self.assertEqual(code, 6, out)
        moves = os.path.join(self.h.state, "moves")
        (name,) = [n for n in os.listdir(moves) if not n.endswith(".partial")]
        return moves, name

    def second_run(self, *extra):
        def fix(cmd):
            self.h.accounts(cli=B, app=B)
            return 0
        return self.run_auto(*extra, wait=self.user_logs_in(), cli_login=fix)

    def test_newer_finished_move_ends_the_search(self):
        moves, old = self.left_unfinished()
        new = "99991231T000000000Z" + old[old.index("-"):]
        shutil.copytree(os.path.join(moves, old), os.path.join(moves, new))
        with open(os.path.join(moves, new, "report-finish.json"), "w") as fh:
            json.dump({"command": "finish", "exit_code": 0}, fh)
        _, _, _, m = self.second_run()
        self.assertEqual(m["cmd_prepare"].call_count, 1)

    def test_newest_expired_unfinished_move_stops_auto(self):
        moves, old = self.left_unfinished()
        mp = os.path.join(moves, old, "manifest.json")
        man = json.loads(slurp(mp, "r"))
        man["created_at"] = "2020-01-01T00:00:00+00:00"
        with open(mp, "w") as fh:
            json.dump(man, fh)
        code, out, _, m = self.second_run()
        self.assertEqual(code, common.EXIT_NO_BASELINE, out)
        self.assertEqual(m["cmd_prepare"].call_count, 0)
        self.assertIn(old, out)
        self.assertIn("--new-move", out)

    def test_new_move_flag_skips_the_search(self):
        self.left_unfinished()
        _, _, _, m = self.second_run("--new-move")
        self.assertEqual(m["cmd_prepare"].call_count, 1)


class Review(Base):
    """Regressions found by the review of auto_move.py."""

    def test_name_taken_by_a_foreign_link_is_left_alone(self):
        d = build_source(self.h)
        real = os.path.realpath(d)
        victim = os.path.join(self.tmp, "victim.txt")
        with open(victim, "w") as fh:
            fh.write("keep me")
        link = os.path.join(real, auto_move.PROBE_NAME)
        os.symlink(victim, link)
        status, _ = auto_move.probe_write(d, self.h.support)
        self.assertEqual(status, "failed")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(slurp(victim, "r"), "keep me")

    def test_name_taken_by_a_directory_is_left_alone(self):
        d = build_source(self.h)
        taken = os.path.join(os.path.realpath(d), auto_move.PROBE_NAME)
        os.makedirs(taken)
        self.assertEqual(auto_move.probe_write(d, self.h.support)[0], "failed")
        self.assertTrue(os.path.isdir(taken))

    def test_delete_error_does_not_hide_the_read_back_error(self):
        d = build_source(self.h)
        real_unlink = os.unlink

        def flaky(path, *a, **k):
            if os.path.basename(str(path)) == auto_move.PROBE_NAME:
                raise PermissionError("denied")
            return real_unlink(path, *a, **k)

        with mock.patch.object(auto_move, "_read_back", lambda p: "wrong"), \
                mock.patch("os.unlink", flaky):
            status, detail = auto_move.probe_write(d, self.h.support)
        os.unlink(os.path.join(os.path.realpath(d), auto_move.PROBE_NAME))
        self.assertEqual(status, "failed")
        self.assertIn("read back differs", detail)
        self.assertIn("could not be deleted", detail)

    def _drifting(self, module):
        calls = {"n": 0}

        def ident(paths):
            calls["n"] += 1
            if calls["n"] > 3000:
                raise RuntimeError("busy loop: no sleep and no deadline check")
            return {"cli": {"status": "ok", "value": B}, "app": {"status": "ok", "value": B},
                    "cli_org": str(calls["n"])}          # differs on every read
        return calls, mock.patch.object(module, "read_identity", ident)

    def test_wait_app_switch_ends_when_identity_keeps_changing(self):
        calls, patch = self._drifting(auto_move)
        paths = Paths(self.h.home, self.h.state)
        with patch:
            ident, reason = auto_move.wait_app_switch(
                cli, paths, A, False, 0.2, 0.01, cli.Out(False, quiet=True))
        self.assertEqual(reason, "identity_invalid")
        self.assertLess(calls["n"], 200)

    def test_cli_wait_for_login_ends_when_identity_keeps_changing(self):
        calls, patch = self._drifting(cli)
        paths = Paths(self.h.home, self.h.state)
        with patch:
            ident, reason, _ = cli.wait_for_login(
                paths, A, False, 0.2, 0.01, cli.Out(False, quiet=True))
        self.assertEqual(reason, "identity_invalid")
        self.assertLess(calls["n"], 200)

    def test_bad_active_pair_is_a_plain_input_error(self):
        build_source(self.h)
        code, out, _, m = self.run_auto("--active-pair", "not-a-pair")
        self.assertEqual(code, 2)
        self.assertIn(ru.BAD_PAIR.split("%s")[0], out)
        self.assertEqual(m["cmd_stamp"].call_count, 0)

    def test_foreign_active_pair_is_a_plain_input_error_and_json(self):
        build_source(self.h)
        code, out, err, m = self.run_auto("--active-pair", "%s/%s" % (B, O2), "--json")
        self.assertEqual(code, 2, err)
        rep = json.loads(out)
        self.assertEqual(rep["verdict"], "input_error")
        self.assertIn(ru.BAD_PAIR.split("%s")[0], err)
        self.assertEqual(m["cmd_stamp"].call_count, 0)

    def test_login_grace_is_one_period_not_one_per_attempt(self):
        build_source(self.h)
        code, out, _, _ = self.run_auto("--wait-login", "0.05", settle=0.5,
                                        wait=self.user_logs_in("cli_lags"))
        spent = time.monotonic() - self.login_started
        self.assertEqual(code, 6)
        self.assertEqual(len(self.login_calls), 2)
        self.assertLess(spent, 0.9)      # one 0.5 s grace, not two


class Launcher(unittest.TestCase):
    def test_entry_and_version(self):
        self.assertTrue(os.access(LAUNCHER, os.X_OK))
        self.assertEqual(subprocess.run(["bash", "-n", LAUNCHER]).returncode, 0)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "claude-account-move"),
                            "--version"], capture_output=True, text=True)
        self.assertIn("0.1.3", r.stdout)
        self.assertEqual(__version__, "0.1.3")

    def _stage(self):
        tmp = tempfile.mkdtemp(prefix="cam-launch-")
        self.addCleanup(shutil.rmtree, tmp, True)
        real = os.path.join(tmp, "real")
        os.makedirs(real)
        shutil.copy(LAUNCHER, os.path.join(real, "Claude Move.command"))
        stub = os.path.join(real, "claude-account-move")
        with open(stub, "w") as fh:
            fh.write('#!/bin/sh\necho "PWD=$(pwd -P)"\necho "ARGS=$*"\nexit 0\n')
        os.chmod(stub, 0o755)
        desk = os.path.join(tmp, "desk")
        os.makedirs(desk)
        link = os.path.join(desk, "Move")
        os.symlink(os.path.join(real, "Claude Move.command"), link)
        return tmp, real, link

    def test_follows_symlink_and_passes_state_dir(self):
        tmp, real, link = self._stage()
        env = dict(os.environ, CLAUDE_ACCOUNT_MOVE_HOME=os.path.join(tmp, "st"))
        r = subprocess.run(["bash", link], env=env, input="\n", capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PWD=" + os.path.realpath(real), r.stdout)
        self.assertIn("ARGS=auto --state-dir " + os.path.join(tmp, "st") + " --pause", r.stdout)

    def test_default_state_dir(self):
        tmp, real, link = self._stage()
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_ACCOUNT_MOVE_HOME"}
        env["HOME"] = os.path.join(tmp, "fakehome")
        r = subprocess.run(["bash", link], env=env, input="\n", capture_output=True, text=True)
        self.assertIn("--state-dir %s/MEGAsync/Private/Work/claude-account-move-state"
                      % env["HOME"], r.stdout)

    def test_exit_code_is_passed_through(self):
        tmp, real, link = self._stage()
        with open(os.path.join(real, "claude-account-move"), "w") as fh:
            fh.write("#!/bin/sh\nexit 3\n")
        r = subprocess.run(["bash", link], input="\n", capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)


class Pause(Base):
    def test_pause_waits_for_enter_and_keeps_the_exit_code(self):
        build_source(self.h)
        with mock.patch("builtins.input", return_value="") as inp:
            code, out, _, _ = self.run_auto("--pause", "--wait-close", "0.05",
                                            app_running=lambda: True)
        self.assertEqual(code, 3)
        inp.assert_called_once()
        self.assertIn(ru.CLOSE_PROMPT, inp.call_args[0][0])

    def test_no_pause_without_the_flag(self):
        build_source(self.h)
        with mock.patch("builtins.input") as inp:
            self.run_auto("--wait-close", "0.05", app_running=lambda: True)
        inp.assert_not_called()

    def test_closed_stdin_does_not_hang_or_fail(self):
        build_source(self.h)
        with mock.patch("builtins.input", side_effect=EOFError):
            code, _, _, _ = self.run_auto("--pause", "--wait-close", "0.05",
                                          app_running=lambda: True)
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
