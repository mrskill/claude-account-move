"""Command line: prepare, finish, sync.

prepare  before signing out: readiness checks, "before" snapshot, one pass.
finish   after signing in to the app and running `claude /login`: waits until
         the CLI account equals the app account, then verifies against the
         snapshot of THIS move.
sync     optional repair: copy missing cards into the target pair (writes).
"""
import argparse
import json
import os
import sys
import time

from . import __version__
from . import panel_snapshot as snap
from . import post_move_check as post
from . import pre_move_check as pre
from . import store as st
from . import post_move_verify as verify
from . import sync_cards
from .common import (EXIT_FAIL, EXIT_INPUT, EXIT_INTERNAL, EXIT_LOGIN_TIMEOUT,
                     EXIT_NO_BASELINE, EXIT_NO_STORE, EXIT_OK, EXIT_WAIT,
                     ENV_STATE, Paths, read_identity, safe_subdir,
                     state_dir_problem, write_json)

SYNC_POLL_S = 10.0
LOGIN_POLL_S = 2.0


def _poll(default, env):
    try:
        return float(os.environ.get(env, default))
    except ValueError:
        return default


class Out(object):
    """Human lines go to stdout, or to stderr when --json owns stdout."""

    def __init__(self, as_json):
        self.stream = sys.stderr if as_json else sys.stdout

    def say(self, msg=""):
        print(msg, file=self.stream)


def add_common(ap):
    ap.add_argument("--home", help="home directory to inspect (default: the HOME variable)")
    ap.add_argument("--state-dir", help="state directory (default: the %s "
                    "variable, else .claude-account-move in the home directory)"
                    % ENV_STATE)
    ap.add_argument("--json", action="store_true",
                    help="print exactly one JSON report on stdout")
    ap.add_argument("--heartbeat", default=os.environ.get(
        "CLAUDE_ACCOUNT_MOVE_SYNC_HEARTBEAT"),
        help="optional: file your own sync job touches on every run")
    ap.add_argument("--sync-max-age-min", type=float, default=pre.SYNC_MAX_AGE_MIN)
    ap.add_argument("--active-pair", help="ACCOUNT/ORG the app writes into, "
                    "when the app log cannot tell")


class JsonArgumentParser(argparse.ArgumentParser):
    """Usage errors exit 2 and, with --json on the command line, print a report."""

    argv = ()

    def error(self, message):
        self.print_usage(sys.stderr)
        if "--json" in self.argv:
            cmd = next((a for a in self.argv if not a.startswith("-")), None)
            print(json.dumps({"schema": "claude-account-move/report/1",
                              "command": cmd, "exit_code": EXIT_INPUT,
                              "verdict": "input_error", "reason": message,
                              "write_verified": False}, indent=1, sort_keys=True))
        else:
            print("%s: error: %s" % (self.prog, message), file=sys.stderr)
        raise SystemExit(EXIT_INPUT)


def build_parser():
    ap = JsonArgumentParser(prog="claude-account-move", description=__doc__,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version",
                    version="claude-account-move %s" % __version__)
    sub = ap.add_subparsers(dest="command")
    p = sub.add_parser("prepare", help="readiness checks and the 'before' snapshot")
    add_common(p)
    p.add_argument("--wait-sync", type=float, default=0.0,
                   help="seconds to wait while only the sync heartbeat is red")
    p.add_argument("--full-copy", action="store_true",
                   help="also copy all card files into the snapshot (large)")
    f = sub.add_parser("finish", help="wait for login, then verify against the snapshot")
    add_common(f)
    f.add_argument("--wait-login", type=float, default=900.0,
                   help="seconds to wait for the CLI to match the app (default 900)")
    f.add_argument("--wait-sync", type=float, default=600.0,
                   help="seconds to wait for a live sync job to deliver names")
    f.add_argument("--allow-same-account", action="store_true",
                   help="the move keeps the same account (re-login)")
    f.add_argument("--move-id", help="use this snapshot instead of the newest ready one")
    f.add_argument("--allow-unready-baseline", action="store_true",
                   help="diagnostic: accept a snapshot that was not ready")
    f.add_argument("--max-age-hours", type=float, default=72.0)
    s = sub.add_parser("sync", help="copy missing cards into the target pair (writes)")
    add_common(s)
    s.add_argument("--apply", action="store_true", help="really copy (default: dry run)")
    s.add_argument("--undo", metavar="JOURNAL", help="remove files a journal lists")
    return ap


def make_paths(args):
    return Paths(home=args.home, state=args.state_dir)


def _report(cmd, code, verdict, started, **extra):
    rep = {"schema": "claude-account-move/report/1", "command": cmd,
           "exit_code": code, "verdict": verdict,
           "elapsed_ms": int((time.monotonic() - started) * 1000)}
    rep.update(extra)
    return rep


def emit(args, out, rep, move_dir=None):
    name = "report-%s.json" % rep["command"]
    if move_dir and os.path.isdir(move_dir) and not os.path.islink(move_dir):
        try:
            write_json(os.path.join(move_dir, name), rep)
        except OSError as exc:
            print("warning: could not write report: %s" % exc, file=sys.stderr)
    if args.json:
        print(json.dumps(rep, indent=1, sort_keys=True, ensure_ascii=False))


def fail(args, out, cmd, started, code, verdict, message, move_dir=None, **extra):
    """Say why, emit the JSON report if asked, return the exit code."""
    out.say(message)
    emit(args, out, _report(cmd, code, verdict, started, reason=message,
                            write_verified=False, **extra), move_dir)
    return code


def print_checks(out, checks):
    for c in checks:
        out.say("  %-5s %-28s %s" % ("ok" if c["ok"] else "FAIL",
                                     c.get("name") or c["id"], c["detail"]))


def cmd_prepare(args):
    started = time.monotonic()
    out = Out(args.json)
    paths = make_paths(args)
    prob = state_dir_problem(paths)
    if prob:
        return fail(args, out, "prepare", started, EXIT_INPUT, "input_error",
                    "error: " + prob)
    if args.active_pair:
        st.parse_override(args.active_pair)      # PairError -> exit 2, before any storage check
    if not os.path.isdir(paths.support):
        return fail(args, out, "prepare", started, EXIT_NO_STORE, "no_store",
                    "storage not found: %s" % paths.support)
    poll = _poll(SYNC_POLL_S, "CLAUDE_ACCOUNT_MOVE_POLL_S")
    deadline = time.monotonic() + max(0.0, args.wait_sync)
    while True:
        index = st.scan(paths)
        if not index.any_pairs():
            return fail(args, out, "prepare", started, EXIT_NO_STORE, "no_store",
                        "no account/organization pair found under %s"
                        % paths.support)
        tmap = st.transcripts(paths)
        identity = read_identity(paths)
        before = snap.build_before(paths, index, tmap, identity)
        checks, active = pre.run_checks(paths, index, tmap, args.heartbeat,
                                        args.sync_max_age_min, args.active_pair,
                                        identity=identity)
        failed = [c["id"] for c in checks if not c["ok"]]
        if failed and set(failed) <= {"sync_agent"} and time.monotonic() < deadline:
            out.say("waiting for the sync job heartbeat ...")
            time.sleep(min(poll, max(0.0, deadline - time.monotonic())))
            continue
        break
    ready = not failed
    try:
        move_id, mdir, copied = snap.write_snapshot(paths, before, ready, index,
                                                    args.full_copy)
    except OSError as exc:
        return fail(args, out, "prepare", started, EXIT_INTERNAL, "internal_error",
                    "cannot write the snapshot: %s" % exc)
    code = EXIT_OK if ready else EXIT_FAIL
    out.say("checks:")
    print_checks(out, checks)
    for name in st.STORES:
        s = index.stores[name]
        out.say("  %s: cards %d, real dirs %d, aliases %d, deletion marks %d"
                % (name, len(s.union_names()), len(s.dirs), len(s.aliases),
                   len(s.tombstone_ids())))
    out.say("snapshot: %s (%s)" % (mdir, "ready" if ready
                                   else "NOT ready, not selected by finish"))
    out.say("VERDICT: %s" % ("ready to move" if ready
                             else "not ready, %d failing checks" % len(failed)))
    rep = _report(
        "prepare", code, "ready" if ready else "not_ready", started,
        move_id=move_id, ready=ready,
        checks=[{"id": c["id"], "ok": c["ok"], "detail": c["detail"]}
                for c in checks],
        accounts={"cli": identity["cli"]["value"], "app": identity["app"]["value"]},
        observation=index.obs.as_dict(), write_verified=False,
        pairs_physical=sum(len(s.dirs) for s in index.stores.values()),
        transcripts=len(tmap), cards_copied=copied)
    emit(args, out, rep, mdir)
    return code


def login_reason(identity, source, allow_same):
    """None when CLI and app agree on a new account, else why not."""
    if identity["cli"]["status"] != "ok" or identity["app"]["status"] != "ok":
        return "identity_invalid"
    if identity["app"]["value"] == source and not allow_same:
        return "app_not_switched"
    if identity["cli"]["value"] != identity["app"]["value"]:
        return "cli_lags"
    return None


def wait_for_login(paths, source, allow_same, limit_s, poll_s, out):
    """(identity, reason, waited seconds). reason None means success."""
    t0 = time.monotonic()
    deadline = t0 + max(0.0, limit_s)
    last_say = -1
    while True:
        ident = read_identity(paths)
        reason = login_reason(ident, source, allow_same)
        if reason is None:
            again = read_identity(paths)       # drift check before success
            if again == ident:
                return ident, None, time.monotonic() - t0
            continue
        if time.monotonic() >= deadline:
            return ident, reason, time.monotonic() - t0
        tick = int((time.monotonic() - t0) // 30)
        if tick != last_say:
            out.say("waiting for login (%s) ..." % reason)
            last_say = tick
        time.sleep(min(poll_s, max(0.05, deadline - time.monotonic())))


def cmd_finish(args):
    started = time.monotonic()
    out = Out(args.json)
    paths = make_paths(args)
    prob = state_dir_problem(paths)
    if prob:
        return fail(args, out, "finish", started, EXIT_INPUT, "input_error",
                    "error: " + prob)
    if args.active_pair:
        st.parse_override(args.active_pair)      # PairError -> exit 2, before any storage check
    if not os.path.isdir(paths.support) or not st.has_pairs(paths):
        return fail(args, out, "finish", started, EXIT_NO_STORE, "no_store",
                    "storage or account/organization pair not found under %s"
                    % paths.support)
    before, man, move_id, problem = snap.load_baseline(
        paths, args.move_id, args.allow_unready_baseline, args.max_age_hours)
    if problem:
        return fail(args, out, "finish", started, EXIT_NO_BASELINE, "no_baseline",
                    "no usable 'before' snapshot: %s" % problem, move_id=move_id)
    source = (before.get("identity") or {}).get("app_account")
    mdir = os.path.join(snap.moves_dir(paths), move_id)
    ident, reason, waited = wait_for_login(
        paths, source, args.allow_same_account, args.wait_login,
        _poll(LOGIN_POLL_S, "CLAUDE_ACCOUNT_MOVE_LOGIN_POLL_S"), out)
    login = {"waited_s": round(waited, 2), "limit_s": args.wait_login,
             "reason": reason}
    accounts = {"cli": ident["cli"]["value"], "app": ident["app"]["value"],
                "target": ident["app"]["value"], "source_from_snapshot": source}
    if reason:
        out.say("login not complete after %.0f s: %s" % (waited, reason))
        out.say({"identity_invalid": "an account file is missing or unreadable",
                 "app_not_switched": "sign in to the app with the new account",
                 "cli_lags": "run `claude /login` in a terminal"}[reason])
        rep = _report("finish", EXIT_LOGIN_TIMEOUT, "login_timeout", started,
                      move_id=move_id, accounts=accounts, login=login,
                      write_verified=False)
        emit(args, out, rep, mdir)
        return EXIT_LOGIN_TIMEOUT
    target = accounts["target"]
    poll = _poll(SYNC_POLL_S, "CLAUDE_ACCOUNT_MOVE_POLL_S")
    deadline = time.monotonic() + max(0.0, args.wait_sync)
    while True:
        index = st.scan(paths)
        if not index.any_pairs():
            return fail(args, out, "finish", started, EXIT_NO_STORE, "no_store",
                        "no account/organization pair found under %s"
                        % paths.support, move_id=move_id)
        tmap = st.transcripts(paths)
        alive = bool(args.heartbeat) and pre.check_sync_agent(
            args.heartbeat, args.sync_max_age_min)[0]
        report, code = post.evaluate(paths, before, index, tmap,
                                     read_identity(paths), target, source, alive,
                                     args.active_pair, args.allow_same_account)
        if code == EXIT_WAIT and time.monotonic() < deadline:
            out.say("waiting for the sync job to deliver ...")
            time.sleep(min(poll, max(0.0, deadline - time.monotonic())))
            continue
        break
    if args.allow_unready_baseline and code == EXIT_OK and not man.get("ready"):
        code = EXIT_FAIL
    out.say("checks:")
    print_checks(out, report["checks"])
    for n in report["notes"]:
        out.say("  note: " + n)
    out.say("VERDICT: %s" % {EXIT_OK: "everything arrived",
                             EXIT_FAIL: "something is missing, see FAIL lines",
                             EXIT_WAIT: "nothing lost, but not delivered yet: "
                             "wait and run finish again"}[code])
    rep = _report("finish", code, {EXIT_OK: "ok", EXIT_FAIL: "fail",
                                   EXIT_WAIT: "wait"}[code], started,
                  move_id=move_id, accounts=accounts, login=login,
                  pairs_physical=sum(len(s.dirs) for s in index.stores.values()),
                  transcripts=len(tmap), **report)
    emit(args, out, rep, mdir)
    return code


def cmd_sync(args):
    started = time.monotonic()
    out = Out(args.json)
    paths = make_paths(args)
    prob = state_dir_problem(paths)
    if prob:
        return fail(args, out, "sync", started, EXIT_INPUT, "input_error",
                    "error: " + prob)
    if args.active_pair:
        st.parse_override(args.active_pair)      # PairError -> exit 2, before any storage check
    if args.undo:
        try:
            res = sync_cards.undo(args.undo, snap.path_id(paths.home),
                                  os.path.realpath(paths.support))
        except sync_cards.JournalError as exc:
            return fail(args, out, "sync", started, EXIT_INPUT, "input_error",
                        "journal rejected: %s" % exc)
        out.say("removed %(removed)d files, skipped %(skipped)d, ambiguous "
                "%(ambiguous)d (same name, not proven to be ours: kept), invalid "
                "%(invalid)d (outside this operation's targets); journal "
                "status: %(status)s" % res)
        code = EXIT_OK if (res["status"] == "ok" and not res["ambiguous"]
                           and not res["invalid"]) else EXIT_FAIL
        emit(args, out, _report("sync", code, "undo", started, undo=res,
                                write_verified=False))
        return code
    if not os.path.isdir(paths.support):
        return fail(args, out, "sync", started, EXIT_NO_STORE, "no_store",
                    "storage not found: %s" % paths.support)
    index = st.scan(paths)
    if not index.any_pairs():
        return fail(args, out, "sync", started, EXIT_NO_STORE, "no_store",
                    "no account/organization pair found under %s" % paths.support)
    if not index.obs.complete():
        return fail(args, out, "sync", started, EXIT_FAIL, "incomplete_observation",
                    "storage could not be read completely (%d unreadable cards, "
                    "%d errors): refusing to plan a repair from partial input"
                    % (index.obs.unreadable_cards, len(index.obs.errors)),
                    observation=index.obs.as_dict())
    ident = read_identity(paths)
    target = ident["app"]["value"]
    if not target:
        return fail(args, out, "sync", started, EXIT_FAIL, "no_target",
                    "the app account is not readable: nothing to sync to")
    pair = st.live_pair(paths, account=target, override=args.active_pair)
    active = st.pair_dir(paths, pair[0], pair[1]) if pair else None
    if active and active not in index.stores[st.STORES[0]].dirs:
        active = None
    for store_name in st.STORES:
        s_ = index.stores[store_name]
        if not s_.union_names():
            continue
        if store_name == st.STORES[0]:
            resolved = active is not None
        else:
            resolved = bool(verify.target_dirs(index, store_name, target)) or bool(pair)
        if not resolved:
            return fail(args, out, "sync", started, EXIT_FAIL, "no_target",
                        "cannot determine the %s folder of the target account "
                        "(the app log names none yet; sign in to the app first or "
                        "pass --active-pair): not reporting success" % store_name)
    plan, conflicts = sync_cards.plan_sync(index, target, active, pair)
    out.say("cards missing in the target pair(s): %d" % len(plan))
    for item in plan[:50]:
        out.say("  would copy %s -> %s" % (item["name"], item["dst_dir"]))
    if len(plan) > 50:
        out.say("  ... and %d more" % (len(plan) - 50))
    for c in conflicts:
        out.say("  conflict, not copied: %s %s has session ids %s"
                % (c["store"], c["name"], ", ".join(c["session_ids"])))
    base = dict(planned=len(plan), conflicts=conflicts, dry_run=not args.apply,
                write_verified=False)
    if not args.apply:
        out.say("dry run: nothing written. Add --apply to copy.")
        code = EXIT_FAIL if conflicts else EXIT_OK
        emit(args, out, _report("sync", code, "dry_run", started, **base))
        return code
    try:
        jdir = safe_subdir(paths)
        journal = sync_cards.new_journal_path(jdir)
        copied, failed = sync_cards.apply_plan(plan, journal, snap.path_id(paths.home))
    except OSError as exc:
        return fail(args, out, "sync", started, EXIT_INTERNAL, "internal_error",
                    "cannot write the journal or copy: %s" % exc)
    out.say("copied %d, failed %d; journal: %s" % (copied, failed, journal))
    out.say("Restart the Claude app so the panel re-reads the cards "
            "(this ends running sessions: save your work first).")
    code = EXIT_FAIL if (failed or conflicts) else EXIT_OK
    emit(args, out, _report("sync", code, "applied", started, copied=copied,
                            failed=failed, journal=journal, **base))
    return code


def main(argv=None):
    JsonArgumentParser.argv = tuple(sys.argv[1:] if argv is None else argv)
    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.command:
        ap.print_help()
        return EXIT_INPUT
    started = time.monotonic()
    try:
        return {"prepare": cmd_prepare, "finish": cmd_finish,
                "sync": cmd_sync}[args.command](args)
    except st.PairError as exc:
        return fail(args, Out(args.json), args.command, started, EXIT_INPUT,
                    "input_error", "error: %s" % exc)
    except KeyboardInterrupt:
        if args.json:
            print(json.dumps(_report(args.command, EXIT_INTERNAL, "interrupted",
                                     started, reason="interrupted",
                                     write_verified=False), indent=1, sort_keys=True))
        return EXIT_INTERNAL
    except Exception as exc:  # last resort: never a silent success
        msg = "internal error: %s: %s" % (type(exc).__name__, exc)
        print(msg, file=sys.stderr)
        if args.json:
            print(json.dumps(_report(args.command, EXIT_INTERNAL, "internal_error",
                                     started, reason=msg, write_verified=False),
                             indent=1, sort_keys=True))
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
