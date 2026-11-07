"""Oracle tests for the post-start verification of `auto` (defects D1, D2 of the
live move of 07.11.2026). Fake HOME only; the app, the login wait and the CLI
login are replaced; `claude /login` is never started.

D1  --active-pair names the OLD account's pair: only the steps before the login
    (prepare, write probe, stamp) may use it; heal-active, sync and finish after
    the login resolve the pair of the NEW login themselves (active_pair=None).
D2  the app picks the organization folder it writes into only when it STARTS
    under the new account (main.log "Loaded N persisted sessions from
    <store>/<acc>/<org>", one line per store). So after finish auto asks the
    owner to open Claude, waits for fresh store-root loads of the NEW account,
    runs finish again with the app running (that run is the verdict), repairs a
    symlinked pair at most once, and never reports success on an unmeasured pair.
"""
import contextlib
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from fixture import AskWatcher, CC, LAM, uid  # noqa: E402
from test_auto import Base  # noqa: E402  (only Base: its fixtures, no test cases)
from test_cli import A, B, O1, O2, build_source  # noqa: E402

from claude_account_move import auto_move, cli, common  # noqa: E402
from claude_account_move import ru_text as ru  # noqa: E402
from claude_account_move.common import EXIT_FAIL, EXIT_OK  # noqa: E402

O3 = uid(0x77, 3)          # the pair the app loads at start (a link to O2 at first)
CYR = re.compile("[" + chr(0x400) + "-" + chr(0x4FF) + "]")
QUIT_AFTER_POLLS = 3       # the owner quits the app after this many polls


def stamp(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


class FakeApp(object):
    """The Claude app as the owner uses it. It is closed at first. `start`
    (the owner opens it when auto asks) appends one store-root load line per
    store for `pair` to main.log, stamped a little in the future so that it is
    newer than any moment auto asked, and then the app is running. A running app
    is quit by the owner after a few polls of `running`."""

    def __init__(self, home, delay=0.05):
        self.h = home
        self.delay = delay
        self.open = False
        self.polls = 0
        self.starts = 0
        self.written = []              # (store, acc, org) of every line written
        self.log_path = os.path.join(home.home, "Library", "Logs", "Claude", "main.log")
        self.before_load = None        # hook run on a start, before the lines
        self.decoy_old_first = False   # write OLD-account loads before the real ones
        self.silent = False            # the owner never opens the app
        self.threads = []
        self.last_epoch = 0            # newest log stamp this app wrote
        self.ahead = 1                 # seconds the stamps run ahead of the clock
        self.reopen = True             # the owner opens the app again after a repair
        self.start_before_ask = False  # ... and the app starts between heal and ask
        self.rotate = False            # the app starts a fresh main.log (new file)

    def running(self):
        if self.open:
            self.polls += 1
            if self.polls > QUIT_AFTER_POLLS:
                self.open = False
        return self.open

    def line(self, store, acc, org, ts):
        return ("%s info: Loaded 3 persisted sessions from %s\n"
                % (ts, os.path.join(self.h.support, store, acc, org)))

    def append(self, rows, ts):
        with open(self.log_path, "a") as fh:
            for store, acc, org in rows:
                fh.write(self.line(store, acc, org, ts))
                self.written.append((store, acc, org))

    def seed_stale(self, rows):
        """Loads from long ago: they must never satisfy the wait."""
        self.append(rows, "2026-01-01 00:00:01")

    def start(self, pair):
        if self.silent:
            return
        self.starts += 1

        def go():
            time.sleep(self.delay)
            if self.before_load:
                self.before_load(self.starts)
            if self.rotate:                         # a rotated log: a new, shorter file
                tmp = self.log_path + ".new"
                open(tmp, "w").close()
                os.replace(tmp, self.log_path)
            self.last_epoch = int(time.time()) + self.ahead
            ts = stamp(self.last_epoch)
            if self.decoy_old_first:
                self.append([(CC, A, O1), (LAM, A, O1)], ts)
                time.sleep(self.delay)
            # one pair for both stores, or a {store: pair} map (stores may differ)
            pairs = pair if isinstance(pair, dict) else {CC: pair, LAM: pair}
            self.append([(CC,) + tuple(pairs[CC])], ts)
            time.sleep(self.delay)               # the second store loads later
            self.append([(LAM,) + tuple(pairs[LAM])], ts)
            self.polls = 0
            self.open = True

        t = threading.Thread(target=go, daemon=True)
        t.start()
        self.threads.append(t)

    def settle(self):
        """Let the clock pass the stamps of the earlier start: lines have a
        resolution of one second, so a later ask must be a later second."""
        wait = self.last_epoch + 1.0 - time.time()
        if wait > 0:
            time.sleep(wait)

    def idle(self):
        return not self.open and not any(t.is_alive() for t in self.threads)

    def join(self):
        for t in self.threads:
            t.join(5)


def link_pair(h, store, org, to_org):
    """<store>/B/org becomes a relative symlink to <store>/B/to_org."""
    d = os.path.join(h.support, store, B)
    os.symlink(to_org, os.path.join(d, org))


class PostStart(Base):
    """Shared scenario: A -> B, the app later loads the pair (B, post_pair)."""

    def setUp(self):
        super(PostStart, self).setUp()
        self.app = FakeApp(self.h)
        self.finish_log = []           # per cmd_finish call: dict(open, loaded, out_len)
        self.heal_log = []
        self.sync_ns = []
        self.finish_stub = {}          # call number (1-based) -> (code, report)
        self.out_buf = io.StringIO()
        self.post_pair = (B, O2)
        self.login_hooks = []

    # -- scenario helpers ----------------------------------------------------
    def empty_lam_source(self):
        d = build_source(self.h)
        lam = os.path.join(self.h.support, LAM, A, O1)
        for f in os.listdir(lam):               # the agent store has no cards at all
            os.unlink(os.path.join(lam, f))
        os.unlink(os.path.join(self.h.projects, uid(14) + ".jsonl"))
        return d

    def symlinked_post_pair(self):
        """The app will load (B, O3); in both stores O3 is a link to the real O2
        (the shape of the live move: the loaded pair was a symlink)."""
        for store in (CC, LAM):
            link_pair(self.h, store, O3, O2)
        self.post_pair = (B, O3)

    def login_with_names(self):
        """The owner signs in; the names are already in the new pair."""
        return self.user_logs_in(also_sync=True)

    # -- the run -------------------------------------------------------------
    def run_ps(self, *extra, **kw):
        # a fresh output for every run of auto; the owner opens the app when auto
        # actually prints its ask (the first one, or the one after a repair)
        def on_ask(marker):
            if marker == ru.START_ASK or self.app.reopen:
                self.app.start(self.post_pair)
        self.out_buf = AskWatcher(on_ask)
        inner = kw.pop("wait", None) or self.login_with_names()

        def wait(*a):
            r = inner(*a)
            # the app that the owner signed in with logged its store roots (long ago
            # by the clock of the tests): the log names the pair at login time
            self.app.seed_stale([(CC, B, O2), (LAM, B, O2)])
            for hook in self.login_hooks:     # things that exist once the owner signed in
                hook()
            return r
        argv = ["auto", "--home", self.h.home, "--state-dir", self.h.state] + list(extra)
        app = self.app
        real_finish, real_heal = cli.cmd_finish, cli.cmd_heal_active

        def finish(ns):
            n = len(self.finish_log) + 1
            self.finish_log.append(dict(
                ns=ns, open=app.open, out_len=len(self.out_buf.getvalue()),
                loaded=list(app.written)))
            if n in self.finish_stub:
                code, rep = self.finish_stub[n]
                ns.capture.append(rep)
            else:
                code = real_finish(ns)
            return code

        def heal(ns):
            code = real_heal(ns)
            rep = ns.capture[-1] if ns.capture else None
            self.heal_log.append(dict(ns=ns, code=code, verdict=(rep or {}).get("verdict")))
            if (app.start_before_ask and app.starts and code == EXIT_OK
                    and (rep or {}).get("verdict") == "healed"):
                # the app starts and quits again before auto prints its ask
                pp = self.post_pair
                pairs = pp if isinstance(pp, dict) else {CC: pp, LAM: pp}
                ts = stamp(int(time.time()) + 1)
                app.append([(CC,) + tuple(pairs[CC]), (LAM,) + tuple(pairs[LAM])], ts)
            return code

        real_sync = cli.cmd_sync

        def sync(ns):
            self.sync_ns.append(ns)
            return real_sync(ns)

        calls = {}
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(common, "claude_app_running", app.running))
            es.enter_context(mock.patch.object(auto_move, "wait_app_switch", wait))
            es.enter_context(mock.patch.object(auto_move, "run_cli_login",
                                               lambda cmd, timeout=None: 0))
            es.enter_context(mock.patch.object(auto_move, "CLI_SETTLE_MIN_S", 0.05))
            for name, fn in (("cmd_finish", finish), ("cmd_heal_active", heal),
                             ("cmd_sync", sync)):
                es.enter_context(mock.patch.object(cli, name, fn))
            for name in ("cmd_prepare", "cmd_stamp"):
                calls[name] = es.enter_context(
                    mock.patch.object(cli, name, wraps=getattr(cli, name)))
            err = io.StringIO()
            es.enter_context(contextlib.redirect_stdout(self.out_buf))
            es.enter_context(contextlib.redirect_stderr(err))
            try:
                code = cli.main(argv)
            except SystemExit as exc:         # a usage error of the argument parser
                code = exc.code
        app.join()
        return code, self.out_buf.getvalue(), err.getvalue(), calls

    def real_dir(self, store, org):
        p = os.path.join(self.h.support, store, B, org)
        return os.path.isdir(p) and not os.path.islink(p)


# ------------------------------------------------------------------ D1 ------
class OverrideOnlyBeforeLogin(PostStart):
    OLD_PAIR = "%s/%s" % (A, O1)

    def test_pre_login_steps_keep_the_override(self):
        build_source(self.h)
        code, out, _, calls = self.run_ps("--active-pair", self.OLD_PAIR)
        self.assertEqual(calls["cmd_prepare"].call_args[0][0].active_pair, self.OLD_PAIR)
        self.assertEqual(calls["cmd_stamp"].call_args[0][0].active_pair, self.OLD_PAIR)

    def test_post_login_steps_get_no_override(self):
        build_source(self.h)
        self.run_ps("--active-pair", self.OLD_PAIR, wait=self.user_logs_in())
        self.assertTrue(self.heal_log, "heal-active was not run")
        self.assertTrue(self.finish_log, "finish was not run")
        for h in self.heal_log:
            self.assertIsNone(h["ns"].active_pair, "heal-active got the old account's pair")
        for f in self.finish_log:
            self.assertIsNone(f["ns"].active_pair, "finish got the old account's pair")
        self.assertTrue(self.sync_ns, "sync was not needed in this scenario")
        for ns in self.sync_ns:      # inferred from "only the steps before the login"
            self.assertIsNone(ns.active_pair, "sync got the old account's pair")

    def test_move_with_old_pair_override_succeeds(self):
        """Live failure of 07.11: the old pair made finish raise PairError."""
        build_source(self.h)
        code, out, err, _ = self.run_ps("--active-pair", self.OLD_PAIR)
        self.assertEqual(code, 0, out + err)
        self.assertIn(ru.VERDICT_OK, out)


# ------------------------------------------------------------------ R1 ------
class AsksToOpenAndWaits(PostStart):
    def test_waits_for_fresh_loads_of_the_new_account_in_each_store(self):
        build_source(self.h)
        # stale loads of the NEW pair from long ago, and a fresh decoy of the OLD one
        self.app.decoy_old_first = True
        self.app.delay = 0.15
        code, out, err, _ = self.run_ps()
        self.assertEqual(code, 0, out + err)
        self.assertGreaterEqual(len(self.finish_log), 2,
                                "no finish after the owner opened the app")
        last = self.finish_log[-1]
        fresh = [w for w in last["loaded"] if w[1] == B]
        stores = {w[0] for w in fresh}
        self.assertEqual(stores, {CC, LAM},
                         "finish ran before both stores loaded for the new account")

    def test_asks_in_plain_russian_before_the_second_finish(self):
        build_source(self.h)
        code, out, err, _ = self.run_ps()
        self.assertEqual(code, 0, out + err)
        self.assertGreaterEqual(len(self.finish_log), 2)
        first, second = self.finish_log[0], self.finish_log[-1]
        between = out[first["out_len"]:second["out_len"]]
        asks = [ln for ln in between.splitlines()
                if CYR.search(ln) and "Claude" in ln]
        self.assertTrue(asks, "no Russian line asking to open Claude between the finishes:\n"
                        + between)

    def test_wait_start_option_exists_with_default_300(self):
        parser = cli.build_parser()
        ns = parser.parse_args(["auto"])
        self.assertEqual(getattr(ns, "wait_start", None), 300.0)
        ns = parser.parse_args(["auto", "--wait-start", "12"])
        self.assertEqual(ns.wait_start, 12.0)


# ------------------------------------------------------------------ R2 ------
class VerdictIsTheSecondFinish(PostStart):
    def test_last_finish_runs_with_the_app_running(self):
        build_source(self.h)
        code, out, err, _ = self.run_ps()
        self.assertEqual(code, 0, out + err)
        self.assertGreaterEqual(len(self.finish_log), 2)
        self.assertFalse(self.finish_log[0]["open"])
        self.assertTrue(self.finish_log[-1]["open"],
                        "the verdict finish ran while the app was quit")

    def test_second_finish_failure_is_the_verdict_even_if_first_passed(self):
        build_source(self.h)
        bad = {"checks": [{"id": "transcripts", "ok": False,
                           "detail": "1 of 4 recorded transcripts missing"}],
               "stores": {}, "notes": []}
        self.finish_stub[2] = (EXIT_FAIL, bad)     # the post-start finish
        code, out, err, _ = self.run_ps()
        self.assertEqual(code, 3, out + err)
        self.assertNotIn(ru.VERDICT_OK, out)
        self.assertIn(ru.VERDICT_BAD.split("%s")[0], out)
        self.assertEqual(len(self.finish_log), 2, "a failure that is not active_pair_real "
                         "must not trigger a repair round")


# ------------------------------------------------------------------ R3 ------
class RepairsTheLoadedPairOnce(PostStart):
    def setUp(self):
        super(RepairsTheLoadedPairOnce, self).setUp()

    def test_symlinked_loaded_pair_is_healed_and_reverified(self):
        """The live shape: after the start the loaded pair is a symlink."""
        build_source(self.h)
        self.login_hooks.append(self.symlinked_post_pair)
        code, out, err, _ = self.run_ps()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.finish_log), 3,
                         "expected finish before start, after start, after the repair")
        failed = [h for h in self.heal_log if h["verdict"] == "healed"]
        self.assertEqual(len(failed), 1, self.heal_log)
        self.assertTrue(self.real_dir(CC, O3) and self.real_dir(LAM, O3),
                        "the pair the app loaded is still not a real directory")
        self.assertTrue(self.finish_log[1]["open"] and self.finish_log[2]["open"])
        self.assertIn(ru.VERDICT_OK, out)

    def test_second_active_pair_failure_stops_with_exit_3_and_a_reason(self):
        build_source(self.h)
        self.login_hooks.append(self.symlinked_post_pair)

        def relink(start_no):
            """The app turns the loaded pair into a symlink again on every start."""
            for store in (CC, LAM):
                d = os.path.join(self.h.support, store, B)
                o3 = os.path.join(d, O3)
                if os.path.islink(o3):
                    continue                    # still a link: nothing to redo
                o2 = os.path.join(d, O2)
                os.unlink(o2)
                os.rename(o3, o2)
                os.symlink(O2, o3)

        self.app.before_load = relink
        code, out, err, _ = self.run_ps()
        self.assertEqual(code, 3, out + err)
        self.assertEqual(len(self.finish_log), 3, "at most one repair round")
        healed = [h for h in self.heal_log if h["verdict"] == "healed"]
        self.assertEqual(len(healed), 1, "a second repair round was started")
        self.assertNotIn(ru.VERDICT_OK, out)
        bad = [ln for ln in out.splitlines() if ln.startswith(ru.VERDICT_BAD.split("%s")[0])]
        self.assertTrue(bad and CYR.search(bad[-1]), out)


# ------------------------------------------------------------------ R4 ------
class NoFreshLoad(PostStart):
    def test_stale_loads_only_stops_with_exit_3_and_never_succeeds(self):
        build_source(self.h)
        self.app.silent = True                   # the owner never opens the app
        code, out, err, _ = self.run_ps("--wait-start", "0.4")
        self.assertEqual(code, 3, out + err)
        self.assertNotIn(ru.VERDICT_OK, out)
        self.assertEqual(len(self.finish_log), 1,
                         "finish must not run again on an unmeasured pair")
        bad = [ln for ln in out.splitlines() if ln.startswith(ru.VERDICT_BAD.split("%s")[0])]
        self.assertTrue(bad and CYR.search(bad[-1]), out)

    def test_loads_of_only_one_store_are_not_enough(self):
        build_source(self.h)
        self.app.silent = True
        # a fresh load for the CC store only; LAM never loads
        def one_store(pair):
            threading.Timer(0.05, lambda: self.app.append(
                [(CC, B, O2)], stamp(int(time.time()) + 1))).start()
        self.app.start = one_store
        code, out, err, _ = self.run_ps("--wait-start", "0.4")
        self.assertEqual(code, 3, out + err)
        self.assertNotIn(ru.VERDICT_OK, out)
        self.assertEqual(len(self.finish_log), 1)


# ------------------------------------------------------------------ R5 ------
class NamesTheAppRestoresDoNotStopAuto(PostStart):
    def test_first_finish_failing_on_restorable_names_goes_on_to_the_start(self):
        build_source(self.h)
        rep = {"checks": [
            {"id": CC + ":target_names", "ok": False, "detail": "names lag"},
            {"id": CC + ":lagging", "ok": False, "detail": "1 directory lags"}],
            "stores": {CC: {"names_before": 3, "names_in_target": 2,
                            "target_copy_wrong_cid": 1}},
            "notes": []}
        self.assertFalse(auto_move.syncable(rep), "scenario must not be syncable")
        self.finish_stub[1] = (EXIT_FAIL, rep)
        code, out, err, _ = self.run_ps()
        self.assertEqual(code, 0, out + err)
        self.assertGreaterEqual(len(self.finish_log), 2)
        self.assertTrue(self.finish_log[-1]["open"])


def link_in(h, store, org, to_org):
    link_pair(h, store, org, to_org)


# ------------------------------------------------------------- R6, R7 ------
class PerStorePairs(PostStart):
    """The app may load a different organization in each store."""

    def test_r6_each_store_is_judged_by_the_pair_it_loaded(self):
        """CC loads (B, O3), a link to O2; LAM then loads (B, O2), a real folder.
        One shared pair ("the last mention": O2) would pass; CC's loaded pair
        is a symlink and must be found and healed."""
        build_source(self.h)
        self.login_hooks.append(lambda: link_in(self.h, CC, O3, O2))
        self.post_pair = {CC: (B, O3), LAM: (B, O2)}
        code, out, err, _ = self.run_ps()
        self.assertTrue(self.real_dir(CC, O3),
                        "the pair CC loaded is still a symlink, the move was reported ok")
        self.assertTrue(self.real_dir(LAM, O2), "LAM's loaded pair was touched")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.finish_log), 3,
                         "finish before start, after start (fails), after the repair")

    def test_r7_repair_touches_only_the_store_whose_loaded_pair_is_a_link(self):
        """CC loads (B, O2), real. LAM loads (B, O3), a link to O2; CC also has an
        O3 link that nobody loaded. Healing O3 in both stores would turn CC's
        real, loaded O2 into a link."""
        build_source(self.h)

        def links():
            link_in(self.h, LAM, O3, O2)
            link_in(self.h, CC, O3, O2)
        self.login_hooks.append(links)
        self.post_pair = {CC: (B, O2), LAM: (B, O3)}
        code, out, err, _ = self.run_ps()
        self.assertTrue(self.real_dir(LAM, O3), "LAM's loaded pair was not healed")
        self.assertTrue(self.real_dir(CC, O2),
                        "CC's loaded, real pair was turned into a symlink by the repair")
        self.assertTrue(os.path.islink(os.path.join(self.h.support, CC, B, O3)),
                        "CC's unloaded O3 link was touched")
        self.assertEqual(code, 0, out + err)


# ------------------------------------------------------------------ R8 ------
class FreshMeansAppendedAfterTheAsk(PostStart):
    def test_r8_lines_already_in_the_log_never_count_even_if_stamped_later(self):
        build_source(self.h)
        ahead = stamp(int(time.time()) + 600)       # stamped after the ask, written before it
        self.login_hooks.append(lambda: self.app.append(
            [(CC, B, O2), (LAM, B, O2)], ahead))
        self.app.silent = True
        code, out, err, _ = self.run_ps("--wait-start", "0.4")
        self.assertEqual(code, 3, out + err)
        self.assertNotIn(ru.VERDICT_OK, out)
        self.assertEqual(len(self.finish_log), 1, "finish ran on an unmeasured pair")

    def test_r8_after_the_repair_old_lines_do_not_count_either(self):
        build_source(self.h)
        self.login_hooks.append(self.symlinked_post_pair)
        self.app.ahead = 5                           # first-start lines stamped well ahead
        self.app.reopen = False                      # the owner does not open the app again
        code, out, err, _ = self.run_ps("--wait-start", "1")
        self.assertEqual(code, 3, out + err)
        self.assertNotIn(ru.VERDICT_OK, out)
        self.assertEqual(len(self.finish_log), 2, "the third finish ran without a new start")

    def test_r8_guard_a_rotated_log_counts_as_all_new(self):
        """Not red before the fix: a fresh, shorter main.log (new file) is accepted."""
        build_source(self.h)
        self.login_hooks.append(lambda: self.app.append(
            [(CC, B, O2)] * 40, "2026-01-01 00:00:02"))      # a long old log
        self.app.rotate = True
        code, out, err, _ = self.run_ps("--wait-start", "3")
        self.assertEqual(code, 0, out + err)


# ------------------------------------------------------------------ R9 ------
class StoppedMoveCanBeResumed(PostStart):
    def resumable(self):
        from claude_account_move.common import Paths, read_identity
        paths = Paths(self.h.home, self.h.state)
        acc = read_identity(paths)["app"]["value"]
        return auto_move.find_unfinished_move(paths, acc)

    def test_r9_start_timeout_leaves_the_move_unfinished_and_a_rerun_resumes_it(self):
        build_source(self.h)
        self.app.silent = True
        code, out, err, _ = self.run_ps("--wait-start", "0.4")
        self.assertEqual(code, 3, out + err)
        found = self.resumable()
        self.assertTrue(found and found[0] == "resume",
                        "the stopped move looks finished to the resume logic: %r" % (found,))
        move_id = found[1]
        # the owner runs auto again and this time opens the app when asked
        self.app.silent = False
        self.login_hooks[:] = []
        keep = self.finish_log[:]
        from claude_account_move.common import Paths, read_identity

        def no_login(*a):
            return read_identity(Paths(self.h.home, self.h.state)), None
        code, out, err, calls = self.run_ps(wait=no_login)
        self.assertEqual(calls["cmd_prepare"].call_count, 0, "a new move was prepared")
        self.assertIn(ru.RESUME_MOVE.split("%s")[0], out)
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.finish_log[len(keep):][-1]["open"])
        self.assertTrue(os.path.isdir(os.path.join(self.h.state, "moves", move_id)))

    def test_r9_second_active_pair_failure_leaves_the_move_unfinished(self):
        build_source(self.h)
        self.login_hooks.append(self.symlinked_post_pair)

        def relink(start_no):
            for store in (CC, LAM):
                d = os.path.join(self.h.support, store, B)
                o3 = os.path.join(d, O3)
                if os.path.islink(o3):
                    continue
                o2 = os.path.join(d, O2)
                os.unlink(o2)
                os.rename(o3, o2)
                os.symlink(O2, o3)
        self.app.before_load = relink
        code, out, err, _ = self.run_ps()
        self.assertEqual(code, 3, out + err)
        found = self.resumable()
        self.assertTrue(found and found[0] == "resume",
                        "the stopped move looks finished to the resume logic: %r" % (found,))


# ----------------------------------------------------------------- R10 ------
class LoadedPairMustExist(PostStart):
    """The pair a store loaded must be a real directory, cards or no cards."""

    def check(self, make):
        self.empty_lam_source()
        missing = os.path.join(self.h.support, LAM, B, O3)
        self.login_hooks.append(lambda: make(missing))
        self.post_pair = {CC: (B, O2), LAM: (B, O3)}
        code, out, err, _ = self.run_ps()
        self.assertEqual(code, 3, out + err)
        self.assertNotIn(ru.VERDICT_OK, out)
        self.assertIn(LAM, out, "the reason does not name the store")
        self.assertIn(O3, out, "the reason does not name the pair")
        self.assertFalse(os.path.islink(missing), "heal must not touch it")
        self.assertEqual(len([h for h in self.heal_log if h["verdict"] == "healed"]), 0)
        return missing

    def test_r10_missing_loaded_pair_of_an_empty_store_fails(self):
        missing = self.check(lambda p: None)
        self.assertFalse(os.path.lexists(missing), "the missing pair was created")

    def test_r10_loaded_pair_that_is_a_file_fails(self):
        def make(p):
            with open(p, "w") as fh:
                fh.write("not a directory")
        missing = self.check(make)
        self.assertTrue(os.path.isfile(missing))


# ----------------------------------------------------------------- R11 ------
class MarkIsTakenAtTheSecondAsk(PostStart):
    def test_r11_a_start_between_the_heal_and_the_ask_is_not_the_start_after_the_ask(self):
        build_source(self.h)
        self.login_hooks.append(self.symlinked_post_pair)
        self.app.start_before_ask = True     # the app starts and quits after the heal ...
        self.app.reopen = False              # ... and is not opened after the ask
        code, out, err, _ = self.run_ps("--wait-start", "1")
        self.assertEqual(code, 3, out + err)
        self.assertNotIn(ru.VERDICT_OK, out)
        self.assertEqual(len(self.finish_log), 2, "the third finish ran without a start "
                         "after the second ask")


# ----------------------------------------------------------------- R12 ------
class RepairNeverMovesAnotherStoresLoadedPair(PostStart):
    def test_r12_shared_physical_root_refuses_the_whole_round(self):
        """LAM's root is a link to CC's root. CC loads the real CC/B/O2; LAM loads
        LAM/B/O3, which physically is CC/B/O3, a link to O2: healing it would move
        CC's real, loaded O2."""
        self.empty_lam_source()
        lam_root = os.path.join(self.h.support, LAM)

        def share_root():
            for name in os.listdir(lam_root):
                shutil.rmtree(os.path.join(lam_root, name))
            os.rmdir(lam_root)
            os.symlink(CC, lam_root)            # relative: LAM -> claude-code-sessions
            link_pair(self.h, CC, O3, O2)
        self.login_hooks.append(share_root)
        self.post_pair = {CC: (B, O2), LAM: (B, O3)}
        code, out, err, _ = self.run_ps()
        self.assertTrue(self.real_dir(CC, O2),
                        "CC's real, loaded pair was moved by the repair")
        self.assertTrue(os.path.islink(os.path.join(self.h.support, CC, B, O3)))
        self.assertEqual(code, 3, out + err)
        self.assertNotIn(ru.VERDICT_OK, out)
        self.assertEqual(len([h for h in self.heal_log if h["verdict"] == "healed"]), 0)


if __name__ == "__main__":
    unittest.main()
