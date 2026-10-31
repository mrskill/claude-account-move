"""The active pair <store>/<account>/<org> must be a real directory.

After an account move the active pair was a symlink chain. The app reads
through it, but every write fails with ENOTDIR (main.log: "Failed to save
session local_...: ENOTDIR: not a directory ... at ensureStorageDir"), so new
cards were silently lost. This module finds such pairs, plans the heal (remove
the link, move the real directory R to the active path, leave a symlink at R's
old path that points to the active path) and applies it for all stores or for
none.
"""
import ctypes
import ctypes.util
import json
import os
import re
import stat
import sys
import time

from . import store as st
from .common import STORES, UUID_RE, inside, write_json

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
    # The CLI organization confirms the pair only when the CLI is signed in to
    # the SAME account as the app and that organization exists under it.
    same = (identity.get("cli") or {}).get("value")
    if cli_org and same and same.lower() == acc and cli_org in orgs:
        return (acc, cli_org), "app account + CLI organization", None
    if len(orgs) == 1:
        return (acc, orgs[0]), "app account + its only organization", None
    return None, None, (
        "sources disagree, not guessing: app account %s; CLI account %s; CLI "
        "organization %s; organizations that exist under the app account: %s; "
        "app log pair: %s. Pass --active-pair ACCOUNT/ORG"
        % (acc, same or "none", cli_org or "none", ", ".join(orgs) or "none",
           "%s/%s" % lp if lp else "none"))


def pair_path(paths, store, pair):
    return os.path.join(paths.support, store, pair[0], pair[1])


def symlinked(paths, pair):
    """[(store, path)] of active pairs that are symlinks."""
    return [(s, pair_path(paths, s, pair)) for s in STORES
            if os.path.islink(pair_path(paths, s, pair))]


class SwapUnavailable(Exception):
    """The atomic exchange of two paths is not available here."""


def _swap_fn():
    libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
    if sys.platform == "darwin":
        fn = getattr(libc, "renamex_np", None)
        if fn is None:
            raise SwapUnavailable("renamex_np is missing")
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        return lambda x, y: fn(x, y, 0x2)             # RENAME_SWAP
    fn = getattr(libc, "renameat2", None)
    if fn is None:
        raise SwapUnavailable("renameat2 is missing")
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                   ctypes.c_uint]
    return lambda x, y: fn(-100, x, -100, y, 0x2)      # AT_FDCWD, RENAME_EXCHANGE


def swap_supported():
    try:
        _swap_fn()
        return True
    except (SwapUnavailable, OSError):
        return False


def rename_swap(x, y):
    """Atomically exchange two paths (no moment without either of them)."""
    fn = _swap_fn()
    if fn(os.fsencode(x), os.fsencode(y)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), "%s <-> %s" % (x, y))


class Changed(Exception):
    """The disk changed between the plan and the operation."""


class RestoreFailed(Exception):
    """An operation failed and putting it back failed too."""


def a_real(path):
    return os.path.join(os.path.realpath(os.path.dirname(path)),
                        os.path.basename(path))


def _stays_inside(root, dp, link):
    """True when the relative `link` found in folder `dp` of the tree at `root`
    points at the same file after the tree moves. The target is walked one
    component at a time from `dp`: never above `root` at any step, and no folder
    on the way may be a symlink (only the last component may be one, or be
    missing). A target that is the link's own folder or a parent is a loop."""
    cur = [c for c in os.path.relpath(dp, root).split(os.sep) if c != os.curdir]
    parts = [c for c in link.split(os.sep) if c not in ("", os.curdir)]
    last = len(parts) - (0 if link.endswith(os.sep) else 1)   # a "d/" must be a folder
    gone = False                         # a folder on the way does not exist
    for i, c in enumerate(parts):
        if c == os.pardir:
            if not cur:
                return False
            cur.pop()
            continue
        cur.append(c)
        if i < last and not gone:
            try:
                mode = os.lstat(os.path.join(root, *cur)).st_mode
            except OSError:
                gone = True              # dangling inside the tree is fine
            else:
                if stat.S_ISLNK(mode):
                    return False
    try:
        end = os.stat(os.path.join(root, *cur))
    except OSError:
        return True
    if not stat.S_ISDIR(end.st_mode):
        return True
    here = dp                            # identity, not spelling (case-folding volumes)
    while True:
        st = os.stat(here)
        if (st.st_dev, st.st_ino) == (end.st_dev, end.st_ino):
            return False
        up = os.path.dirname(here)
        if here == root or up == here:
            return True
        here = up


def relative_link_problem(root):
    """Why the tree at `root` cannot be moved safely, or None. Every absolute
    symlink is refused, wherever it points (inside it would keep pointing at the
    old place). A relative symlink is fine when walking its target from its own
    folder never leaves `root` and passes through no other symlink (it points at
    the same file after the move); otherwise, or when it points at its own folder
    or a parent, it is refused. A dangling link inside the tree is allowed."""
    errors = []
    for dp, dns, fns in os.walk(root, onerror=errors.append):
        for n in dns + fns:
            p = os.path.join(dp, n)
            if not os.path.islink(p):
                continue
            link = os.readlink(p)
            if os.path.isabs(link):
                return ("absolute symlink check: %s -> %s is an absolute symlink "
                        "inside the folder and is not moved safely" % (p, link))
            if not _stays_inside(root, dp, link):
                return ("relative symlink check: %s -> %s leaves the folder, passes "
                        "through another symlink or loops back on it and could "
                        "change its target after the move" % (p, link))
    if errors:
        return "cannot read the folder %s completely: %s" % (root, errors[0])
    return None


def plan_store(paths, store, pair):
    """One dict per store: state is real, absent, heal or refuse."""
    a = pair_path(paths, store, pair)
    row = {"store": store, "path": a, "pair": list(pair), "state": None,
           "target": None, "reason": None}
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
    elif inside(a_real(a), target):
        row.update(state="refuse", reason="the target %s contains the active path" % target)
    else:
        bad = relative_link_problem(target)
        if bad:
            row.update(state="refuse", reason=bad)
        else:
            lst = os.lstat(target)
            row.update(state="heal", ino=lst.st_ino, dev=lst.st_dev)
    return row


def plan(paths, pair):
    return [plan_store(paths, s, pair) for s in STORES]


def dedupe(rows, confirmed=None):
    """One row per physical target. Rows of different pairs that share a target
    keep the confirmed pair; without one they are all refused."""
    groups = {}
    for r in rows:
        if r["state"] == "heal":
            groups.setdefault((r["store"], r["target"]), []).append(r)
    drop = []
    for grp in groups.values():
        if len(grp) < 2:
            continue
        keep = [r for r in grp if confirmed and tuple(r["pair"]) == tuple(confirmed)]
        if len(keep) == 1:
            drop.extend(r for r in grp if r is not keep[0])
        else:
            for r in grp:
                r.update(state="refuse", reason="several pairs (%s) share the target "
                         "%s and none is confirmed as the current one" % (
                             ", ".join("/".join(x["pair"]) for x in grp), r["target"]))
    return [r for r in rows if not any(r is d for d in drop)]


# --- the step journal -------------------------------------------------------
def journal_path(paths):
    return os.path.join(paths.state, "heal-active-journal.json")


def _save_journal(paths, doc):
    os.makedirs(paths.state, exist_ok=True)
    write_json(journal_path(paths), doc)


def _drop_journal(paths):
    try:
        os.unlink(journal_path(paths))
    except FileNotFoundError:
        pass


def _lstat(p):
    try:
        return os.lstat(p)
    except OSError:
        return None


def _same_dir(st_, op):
    return (st_ is not None and stat.S_ISDIR(st_.st_mode)
            and (st_.st_ino, st_.st_dev) == (op["ino"], op["dev"]))


def _replace_link(path, content, tmp):
    """Atomically put a symlink `path` -> content over whatever symlink is there."""
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    os.symlink(content, tmp)
    os.rename(tmp, path)


def _recheck(op):
    """Raise Changed unless A is still the planned symlink and R the planned dir,
    and the symlinks inside R are still safe to move."""
    la, lr = _lstat(op["path"]), _lstat(op["target"])
    if la is None or not stat.S_ISLNK(la.st_mode) or os.readlink(op["path"]) != op["link"]:
        raise Changed("%s is no longer the planned symlink" % op["path"])
    if os.path.realpath(op["path"]) != op["target"]:
        raise Changed("%s no longer resolves to %s" % (op["path"], op["target"]))
    if not _same_dir(lr, op):
        raise Changed("%s is no longer the planned real directory (inode changed)"
                      % op["target"])
    bad = relative_link_problem(op["target"])   # a folder may have become a link
    if bad:
        raise Changed(bad)


def _verify_done(op):
    la, lr = _lstat(op["path"]), _lstat(op["target"])
    if not _same_dir(la, op):
        raise Changed("after the move %s is not the original directory" % op["path"])
    if lr is None or not stat.S_ISLNK(lr.st_mode) or \
            os.path.realpath(op["target"]) != op["a_real"]:
        raise Changed("after the move %s is not a link to %s" % (op["target"], op["path"]))


def _heal_op(op, step):
    """Swap the real folder R with the symlink A, then replace the swapped
    link at R by a clean link to A. `step(name)` records progress."""
    _recheck(op)
    link_to_a = os.path.relpath(op["a_real"], os.path.dirname(op["target"]))
    try:
        os.unlink(op["tmp"])
    except FileNotFoundError:
        pass
    os.symlink(link_to_a, op["tmp"])
    try:
        step("tmp_made")
        rename_swap(op["target"], op["a_real"])
    except OSError:
        try:
            os.unlink(op["tmp"])
        except OSError:
            pass
        raise
    try:                    # from here the disk may have changed: protected
        step("swapped")
        os.rename(op["tmp"], op["target"])
        step("linked")
        _verify_done(op)
    except (OSError, Changed) as exc:
        try:
            _undo_op(op)
        except (OSError, Changed) as e2:
            raise RestoreFailed("%s; and restoring it failed: %s" % (exc, e2))
        raise


def _restored(op):
    """None when A is the original symlink and R the original folder again."""
    la, lr = _lstat(op["path"]), _lstat(op["target"])
    if la is None or not stat.S_ISLNK(la.st_mode) or os.readlink(op["path"]) != op["link"]:
        return "%s is not the original symlink" % op["path"]
    if not _same_dir(lr, op):
        return "%s is not the original folder" % op["target"]
    if os.path.realpath(op["path"]) != op["target"]:
        return "%s does not resolve to %s again" % (op["path"], op["target"])
    return None


def _undo_op(op):
    """Put one op back as planned: A the original symlink, R the real folder."""
    la, lr = _lstat(op["path"]), _lstat(op["target"])
    if _same_dir(la, op) and lr is not None and stat.S_ISLNK(lr.st_mode):
        rename_swap(op["a_real"], op["target"])          # A: link, R: directory
        _replace_link(op["path"], op["link"], op["tmp"])
    elif _same_dir(lr, op) and la is not None and stat.S_ISLNK(la.st_mode):
        if os.readlink(op["path"]) != op["link"]:
            _replace_link(op["path"], op["link"], op["tmp"])
    else:
        raise Changed("cannot restore %s / %s: neither the planned nor the healed "
                      "state" % (op["path"], op["target"]))
    try:
        os.unlink(op["tmp"])
    except FileNotFoundError:
        pass


def apply(paths, rows):
    """Heal every row in state heal for all stores or none, with a step journal.

    Returns (done_rows, error text or None, rollback_errors). A non-empty
    rollback_errors means the disk is partly changed: the journal is kept.
    """
    ops = []
    for r in rows:
        if r["state"] != "heal":
            continue
        d = dict((k, r[k]) for k in ("store", "path", "link", "target", "ino", "dev"))
        d["a_real"] = a_real(r["path"])
        d["tmp"] = os.path.join(os.path.dirname(r["target"]),
                                ".heal-active-%d-%d.tmp" % (os.getpid(), len(ops)))
        d["step"] = "planned"
        ops.append(d)
    doc = {"schema": "claude-account-move/heal-journal/1", "status": "applying",
           "ops": ops}
    _save_journal(paths, doc)           # before the first change
    done = []
    for op in ops:
        def step(name, op=op):
            op["step"] = name
            _save_journal(paths, doc)
        try:
            _heal_op(op, step)
        except (OSError, Changed, RestoreFailed) as exc:
            errors = []
            if isinstance(exc, RestoreFailed):
                errors.append("%s / %s: %s" % (op["path"], op["target"], exc))
            try:
                os.unlink(op["tmp"])
            except OSError:
                pass
            for d in reversed(done):
                try:
                    _undo_op(d)
                    d["step"] = "undone"
                except (OSError, Changed) as e2:
                    errors.append("%s / %s: %s" % (d["path"], d["target"], e2))
            touched = done + ([op] if op["step"] != "planned" else [])
            for d in touched:                   # the rollback must be proven
                bad = _restored(d)
                if bad and not any(bad in e for e in errors):
                    errors.append("not restored: " + bad)
            msg = "%s: %s: %s" % (op["store"], op["path"], exc)
            if errors:
                doc["status"] = "failed"
                doc["errors"] = errors
                try:
                    _save_journal(paths, doc)
                except OSError:
                    pass
                return [], msg, errors
            _drop_journal(paths)
            return [], msg, []
        op["step"] = "done"
        done.append(op)
        _save_journal(paths, doc)
    _drop_journal(paths)
    return done, None, []


def recover(paths):
    """Settle an unfinished journal first. Returns (messages, errors)."""
    jp = journal_path(paths)
    if not os.path.exists(jp):
        return [], []
    try:
        with open(jp, encoding="utf-8") as fh:
            doc = json.load(fh)
        ops = doc["ops"]
    except (OSError, ValueError, KeyError) as exc:
        return [], ["unreadable heal journal %s: %s" % (jp, exc)]
    msgs, errs = [], []
    for op in ops:
        la, lr = _lstat(op["path"]), _lstat(op["target"])
        try:
            if _same_dir(lr, op) and la is not None and stat.S_ISLNK(la.st_mode):
                if os.readlink(op["path"]) != op["link"]:
                    _replace_link(op["path"], op["link"], op["tmp"])
                msgs.append("recovered %s: the move had not happened, link intact"
                            % op["path"])
            elif _same_dir(la, op) and lr is not None and stat.S_ISLNK(lr.st_mode):
                if os.path.realpath(op["target"]) != op["a_real"]:
                    _replace_link(op["target"], os.path.relpath(
                        op["a_real"], os.path.dirname(op["target"])), op["tmp"])
                msgs.append("recovered %s: the move is complete" % op["path"])
            else:
                errs.append("cannot recover %s / %s: the folders are in neither the "
                            "planned nor the healed state; fix by hand" % (
                                op["path"], op["target"]))
                continue
            try:
                os.unlink(op["tmp"])
            except FileNotFoundError:
                pass
        except OSError as exc:
            errs.append("cannot recover %s / %s: %s" % (op["path"], op["target"], exc))
    if errs:
        errs.append("journal kept: %s (delete it after fixing by hand)" % jp)
    else:
        _drop_journal(paths)
    return msgs, errs


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


def _ts(line):
    m = _LINE_TS.match(line)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1).replace("T", " "),
                                         "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def log_events(paths):
    """Ordered events of main.log: acct (account changed), error (a failure line
    naming a sessions-store pair) and ok (any other line naming one)."""
    events = []
    for line in _log_text(paths).splitlines():
        low = line.lower()
        if "account changed" in low:
            events.append({"kind": "acct", "line": line})
            continue
        pairs = [(sto, a.lower(), o.lower()) for sto, a, o in _PAIR_PATH.findall(line)
                 if UUID_RE.match(a) and UUID_RE.match(o)]
        if not pairs:
            continue
        bad = any(k in low for k in ("enotdir", "fail", "error"))
        events.append({"kind": "error" if bad else "ok", "line": line, "pairs": pairs,
                       "ts": _ts(line), "enotdir": "enotdir" in low})
    return events


def plan_from_log(paths, now=None):
    """(rows, notes). At most ONE pair is healed: the current one, taken from the
    newest line of the log (of any kind) that names a sessions-store pair and is
    newer than the last account change. It is healed only if an ENOTDIR line of
    the last 10 minutes names it and it is still a symlink. Every other pair is
    never touched."""
    now = time.time() if now is None else now
    ev = log_events(paths)
    last_acct = max([i for i, e in enumerate(ev) if e["kind"] == "acct"] or [-1])
    named = [e for i, e in enumerate(ev) if i > last_acct and e["kind"] != "acct"]
    if not named:
        return [], ["no line of the app log names a pair after the last account "
                    "change: nothing to heal"]
    cur = named[-1]["pairs"][-1][1:]
    stores, others, notes = set(), set(), []
    for e in named:
        if e["kind"] != "error" or not e["enotdir"] or e["ts"] is None \
                or now - e["ts"] >= FROM_LOG_MAX_AGE_S:
            continue
        for sto, acc, org in e["pairs"]:
            if (acc, org) == cur:
                stores.add(sto)
            else:
                others.add((acc, org))
    for acc, org in sorted(others):
        notes.append("skipped %s/%s: the current pair is %s/%s" % (acc, org, cur[0], cur[1]))
    rows = []
    for sto in sorted(stores):
        r = plan_store(paths, sto, cur)
        if r["state"] in ("heal", "refuse"):
            rows.append(r)
    oks = [e for e in named if e["kind"] == "ok"]
    confirmed = oks[-1]["pairs"][-1][1:] if oks else None
    for r in rows:
        if r["state"] != "heal" or confirmed == cur:
            continue
        for acc, org in sorted(others):     # an unconfirmed pair sharing the folder
            o = plan_store(paths, r["store"], (acc, org))
            if o["state"] == "heal" and o["target"] == r["target"]:
                r.update(state="refuse", reason="pairs %s/%s and %s/%s share the "
                         "folder %s and the current one is not confirmed by a write"
                         % (cur[0], cur[1], acc, org, r["target"]))
    return rows, notes
