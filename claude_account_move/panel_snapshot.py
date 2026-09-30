"""The "before" snapshot of one move, stored in the tool's own state directory.

By default only metadata is stored: per card name the session id, activity
time, completed turns and number of copies; deleted-session ids; which sessions
have a transcript; directory layout; accounts; settings counts. No titles and
no email address. A full copy of the card files is made only on request.

Layout: <state>/moves/<move-id>/{before.json, manifest.json}. The directory is
built as <move-id>.partial and renamed, so a half-written snapshot is never
selected. <state>/current-ready-move-id points at the newest READY snapshot.
"""
import hashlib
import json
import os
import random
import re
import shutil
import time
from datetime import datetime, timezone

from . import store as st
from .common import (CARD_PREFIX, STORES, TOMB_PREFIX, atomic_write,
                     now_rfc3339, read_json, safe_subdir, sha256_file,
                     write_json)
from . import __version__

SCHEMA_BEFORE = "claude-account-move/before/1"
SCHEMA_MANIFEST = "claude-account-move/manifest/1"
POINTER = "current-ready-move-id"
MOVE_ID_RE = re.compile(r"^[0-9TZ]+-[0-9a-f]{8}-[0-9a-f]{8}$")
NEWEST_SAMPLE = 40


def path_id(path):
    """Short fingerprint of a resolved path: binds a snapshot to a home
    directory without writing the path itself into the snapshot."""
    return hashlib.sha256(os.path.realpath(path).encode("utf-8")).hexdigest()[:16]


def new_move_id(app_account):
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S") + "%03dZ" % (now.microsecond // 1000)
    acc = (app_account or "00000000").replace("-", "")[:8]
    return "%s-%s-%08x" % (stamp, acc, random.SystemRandom().getrandbits(32))


def read_tasks(index, store_name):
    """{task id: {"enabled", "approved"}} unioned over the store's directories."""
    out = {}
    for info in index.stores[store_name].dirs.values():
        for tid, meta in info["task_map"].items():
            cur = out.setdefault(tid, {"enabled": False, "approved": 0})
            cur["enabled"] = cur["enabled"] or meta["enabled"]
            cur["approved"] = max(cur["approved"], meta["approved"])
    return out


def _digest(obj):
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def settings_state(paths):
    """"missing" (no file), "ok", or "unreadable" (exists but not usable)."""
    data, err = read_json(paths.settings)
    if err == "missing":
        return "missing"
    if err or not isinstance(data, dict):
        return "unreadable"
    return "ok"


def settings_summary(paths):
    """Counts plus digests of permissions and hooks, or None when not usable.

    Digests cover the full canonical content, so replacing one permission with
    another, or changing a hook's command or event, changes the summary even
    when the counts stay equal. Permission lists are compared as sets.
    """
    data, err = read_json(paths.settings)
    if err or not isinstance(data, dict):
        return None
    perm = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    n_hooks = 0
    for groups in hooks.values():
        for g in groups or []:
            if isinstance(g, dict):
                n_hooks += len(g.get("hooks") or [])
    canon_perm = {}
    for k, v in perm.items():
        canon_perm[k] = (sorted(json.dumps(x, sort_keys=True) for x in v)
                         if isinstance(v, list) else v)
    return {"cleanupPeriodDays": data.get("cleanupPeriodDays"),
            "allow": len(perm.get("allow") or []),
            "deny": len(perm.get("deny") or []),
            "ask": len(perm.get("ask") or []),
            "hook_events": len(hooks), "hook_commands": n_hooks,
            "permissions_digest": _digest(canon_perm),
            "hooks_digest": _digest(hooks)}


def newest_message(index, tmap):
    """Newest real message time over the most recently active sessions."""
    s = index.stores[STORES[0]]
    best = []
    for n in s.union_names():
        c = s.best_card(n)
        if c["cid"]:
            best.append((c["act"], c["cid"].lower()))
    best.sort(reverse=True)
    vals = [v for v in (st.session_last_ms(tmap, sid)
                        for _, sid in best[:NEWEST_SAMPLE]) if v]
    return max(vals) if vals else None


def store_section_ids(index, tmap, name):
    s = index.stores[name]
    return {c["cid"].lower() for c in (s.best_card(n) for n in s.union_names())
            if c["cid"] and c["cid"].lower() in tmap}


def store_section(index, tmap, name):
    s = index.stores[name]
    cards = {}
    for n in sorted(s.union_names()):
        b = s.best_card(n)
        cards[n] = {"cid": b["cid"], "act": b["act"], "turns": b["turns"],
                    "copies": len(s.copies_of(n))}
    return {
        "cards": cards,
        "tombstone_ids": sorted(s.tombstone_ids()),
        "aliases": sorted("%s/%s" % (a["acc"], a["org"]) for a in s.aliases),
        "physical_dirs": sorted(s.rel(r) for r in s.dirs),
        "cards_with_transcript": sorted(
            {c["cid"].lower() for c in (s.best_card(n) for n in s.union_names())
             if c["cid"] and c["cid"].lower() in tmap}),
        "tasks": read_tasks(index, name),
    }


def build_before(paths, index, tmap, identity):
    sids = set()
    for name in STORES:
        sids |= set(store_section_ids(index, tmap, name))
    return {
        "schema": SCHEMA_BEFORE,
        "identity": {"app_account": identity["app"]["value"],
                     "cli_account": identity["cli"]["value"],
                     "cli_org": identity.get("cli_org")},
        "stores": {name: store_section(index, tmap, name) for name in STORES},
        "newest_message_ms": newest_message(index, tmap),
        "transcripts": st.transcript_evidence(tmap, sorted(sids), index.obs),
        "settings": settings_summary(paths),
    }


def copy_full(index, dest):
    """Copy card, tombstone and task files of every physical directory."""
    n = 0
    for name in STORES:
        s = index.stores[name]
        for real in s.dirs:
            target = os.path.join(dest, name, s.rel(real).replace(os.sep, "__"))
            os.makedirs(target, exist_ok=True)
            for f in os.listdir(real):
                if f.startswith(CARD_PREFIX) or f.startswith(TOMB_PREFIX) \
                        or f == "scheduled-tasks.json":
                    src = os.path.join(real, f)
                    if os.path.isfile(src):
                        shutil.copy2(src, os.path.join(target, f))
                        n += 1
    return n


def moves_dir(paths):
    return os.path.join(paths.state, "moves")


def write_snapshot(paths, before, ready, index=None, full_copy=False):
    """Publish the snapshot atomically; returns (move_id, directory, copied)."""
    safe_subdir(paths, "moves")
    move_id = new_move_id(before["identity"].get("app_account"))
    partial = os.path.join(moves_dir(paths), move_id + ".partial")
    final = os.path.join(moves_dir(paths), move_id)
    os.mkdir(partial)
    copied = 0
    write_json(os.path.join(partial, "before.json"), before)
    if full_copy and index is not None:
        copied = copy_full(index, os.path.join(partial, "cards-copy"))
    manifest = {"schema": SCHEMA_MANIFEST, "version": __version__,
                "ready": bool(ready), "created_at": now_rfc3339(),
                "home_id": path_id(paths.home),
                "support_id": path_id(paths.support),
                "files": {"before.json": sha256_file(
                    os.path.join(partial, "before.json"))}}
    write_json(os.path.join(partial, "manifest.json"), manifest)
    os.rename(partial, final)
    if ready:
        atomic_write(os.path.join(paths.state, POINTER),
                     (move_id + "\n").encode("ascii"))
    return move_id, final, copied


def load_baseline(paths, move_id=None, allow_unready=False, max_age_hours=72.0,
                  now=None):
    """(before, manifest, move_id, problem). problem is None when usable."""
    if move_id is None:
        ptr = os.path.join(paths.state, POINTER)
        try:
            with open(ptr, encoding="ascii") as fh:
                move_id = fh.read().strip()
        except OSError:
            return None, None, None, "no ready snapshot (run prepare first)"
    if not MOVE_ID_RE.match(move_id):
        return None, None, move_id, "malformed move id"
    if os.path.islink(moves_dir(paths)):
        return None, None, move_id, "the moves directory is a symlink"
    d = os.path.join(moves_dir(paths), move_id)
    if os.path.islink(d) or not os.path.isdir(d):
        return None, None, move_id, "snapshot directory not found: %s" % move_id
    man, err = read_json(os.path.join(d, "manifest.json"))
    if err or not isinstance(man, dict) or man.get("schema") != SCHEMA_MANIFEST:
        return None, None, move_id, "manifest unreadable or of another version"
    if not man.get("ready") and not allow_unready:
        return None, man, move_id, "snapshot is not ready (prepare reported failures)"
    files = man.get("files") or {}
    bpath = os.path.join(d, "before.json")
    try:
        if sha256_file(bpath) != files.get("before.json"):
            return None, man, move_id, "before.json does not match its checksum"
    except OSError:
        return None, man, move_id, "before.json missing"
    if man.get("home_id") != path_id(paths.home) \
            or man.get("support_id") != path_id(paths.support):
        return None, man, move_id, "snapshot belongs to another home directory"
    try:
        created = datetime.fromisoformat(man["created_at"])
        age_h = ((time.time() if now is None else now) - created.timestamp()) / 3600.0
    except Exception:
        return None, man, move_id, "created_at unreadable"
    if age_h > max_age_hours:
        return None, man, move_id, "snapshot is %.1f h old (limit %.0f h)" % (
            age_h, max_age_hours)
    before, err = read_json(bpath)
    if err or not isinstance(before, dict) or before.get("schema") != SCHEMA_BEFORE:
        return None, man, move_id, "before.json unreadable or of another version"
    if man.get("ready") and not (before.get("identity") or {}).get("app_account"):
        return None, man, move_id, "ready snapshot without a source account"
    return before, man, move_id, None
