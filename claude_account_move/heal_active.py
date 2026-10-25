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
LOG_TAIL_BYTES = 8 * 1024 * 1024
_LINE_TS = re.compile(r"^\s*\[?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")
_PAIR_PATH = re.compile(r"(claude-code-sessions|local-agent-mode-sessions)/"
                        r"([0-9a-fA-F-]{36})/([0-9a-fA-F-]{36})")


def active_pair(paths, identity, override=None):
    """(acc, org) of the active pair: --active-pair, else app account + CLI
    organization, else the last pair of that account in the app log; or None."""
    if override:
        return st.parse_override(override)
    acc = identity["app"]["value"]
    org = identity.get("cli_org")
    if acc and org:
        return acc.lower(), org.lower()
    if acc:
        hits = st.app_pairs(paths, acc)
        if hits:
            return hits[-1]
    return None


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


def recent_enotdir(paths, now=None):
    """[(store, path, line)] of ENOTDIR lines of the last 2 days in main.log
    that name a sessions-store pair which is still a symlink."""
    now = time.time() if now is None else now
    try:
        with open(paths.main_log, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - LOG_TAIL_BYTES))
            text = fh.read().decode("utf-8", "ignore")
    except OSError:
        return []
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
        if now - ts >= ENOTDIR_MAX_AGE_S:
            continue
        for store, acc, org in _PAIR_PATH.findall(line):
            if not (UUID_RE.match(acc) and UUID_RE.match(org)):
                continue
            p = pair_path(paths, store, (acc, org))
            if p not in seen and os.path.islink(p):
                seen.add(p)
                found.append((store, p, line.strip()[:160]))
    return found
