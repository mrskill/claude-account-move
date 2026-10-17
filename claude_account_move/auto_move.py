"""auto: the whole move in one run, for an owner who does not use a terminal.

Order (stops at the first failure with a plain Russian message):
  1. the Claude app must be closed (waits for it);
  2. prepare: readiness checks and the "before" snapshot (must be ready);
  3. write probe: write, read back and delete a small file in the folder the
     app writes cards into (always cleaned up);
  4. stamp --apply: last-message date into the titles (journal kept for undo);
  5. wait for the new login in the app; if the CLI still names the old
     account, run the CLI login in this very window;
  6. finish; when only names missing in the new pair are wrong, sync --apply
     (adds only, journaled) and finish once more;
  7. a summary in plain Russian and the verdict line.

Writes stay where they were: the state directory, and `stamp`/`sync` apply.
The only new write into the storage is the probe file, removed at once.
The steps are the existing cmd_* functions of cli.py run quietly; their
reports are collected through args.capture. Texts: ru_text.py.
"""
import argparse
import os
import re
import secrets
import shlex
import subprocess
import time

from . import common
from . import panel_snapshot as snap
from . import ru_text as ru
from . import store as st
from .common import (EXIT_FAIL, EXIT_INPUT, EXIT_NO_BASELINE, EXIT_LOGIN_TIMEOUT, EXIT_OK,
                     EXIT_WAIT, STORES, read_identity, state_dir_problem,
                     write_lock)

PROBE_NAME = ".claude-account-move-write-probe"   # never matches local_*.json
CLOSE_POLL_S = 2.0
LOGIN_POLL_S = 2.0
CLI_SETTLE_MIN_S = 3.0      # how long to watch for the CLI account after a login run
CLI_SETTLE_MAX_S = 15.0
CLI_LOGIN_FLOOR_S = 1.0     # least time a CLI login run is given
# Tried in this order; the first finishes by itself after the login.
LOGIN_COMMANDS = (("claude", "auth", "login"), ("claude", "/login"))
ENTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                     "claude-account-move")


# ---------------------------------------------------------------- helpers ---

def _poll(default, env):
    try:
        return float(os.environ.get(env, default))
    except ValueError:
        return default


def _ns(args, **extra):
    """Arguments for one of the cmd_* steps: quiet, reports collected."""
    base = dict(home=args.home, state_dir=args.state_dir, json=False,
                heartbeat=args.heartbeat, sync_max_age_min=args.sync_max_age_min,
                active_pair=args.active_pair, quiet=True, capture=[])
    base.update(extra)
    return argparse.Namespace(**base)


def run_step(fn, ns):
    """(exit code, last report of the step or None)."""
    code = fn(ns)
    return code, (ns.capture[-1] if ns.capture else None)


def _read_back(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def probe_write(directory, support_real):
    """(status, detail). status: ok | outside | failed.

    Writes a fresh token to a file the app does not read (its name is not
    local_*.json), reads it back, deletes it. The deletion happens on every
    path, including a failed read-back; a file that cannot be deleted is a
    failure by itself.
    """
    real = os.path.realpath(directory)
    store_root = os.path.realpath(os.path.join(support_real, STORES[0]))
    if not os.path.isdir(real) or not common.inside(real, store_root):
        return "outside", real
    path = os.path.join(real, PROBE_NAME)
    token = secrets.token_hex(16)
    if os.path.lexists(path):
        if os.path.islink(path) or not os.path.isfile(path):
            # not ours: leave it alone, say so
            return "failed", "the probe name is taken by something that is not a file"
        os.unlink(path)                  # a regular leftover of an interrupted run
    errors = []
    created = False
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True                   # from here on the file is ours
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
            fh.flush()
            os.fsync(fh.fileno())
        if _read_back(path) != token:
            errors.append("the file read back differs from what was written")
    except OSError as exc:
        errors.append("%s: %s" % (type(exc).__name__, exc))
    finally:
        if created:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append("the probe file could not be deleted: %s" % exc)
    if created and not errors and os.path.lexists(path):
        errors.append("the probe file is still there after deleting it")
    return ("failed", "; ".join(errors)) if errors else ("ok", "")


def wait_app_closed(limit_s, poll_s, out):
    """True once the Claude app is not running; False when the time is up."""
    if not common.claude_app_running():
        return True
    out.say(ru.APP_RUNNING)
    t0 = time.monotonic()
    deadline = t0 + max(0.0, limit_s)
    last = -1
    while time.monotonic() < deadline:
        time.sleep(min(poll_s, max(0.01, deadline - time.monotonic())))
        if not common.claude_app_running():
            out.say(ru.APP_CLOSED)
            return True
        tick = int((time.monotonic() - t0) // 30)
        if tick != last:
            out.say(ru.APP_WAIT % max(0, int(deadline - time.monotonic())))
            last = tick
    return False


def wait_app_switch(cli, paths, source, allow_same, limit_s, poll_s, out):
    """(identity, reason). reason None or "cli_lags": the app has the new account."""
    t0 = time.monotonic()
    deadline = t0 + max(0.0, limit_s)
    last = -1
    while True:
        ident = read_identity(paths)
        reason = cli.login_reason(ident, source, allow_same)
        if reason in (None, "cli_lags"):
            if read_identity(paths) == ident:         # drift check
                return ident, reason
            reason = "identity_invalid"               # still changing: deadline and sleep apply
        if time.monotonic() >= deadline:
            return ident, reason
        tick = int((time.monotonic() - t0) // 30)
        if tick != last:
            out.say(ru.LOGIN_WAIT % max(0, int(deadline - time.monotonic())))
            last = tick
        time.sleep(min(poll_s, max(0.05, deadline - time.monotonic())))


CLI_LOGIN_TIMEOUT = "timeout"     # run_cli_login result: the login did not end in time


def run_cli_login(cmd, timeout=None):
    """Run the CLI login interactively in this window. Returns the exit code,
    None when the command does not exist, or CLI_LOGIN_TIMEOUT when it did not
    finish within `timeout` seconds (the child is killed). Tests replace this."""
    try:
        return subprocess.run(list(cmd), timeout=timeout).returncode
    except subprocess.TimeoutExpired:      # run() has already killed and reaped the child
        return CLI_LOGIN_TIMEOUT
    except OSError:
        return None


def find_unfinished_move(paths, current_app_account):
    """Look at the newest move directory only. Returns one of:

      None                          no move dirs, or the newest is finished, or
                                    it is a failed prepare, or its source is
                                    the account the app has now: prepare anew;
      ("resume", move_id, dir, src) unfinished, usable, source differs from the
                                    current app account: continue it;
      ("blocked", move_id, problem) unfinished but unusable (too old, damaged):
                                    the owner decides, `auto` must not guess.
    """
    root = snap.moves_dir(paths)
    if os.path.islink(root) or not os.path.isdir(root):
        return None
    names = sorted((n for n in os.listdir(root) if snap.MOVE_ID_RE.match(n)),
                   reverse=True)
    if not names:
        return None
    name = names[0]
    mdir = os.path.join(root, name)
    rep, err = common.read_json(os.path.join(mdir, "report-finish.json"))
    if not err and isinstance(rep, dict) and rep.get("exit_code") == EXIT_OK:
        return None                               # finished: a new move starts
    before, man, move_id, problem = snap.load_baseline(paths, name, allow_unready=True)
    if man is not None and not man.get("ready"):
        return None                               # a prepare that was not ready
    if problem:
        return ("blocked", name, problem)
    source = (before.get("identity") or {}).get("app_account")
    if source and source != current_app_account:
        return ("resume", move_id, mdir, source)
    return None


# Store counters that must be zero for a missing-names failure to be repairable
# by adding cards: nothing is lost, only not delivered to the new pair yet.
NO_LOSS_FIELDS = ("names_missing_anywhere", "transcripts_gone_since_snapshot",
                  "empty_cliSessionId_with_transcript", "target_copy_empty_cid",
                  "target_copy_wrong_cid", "dirs_missing", "aliases_lost",
                  "tombstone_missing_in_union")
SYNC_FIXABLE = ("target_names", "lagging", "loss")   # lagging also folds into loss


def syncable(rep):
    """True when finish failed only because cards are not in the new pair yet.

    A missing-names failure shows up as target_names, lagging and loss at once
    (loss folds the lagging directories in), so the checks alone cannot say
    whether anything is really lost: the per-store counters decide.
    """
    failed = [c["id"] for c in (rep.get("checks") or []) if not c["ok"]]
    if not any(i.endswith(":target_names") for i in failed):
        return False
    if not all(":" in i and i.split(":", 1)[1] in SYNC_FIXABLE for i in failed):
        return False
    stores = rep.get("stores") or {}
    return bool(stores) and all(not s.get(f) for s in stores.values()
                                for f in NO_LOSS_FIELDS)


def _undo_cmd(kind, journal, args):
    parts = [ENTRY, kind, "--undo", journal]
    if args.state_dir:
        parts += ["--state-dir", args.state_dir]
    if args.home:
        parts += ["--home", args.home]
    return " ".join(shlex.quote(p) for p in parts)


# ---------------------------------------------------------------- summary ---

def _ratio(detail, pattern):
    m = re.search(pattern, detail)
    return (int(m.group(1)), int(m.group(2))) if m else None


def describe_check(c, rep):
    """(ok, Russian line) for one check of the finish report."""
    cid, ok, detail = c["id"], c["ok"], c["detail"]
    num = ""
    if ":" in cid:
        store, sub = cid.split(":", 1)
        srep = (rep.get("stores") or {}).get(store) or {}
        if sub == "target_names" and srep.get("names_before") is not None:
            num = ": " + ru.OF % (srep.get("names_in_target") or 0, srep["names_before"])
        elif sub == "loss":
            r = _ratio(detail, r"(\d+) of (\d+) names present anywhere")
            if r:
                num = ": " + ru.OF % r
        line = "%s, %s%s" % (ru.STORE_NAMES.get(store, store),
                             ru.FINISH_STORE_CHECKS.get(sub, sub), num)
    else:
        if cid == "transcripts":
            r = _ratio(detail, r"(\d+) of (\d+) recorded transcripts")
            if r:
                num = ": " + ru.OF % r
        elif cid == "tasks":
            r = _ratio(detail, r"(\d+) of (\d+) recorded scheduled tasks missing")
            if r:
                num = ": " + ru.OF % (r[1] - r[0], r[1])
        line = ru.FINISH_CHECKS.get(cid, cid) + num
    if not ok:
        line += " (%s)" % detail
    return ok, line


def print_summary(out, args, ctx):
    out.say("")
    out.say("=" * 60)
    out.say(ru.SUMMARY_TITLE)
    out.say("=" * 60)
    out.say(ru.CHECKED)
    rep = ctx.get("finish")
    sr = ctx.get("stamp")
    if sr:
        if sr.get("exit_code") == EXIT_OK:
            stats = sr.get("stats") or {}
            written = (sr.get("result") or {}).get("written", 0)
            out.say("  [%s] %s" % (
                ru.TAG_OK, ru.LINE_STAMP % (written, stats.get("cards", 0))))
        else:
            why = sr.get("reason") or sr.get("verdict") or ru.PROBLEM_STEP
            out.say("  [%s] %s" % (ru.TAG_BAD, ru.LINE_STAMP_FAILED % why))
    if rep and rep.get("checks"):
        for c in rep["checks"]:
            ok, line = describe_check(c, rep)
            out.say("  [%s] %s" % (ru.TAG_OK if ok else ru.TAG_BAD, line))
        for n in rep.get("notes") or []:
            out.say("  " + ru.LINE_NOTE % n)
    else:
        out.say("  " + ru.LINE_NO_FINISH)
    out.say("")
    out.say(ru.FILES)
    if ctx.get("move_dir"):
        out.say("  " + ru.LINE_SNAPSHOT % ctx["move_dir"])
    if ctx.get("stamp_journal"):
        out.say("  " + ru.LINE_STAMP_JOURNAL % ctx["stamp_journal"])
    if ctx.get("sync_journal"):
        out.say("  " + ru.LINE_SYNC_JOURNAL % ctx["sync_journal"])
    out.say("  " + ru.LINE_STATE_DIR % ctx["state_dir"])
    out.say("")
    out.say(ru.UNDO_TITLE)
    if ctx.get("stamp_journal") or ctx.get("sync_journal"):
        out.say("  " + ru.UNDO_CLOSE_FIRST)
        if ctx.get("sync_journal"):
            out.say("  " + ru.UNDO_SYNC)
            out.say("    " + _undo_cmd("sync", ctx["sync_journal"], args))
        if ctx.get("stamp_journal"):
            out.say("  " + ru.UNDO_STAMP)
            out.say("    " + _undo_cmd("stamp", ctx["stamp_journal"], args))
    else:
        out.say("  " + ru.UNDO_NOTHING)
    if ctx.get("synced"):
        out.say("")
        out.say(ru.RESTART_APP)
    out.say("")
    out.say(ru.VERDICT_BAD % ctx["problem"] if ctx.get("problem") else ru.VERDICT_OK)


# ------------------------------------------------------------------- main ---

def cmd_auto(args, cli):
    started = time.monotonic()
    out = cli.Out.of(args)
    paths = cli.make_paths(args)
    steps = []
    ctx = {"state_dir": paths.state}

    def step(name, ok, **extra):
        steps.append(dict(step=name, ok=bool(ok), **extra))

    def stop(code, verdict, message, show_summary=False, **extra):
        out.say(message)
        if show_summary:
            ctx["problem"] = ctx.get("problem") or message
            print_summary(out, args, ctx)
        cli.emit(args, out, cli._report(
            "auto", code, verdict, started, reason=message, steps=steps,
            write_verified=False, move_id=ctx.get("move_id"),
            stamp_journal=ctx.get("stamp_journal"),
            sync_journal=ctx.get("sync_journal"), **extra), ctx.get("move_dir"))
        return code

    def flow():
        prob = state_dir_problem(paths)
        if prob:
            return stop(EXIT_INPUT, "input_error", "error: " + prob)
        if args.active_pair:
            st.parse_override(args.active_pair)
        if not os.path.isdir(paths.support):
            return stop(common.EXIT_NO_STORE, "no_store", ru.NO_STORE % paths.support)

        out.say(ru.TITLE)

        # 1. the app must be closed
        if not wait_app_closed(args.wait_close,
                               _poll(CLOSE_POLL_S, "CLAUDE_ACCOUNT_MOVE_CLOSE_POLL_S"), out):
            step("app_closed", False)
            return stop(EXIT_FAIL, "app_running", ru.APP_TIMEOUT)
        step("app_closed", True)

        # 2-4 are skipped when an earlier move was left unfinished (the app is
        # already in the new account): its snapshot and stamp stay, and we go on
        # to the login wait and finish with the same move_id.
        try:
            current = read_identity(paths)["app"]["value"]
        except (KeyError, TypeError):
            current = None
        found = (None if args.new_move or not current
                 else find_unfinished_move(paths, current))
        if found and found[0] == "blocked":
            step("resume", False, move_id=found[1], problem=found[2])
            return stop(EXIT_NO_BASELINE, "unfinished_move_unusable",
                        ru.UNFINISHED_BLOCKED % (found[1], found[2]))
        resumed = found[1:] if found else None
        if resumed:
            ctx["move_id"], ctx["move_dir"], source = resumed
            step("resume", True, move_id=ctx["move_id"])
            out.say(ru.RESUME_MOVE % ctx["move_dir"])
        else:
            # 2. prepare
            out.say(ru.PREPARE_START)
            code, rep = run_step(cli.cmd_prepare, _ns(args, wait_sync=0.0, full_copy=False))
            if rep and rep.get("move_id"):
                ctx["move_id"] = rep["move_id"]
                ctx["move_dir"] = os.path.join(snap.moves_dir(paths), rep["move_id"])
            if code != EXIT_OK or not rep or not rep.get("ready"):
                step("prepare", False, exit_code=code)
                lines = [ru.PREPARE_FAIL]
                if rep and rep.get("checks"):
                    for c in rep["checks"]:
                        if not c["ok"]:
                            lines.append("  - %s (%s)" % (ru.PREPARE_CHECKS.get(c["id"], c["id"]),
                                                          c["detail"]))
                elif rep:
                    lines.append("  - %s" % rep.get("reason", rep.get("verdict")))
                lines.append(ru.PREPARE_FAIL_END)
                return stop(code if code != EXIT_OK else EXIT_FAIL,
                            (rep or {}).get("verdict", "not_ready"), "\n".join(lines),
                            checks=(rep or {}).get("checks"))
            source = rep["accounts"]["app"]
            step("prepare", True, move_id=ctx["move_id"])
            out.say(ru.PREPARE_OK % ctx["move_dir"])

            # 3. real write probe
            out.say(ru.PROBE_START)
            ident = read_identity(paths)
            pair = st.live_pair(paths, account=ident["app"]["value"] if args.active_pair else None,
                                override=args.active_pair)
            target_dir = st.pair_dir(paths, pair[0], pair[1]) if pair else None
            if not target_dir:
                step("write_probe", False, status="no_dir")
                return stop(EXIT_FAIL, "probe_failed", ru.PROBE_NO_DIR)
            with write_lock(paths):
                status, detail = probe_write(target_dir, os.path.realpath(paths.support))
            step("write_probe", status == "ok", status=status, detail=detail)
            if status == "outside":
                return stop(EXIT_FAIL, "probe_failed", ru.PROBE_OUTSIDE % detail)
            if status != "ok":
                return stop(EXIT_FAIL, "probe_failed", ru.PROBE_FAIL % detail)
            out.say(ru.PROBE_OK)

            # 4. stamp
            if args.no_stamp:
                out.say(ru.STAMP_SKIPPED)
                step("stamp", True, skipped=True)
            else:
                out.say(ru.STAMP_START)
                code, srep = run_step(cli.cmd_stamp, _ns(args, apply=True, undo=None))
                ctx["stamp"] = srep
                ctx["stamp_journal"] = (srep or {}).get("journal")
                if code != EXIT_OK:
                    step("stamp", False, exit_code=code)
                    ctx["problem"] = ru.PROBLEM_STEP
                    why = (srep or {}).get("reason") or (srep or {}).get("verdict")
                    return stop(code, "stamp_failed",
                                ru.STAMP_FAIL % (code, why, ctx["stamp_journal"] or ru.NONE_WORD),
                                show_summary=True)
                step("stamp", True, journal=ctx["stamp_journal"])
                out.say(ru.STAMP_OK % ((srep.get("result") or {}).get("written", 0),
                                       ctx["stamp_journal"]))

        # 5. wait for the new login
        out.say(ru.LOGIN_START)
        out.say(ru.LOGIN_ASK)
        deadline = time.monotonic() + max(0.0, args.wait_login)
        poll = _poll(LOGIN_POLL_S, "CLAUDE_ACCOUNT_MOVE_LOGIN_POLL_S")
        ident, reason = wait_app_switch(cli, paths, source, args.allow_same_account,
                                        args.wait_login, poll, out)
        if reason is None:
            out.say(ru.LOGIN_APP_OK)
        elif reason == "cli_lags":
            out.say(ru.LOGIN_APP_OK)
            quiet = cli.Out(False, quiet=True)
            # One grace period in total, not one per attempt, past the login deadline.
            hard_deadline = deadline + CLI_SETTLE_MIN_S
            for cmd in LOGIN_COMMANDS:
                out.say(ru.LOGIN_CLI)
                # Bounded by what is left of the login budget; never below a floor,
                # so a spent budget still gives the command a short bounded try.
                budget = max(CLI_LOGIN_FLOOR_S, hard_deadline - time.monotonic())
                rc = run_cli_login(cmd, timeout=budget)
                if rc == CLI_LOGIN_TIMEOUT:
                    reason = "cli_login_timeout"
                    break
                if rc is None:
                    out.say(ru.LOGIN_NO_CLAUDE)
                    continue
                left = min(CLI_SETTLE_MAX_S, max(0.0, hard_deadline - time.monotonic()))
                ident, reason, _ = cli.wait_for_login(
                    paths, source, args.allow_same_account, left, poll, quiet)
                if reason is None:
                    out.say(ru.LOGIN_CLI_OK)
                    break
            else:
                left = max(0.0, deadline - time.monotonic())
                ident, reason, _ = cli.wait_for_login(
                    paths, source, args.allow_same_account, left, poll, quiet)
        step("login", reason is None, reason=reason)
        if reason is not None:
            ctx["problem"] = ru.LOGIN_REASON.get(reason, reason)
            return stop(EXIT_LOGIN_TIMEOUT, "login_timeout",
                        ru.LOGIN_TIMEOUT % ru.LOGIN_REASON.get(reason, reason),
                        show_summary=True, login_reason=reason)

        # 6. finish (and sync when only deliverable names are missing)
        out.say(ru.FINISH_START)

        def finish_once():
            return run_step(cli.cmd_finish, _ns(
                args, wait_login=30.0, wait_sync=0.0, move_id=ctx["move_id"],
                allow_same_account=args.allow_same_account, allow_unready_baseline=False, max_age_hours=72.0))

        fcode, frep = finish_once()
        if fcode == EXIT_FAIL and frep and syncable(frep):
            out.say(ru.SYNC_START)
            scode, srep2 = run_step(cli.cmd_sync, _ns(args, apply=True, undo=None))
            ctx["sync_journal"] = (srep2 or {}).get("journal")
            ctx["synced"] = bool((srep2 or {}).get("copied"))
            step("sync", scode == EXIT_OK, exit_code=scode, copied=(srep2 or {}).get("copied"))
            if scode != EXIT_OK:
                ctx["finish"] = frep
                ctx["problem"] = ru.PROBLEM_STEP
                why = (srep2 or {}).get("reason") or (srep2 or {}).get("verdict")
                return stop(scode, "sync_failed", ru.SYNC_FAIL % (scode, why),
                            show_summary=True)
            fcode, frep = finish_once()
        ctx["finish"] = frep
        step("finish", fcode == EXIT_OK, exit_code=fcode)
        if fcode == EXIT_OK:
            code, verdict = EXIT_OK, "ok"
        else:
            ctx["problem"] = (ru.PROBLEM_FINISH_WAIT if fcode == EXIT_WAIT
                              else ru.PROBLEM_FINISH)
            code, verdict = (EXIT_WAIT if fcode == EXIT_WAIT else EXIT_FAIL), "fail"
        print_summary(out, args, ctx)
        cli.emit(args, out, cli._report(
            "auto", code, verdict, started, steps=steps, write_verified=False,
            move_id=ctx["move_id"], stamp_journal=ctx.get("stamp_journal"),
            sync_journal=ctx.get("sync_journal"), move_dir=ctx["move_dir"],
            finish=frep), ctx["move_dir"])
        return code

    try:
        return flow()
    except st.PairError as exc:
        return stop(EXIT_INPUT, "input_error", ru.BAD_PAIR % exc)
