"""Put aside the cards of sessions the panel deleted (zombie cards).

A zombie card is a card local_<id>.json whose session has a deleted_<id> mark in
the same store. The readiness check `zombies` counts them with
pre_move_check.check_zombies over the store index; this module walks the SAME
index (dirs and tombstone_ids), so the cleanup and the check cannot drift apart.

Nothing is ever deleted. Each card is renamed into a backup folder of the state
directory, under its path relative to the support folder. A move never replaces
a file, never touches a path that resolves outside the support folder, and is
journaled before it happens. `undo` puts a card back only where its source path
is free.
"""
import json
import os
import time
import uuid

from . import store as st
from .common import (CARD_PREFIX, STORES, inside, safe_subdir, sha256_file,
                     write_json)

JOURNAL_SCHEMA = "claude-account-move/zombie-journal/1"
BACKUP_PREFIX = "zombie-cards-"


class JournalError(Exception):
    pass


def _outside_dirs(paths):
    """{(store, real dir)} of org directories excluded because they resolve
    outside their store; their cards are reported, never moved."""
    out = set()
    support_real = os.path.realpath(paths.support)
    for store in STORES:
        base = os.path.join(paths.support, store)
        if not os.path.isdir(base):
            continue
        base_real = os.path.realpath(base)
        if not inside(base_real, support_real):
            continue
        found = []
        for _ in st._candidate_dirs(base, base_real, None, outside=found):
            pass
        for real in found:
            out.add((store, real))
    return out


def plan(paths, index):
    """(items, problems). items: movable cards, one per real file:
    {"store","sid","source","rel"}. problems: [{"path","why"}]."""
    support_real = os.path.realpath(paths.support)
    items, problems, seen = [], [], set()

    def problem(path, why):
        if (path, why) not in seen:
            seen.add((path, why))
            problems.append({"path": path, "why": why})

    taken = set()
    for store in STORES:
        s = index.stores.get(store)
        if s is None:
            continue
        dead = s.tombstone_ids()
        if not dead:
            continue
        for real_dir, d in sorted(s.dirs.items()):
            for n in sorted(d["cards"]):
                sid = n[len(CARD_PREFIX):-len(".json")]
                if sid not in dead:
                    continue
                path = os.path.join(real_dir, n)
                real = os.path.realpath(path)
                if os.path.islink(path):
                    problem(real, "symlink")
                elif not inside(real, support_real):
                    problem(real, "outside")
                elif real not in taken:
                    taken.add(real)
                    items.append({"store": store, "sid": sid, "source": real,
                                  "rel": os.path.relpath(real, support_real)})
        for st_name, real_dir in sorted(_outside_dirs(paths)):
            if st_name != store:
                continue
            if inside(real_dir, support_real):
                continue                    # inside the support folder: not counted by the check
            try:
                names = sorted(os.listdir(real_dir))
            except OSError:
                continue
            for n in names:
                if n.startswith(CARD_PREFIX) and n.endswith(".json") \
                        and n[len(CARD_PREFIX):-len(".json")] in dead:
                    problem(os.path.realpath(os.path.join(real_dir, n)), "outside")
    items.sort(key=lambda i: i["source"])
    return items, problems


def new_backup_name():
    return BACKUP_PREFIX + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _unique_backup(paths):
    base = new_backup_name()
    name, n = base, 1
    while os.path.lexists(os.path.join(paths.state, name)):
        n += 1
        name = "%s-%d" % (base, n)
    return name


def apply_plan(paths, items):
    """Move every item. Returns {"backup","journal","moved","failed","entries"}."""
    support_real = os.path.realpath(paths.support)
    backup = safe_subdir(paths, _unique_backup(paths))
    backup_real = os.path.realpath(backup)
    jdir = safe_subdir(paths)
    journal = os.path.join(jdir, "zombie-journal-%s-%s.json" % (
        time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:8]))
    doc = {"schema": JOURNAL_SCHEMA, "support": support_real, "backup": backup,
           "entries": []}
    write_json(journal, doc)
    moved = failed = 0
    for it in items:
        src = it["source"]
        dest = os.path.join(backup, it["rel"])
        entry = {"source": src, "destination": dest, "sha256": None, "size": None,
                 "status": "planned"}
        doc["entries"].append(entry)
        try:
            if os.path.islink(src) or not os.path.isfile(src) \
                    or not inside(os.path.realpath(src), support_real):
                raise OSError("source is not a plain file inside the support folder")
            if os.path.lexists(dest):
                raise OSError("destination exists, never overwritten")
            entry["sha256"] = sha256_file(src)
            entry["size"] = os.path.getsize(src)
            entry["status"] = "moving"
            write_json(journal, doc)            # journaled before the move
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if not inside(os.path.realpath(os.path.dirname(dest)), backup_real):
                raise OSError("destination resolves outside the backup folder")
            os.rename(src, dest)
            if os.path.lexists(src) or not os.path.isfile(dest) \
                    or sha256_file(dest) != entry["sha256"]:
                raise OSError("verification after the move failed")
            entry["status"] = "moved"
            moved += 1
        except OSError as exc:
            entry["status"] = "failed"
            entry["error"] = str(exc)
            failed += 1
        write_json(journal, doc)
    return {"backup": backup, "journal": journal, "moved": moved,
            "failed": failed, "entries": doc["entries"]}


def _existing_ancestor_real(path):
    cur = path
    while not os.path.lexists(cur):
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.realpath(cur)


def undo(paths, journal):
    """Put cards back where the journal says, never over an existing file.
    Returns {"restored","kept","failed","kept_paths"}."""
    try:
        with open(journal, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise JournalError("cannot read the journal: %s" % exc)
    if not isinstance(doc, dict) or doc.get("schema") != JOURNAL_SCHEMA \
            or not isinstance(doc.get("entries"), list):
        raise JournalError("not a zombie-cards journal")
    support_real = os.path.realpath(paths.support)
    state_real = os.path.realpath(paths.state)
    restored, kept, failed, kept_paths = 0, 0, 0, []
    for e in doc["entries"]:
        if not isinstance(e, dict) or e.get("status") != "moved":
            continue
        src, dest = e.get("source"), e.get("destination")
        if not isinstance(src, str) or not isinstance(dest, str):
            failed += 1
            continue
        if not inside(os.path.realpath(src), support_real) \
                or not inside(os.path.realpath(dest), state_real) \
                or os.path.islink(dest) or not os.path.isfile(dest):
            failed += 1
            continue
        if os.path.lexists(src):
            kept += 1
            kept_paths.append(src)
            continue
        try:
            if sha256_file(dest) != e.get("sha256"):
                raise OSError("backup copy changed")
            if not inside(_existing_ancestor_real(os.path.dirname(src)), support_real):
                raise OSError("target folder resolves outside the support folder")
            os.makedirs(os.path.dirname(src), exist_ok=True)
            os.rename(dest, src)
            restored += 1
        except OSError:
            failed += 1
    return {"restored": restored, "kept": kept, "failed": failed,
            "kept_paths": kept_paths}
