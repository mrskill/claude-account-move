"""The active pair <store>/<account>/<org> must be a real directory.

After an account move the active pair was a symlink chain. The app reads
through it, but every write fails with ENOTDIR (main.log: "Failed to save
session local_...: ENOTDIR: not a directory ... at ensureStorageDir"), so new
cards were silently lost. This module finds such pairs, plans the heal (remove
the link, move the real directory R to the active path, leave a symlink at R's
old path that points to the active path) and applies it for all stores or for
none.
"""
import os
import re
import time

from . import store as st
from .common import STORES, UUID_RE, inside

ENOTDIR_MAX_AGE_S = 2 * 24 * 3600
FROM_LOG_MAX_AGE_S = 10 * 60
LOG_TAIL_BYTES = 8 * 1024 * 1024
_LINE_TS = re.compile(r"^\s*\[?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")
_PAIR_PATH = re.compile(r"(claude-code-sessions|local-agent-mode-sessions)/"
                        r"([0-9a-fA-F-]{36})/([0-9a-fA-F-]{36})")


_LOG_KEYS = ("enotdir", "save", "saved", "read", "load")


def _log_text(paths):
    try:
        with open(paths.main_log, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - LOG_TAIL_BYTES))
            return fh.read().decode("utf-8", "ignore")
    except OSError:
        return ""


def log_pair(paths):
    """(acc, org) of the newest save/read/ENOTDIR line that names a sessions
    store path and is newer than the newest "account changed" line; or None."""
    cand = None
    for line in _log_text(paths).splitlines():
        low = line.lower()
        if "account changed" in low:
            cand = None
            continue
        if not any(k in low for k in _LOG_KEYS):
            continue
        hits = [(a.lower(), o.lower()) for _s, a, o in _PAIR_PATH.findall(line)
                if UUID_RE.match(a) and UUID_RE.match(o)]
        if hits:
            cand = hits[-1]
    return cand


def _orgs_under(paths, acc):
    orgs = set()
    for store in STORES:
        base = os.path.join(paths.support, store, acc)
        try:
            names = os.listdir(base)
        except OSError:
            continue
        orgs.update(n.lower() for n in names if UUID_RE.match(n))
    return sorted(orgs)


def active_pair(paths, identity, override=None):
    """(pair, source, problem). The pair is --active-pair, else the pair the app
    log names last (when its account is the app account), else the app account
    with an organization that exists under it in the store (the CLI one when it
    exists there, else the only one). When the sources disagree: pair None and
    a problem text naming them."""
    if override:
        return st.parse_override(override), "--active-pair", None
    acc = identity["app"]["value"]
    acc = acc.lower() if acc else None
    cli_org = identity.get("cli_org")
    cli_org = cli_org.lower() if cli_org else None
    lp = log_pair(paths)
    if lp and (acc is None or lp[0] == acc):
        return lp, "app log", None
    if not acc:
        return None, None, ("the app account is not readable and the app log names "
                            "no usable pair")
    orgs = _orgs_under(paths, acc)
    if cli_org and cli_org in orgs:
        return (acc, cli_org), "app account + CLI organization", None
    if len(orgs) == 1:
        return (acc, orgs[0]), "app account + its only organization", None
    return None, None, (
        "sources disagree, not guessing: app account %s; CLI organization %s; "
        "organizations that exist under that account: %s; app log pair: %s. "
        "Pass --active-pair ACCOUNT/ORG"
        % (acc, cli_org or "none", ", ".join(orgs) or "none",
           "%s/%s" % lp if lp else "none"))


def pair_path(paths, store, pair):
    return os.path.join(paths.support, store, pair[0], pair[1])


def symlinked(paths, pair):
    """[(store, path)] of active pairs that are symlinks."""
    return [(s, pair_path(paths, s, pair)) for s in STORES
            if os.path.islink(pair_path(paths, s, pair))]


def plan_store(paths, store, pair):
    """One dict per store: state is real, absent, heal or refuse."""
    a = pair_path(paths, store, pair)
    row = {"store": store, "path": a, "state": None, "target": None, "reason": None}
    if not os.path.islink(a):
        row["state"] = "real" if os.path.isdir(a) else "absent"
        if os.path.lexists(a) and not os.path.isdir(a):
            row["state"], row["reason"] = "refuse", "the active path is not a directory"
        return row
    row["link"] = os.readlink(a)
    root = os.path.realpath(os.path.join(paths.support, store))
    target = os.path.realpath(a)
    row["target"] = target
    if not os.path.isdir(target):
        row.update(state="refuse", reason="the link chain ends nowhere (target %s "
                   "is missing or not a directory)" % target)
    elif not inside(target, root) or target == root:
        row.update(state="refuse", reason="the link resolves outside this store "
                   "(%s); outside the store %s, nothing is moved" % (target, root))
    elif inside(os.path.join(os.path.realpath(os.path.dirname(a)),
                             os.path.basename(a)), target):
        row.update(state="refuse", reason="the target %s contains the active path" % target)
    else:
        row["state"] = "heal"
    return row


def plan(paths, pair):
    return [plan_store(paths, s, pair) for s in STORES]


def _heal_one(row):
    a, r = row["path"], row["target"]
    os.unlink(a)
    try:
        os.rename(r, a)
    except OSError:
        os.symlink(row["link"], a)
        raise
    try:
        os.symlink(os.path.relpath(a, os.path.dirname(r)), r)
    except OSError:
        os.rename(a, r)
        os.symlink(row["link"], a)
        raise


def _undo_one(row):
    a, r = row["path"], row["target"]
    os.unlink(r)
    os.rename(a, r)
    os.symlink(row["link"], a)


def apply(rows):
    """Heal every row in state heal; undo the finished ones when one fails.

    Returns (done_rows, error text or None).
    """
    done = []
    for row in rows:
        if row["state"] != "heal":
            continue
        try:
            _heal_one(row)
        except OSError as exc:
            for d in reversed(done):
                try:
                    _undo_one(d)
                except OSError:
                    pass
            return [], "%s: %s: %s" % (row["store"], row["path"], exc)
        done.append(row)
    return done, None


def recent_enotdir(paths, now=None, max_age_s=ENOTDIR_MAX_AGE_S):
    """[(store, path, line)] of ENOTDIR lines of the last 2 days in main.log
    that name a sessions-store pair which is still a symlink."""
    now = time.time() if now is None else now
    text = _log_text(paths)
    found, seen = [], set()
    for line in text.splitlines():
        if "ENOTDIR" not in line:
            continue
        m = _LINE_TS.match(line)
        if not m:
            continue
        try:
            ts = time.mktime(time.strptime(m.group(1).replace("T", " "),
                                           "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            continue
        if now - ts >= max_age_s:
            continue
        for store, acc, org in _PAIR_PATH.findall(line):
            if not (UUID_RE.match(acc) and UUID_RE.match(org)):
                continue
            p = pair_path(paths, store, (acc, org))
            if p not in seen and os.path.islink(p):
                seen.add(p)
                found.append((store, p, line.strip()[:160]))
    return found


def plan_from_log(paths, now=None):
    """Rows for every pair named by an ENOTDIR line of the last 10 minutes that
    is still a symlink."""
    rows = []
    for store, path, _line in recent_enotdir(paths, now, FROM_LOG_MAX_AGE_S):
        acc, org = path.split(os.sep)[-2:]
        rows.append(plan_store(paths, store, (acc.lower(), org.lower())))
    return rows
