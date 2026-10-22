"""Optional repair step: rebind target-pair cards that point at a stale session.

`sync` never overwrites an existing card (a hard link fails when the name
exists), so a card that was copied into the target pair earlier and has since
fallen behind another copy of the same card stays behind. The panel then opens
a session whose history ends days before the real transcript does.

`repair` finds, for every card name, the freshest copy in the whole store (the
key sync uses: has a session id, last activity, completed turns). A copy in the
target pair whose session id differs from the freshest copy's, while the
freshest copy is STRICTLY newer by last activity, is replaced by the freshest
copy's file. A card is rebound only when the freshest copy's transcript exists:
rebinding to a session that has no history would lose the old history.

Dry run by default. `--apply` (app closed) writes, per card:
  1. a byte-for-byte backup in the state directory,
  2. a staged copy of the freshest card next to the target card,
  3. a journal line (flushed BEFORE the replacement) with both checksums and
     the device and inode of the staged copy,
  4. one atomic `os.replace` of the staged copy over the target card, after the
     target card was checked once more to hold exactly the planned bytes.
`--undo JOURNAL` puts the backed-up bytes back with one atomic replace, only
when the card still is the file this run published (same device and inode, same
checksum); anything else is reported as ambiguous and left alone.

The journal is JSON lines in the same shape as the sync journal (begin /
intent / done / failed, bound to the home directory and one operation id).
"""
import os
import time
import uuid

from . import post_move_verify as verify
from .common import CARD_PREFIX, STORES, fmt_ms, inside, sha256_file
from .sync_cards import (Journal, JournalError, _new_binary, _rank,
                         read_journal)

JOURNAL_VERSION = 1
REPAIR_FIELDS = {
    "begin": (("version", int), ("op_id", str), ("home_id", str), ("targets", list)),
    "intent": (("path", str), ("src", str), ("backup", str),
               ("sha_before", str), ("sha_after", str), ("dev", int),
               ("ino", int)),
    "done": (("path", str),),
    "failed": (("path", str),),
}


def new_journal_path(state_dir):
    return os.path.join(state_dir, "repair-%s-%s.jsonl" % (
        time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:8]))


def _short(cid):
    return cid[:8] if cid else "-"


def plan_repair(index, tmap, target_account, active_dir):
    """(items, skipped). Item: store, name, title, src, dst, old/new cid, act.

    A card qualifies when the freshest copy of its name (by sync's key) lives in
    another directory, carries a different session id and is strictly newer by
    last activity, and when that session's transcript file exists. Cards whose
    freshest copy has no transcript are returned in `skipped` (reason
    `no_transcript`), never rebound. Cards of sessions deleted in the panel (a
    `deleted_<id>` mark) are left alone.
    """
    items, skipped = [], []
    for store in STORES:
        s = index.stores[store]
        if store == STORES[0]:
            dirs = [active_dir] if active_dir else []
        else:
            dirs = verify.target_dirs(index, store, target_account)
        dead = s.tombstone_ids()
        for dst_dir in dirs:
            if dst_dir not in s.dirs:
                continue
            for name in sorted(s.dirs[dst_dir]["cards"]):
                if name[len(CARD_PREFIX):-len(".json")] in dead:
                    continue
                old = s.dirs[dst_dir]["cards"][name]
                src_dir, best = max(s.copies_of(name), key=lambda rc: _rank(rc[1]))
                if src_dir == dst_dir or not best["cid"]:
                    continue
                if best["cid"].lower() == old["cid"].lower():
                    continue
                if best["act"] <= old["act"]:
                    continue
                entry = {"store": store, "name": name, "title": old["title"],
                         "src": os.path.join(src_dir, name),
                         "dst": os.path.join(dst_dir, name),
                         "old_cid": old["cid"], "new_cid": best["cid"],
                         "old_act": old["act"], "new_act": best["act"]}
                files = tmap.get(best["cid"].lower()) or []
                if not any(os.path.isfile(f) for f in files):
                    entry["reason"] = "no_transcript"
                    skipped.append(entry)
                    continue
                try:
                    entry["old_sha"] = sha256_file(entry["dst"])
                    entry["new_sha"] = sha256_file(entry["src"])
                except OSError:
                    index.obs.unreadable_cards += 1
                    index.obs.errors.append("card unreadable while planning: %s"
                                            % entry["dst"])
                    continue
                items.append(entry)
    return items, skipped


def describe(item):
    """The one dry-run line of a planned rebind."""
    title = " ".join((item["title"] or "-").split())
    return "rebind: %s %s target %s -> %s (act %s/%s)" % (
        item["name"], title, _short(item["old_cid"]), _short(item["new_cid"]),
        fmt_ms(item["old_act"]), fmt_ms(item["new_act"]))


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def _write_new(path, data, mode=None):
    """Create a NEW file (never through a symlink), flush it to disk."""
    with _new_binary(path) as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
        st = os.fstat(fh.fileno())
    if mode is not None:
        os.chmod(path, mode)
    return st


def apply_repairs(items, journal_path, backup_dir, home_id, support_real=None,
                  before_replace=None):
    """Replace the planned cards. Returns {"replaced","changed","skipped","failed"}.

    `changed`: the target or the source card no longer holds the bytes that were
    planned (the app or someone else wrote it), nothing was touched.
    """
    res = {"replaced": 0, "changed": 0, "skipped": 0, "failed": 0}
    op_id = uuid.uuid4().hex
    jr = Journal(journal_path, op_id)
    try:
        targets = sorted({os.path.realpath(os.path.dirname(i["dst"])) for i in items})
        jr.add(op="begin", version=JOURNAL_VERSION, home_id=home_id,
               targets=targets, at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        for n, it in enumerate(items):
            dst, src = it["dst"], it["src"]
            tmp = os.path.join(os.path.dirname(dst), ".tmp-repair-%s-%d" % (op_id[:8], n))
            backup = os.path.join(backup_dir, "repair-%s-%03d-%s.bak"
                                  % (op_id[:8], n, it["name"]))
            try:
                if support_real and not inside(os.path.realpath(os.path.dirname(dst)),
                                               support_real):
                    res["skipped"] += 1
                    continue
                if os.path.islink(dst) or not os.path.isfile(dst):
                    res["skipped"] += 1
                    continue
                old_raw, new_raw = _read(dst), _read(src)
                if sha256_file(dst) != it["old_sha"] \
                        or sha256_file(src) != it["new_sha"]:
                    res["changed"] += 1
                    continue
                mode = os.stat(dst).st_mode & 0o7777
                _write_new(backup, old_raw, 0o600)          # byte for byte
                if _read(backup) != old_raw:
                    raise OSError("backup does not match the card")
                st = _write_new(tmp, new_raw, mode)
                jr.add(op="intent", path=dst, src=src, backup=backup,
                       sha_before=sha256_file(backup), sha_after=sha256_file(tmp),
                       dev=st.st_dev, ino=st.st_ino)
                if before_replace is not None:
                    before_replace(dst)
                if sha256_file(dst) != sha256_file(backup):
                    res["changed"] += 1                       # written meanwhile
                    jr.add(op="failed", path=dst, error="changed")
                    continue
                os.replace(tmp, dst)                          # atomic publish
                res["replaced"] += 1
                jr.add(op="done", path=dst)
            except (OSError, ValueError) as exc:
                res["failed"] += 1
                try:
                    jr.add(op="failed", path=dst, error=str(exc)[:80])
                except OSError:
                    pass
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    finally:
        jr.close()
    return res


def _undo_one(path, rec, journal_dir):
    """"restored" | "skipped" | "ambiguous" for one intent record."""
    backup = rec["backup"]
    if os.path.dirname(os.path.realpath(backup)) != journal_dir \
            or not os.path.isfile(backup) or os.path.islink(backup):
        return "ambiguous"                       # backup gone: nothing is touched
    if sha256_file(backup) != rec["sha_before"]:
        return "ambiguous"
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "ambiguous"                       # card deleted since: do not recreate
    if not os.path.isfile(path) or os.path.islink(path):
        return "ambiguous"
    cur = sha256_file(path)
    if (st.st_dev, st.st_ino) != (rec["dev"], rec["ino"]) or cur != rec["sha_after"]:
        # an interrupted run leaves the original untouched: nothing to undo
        return "skipped" if cur == rec["sha_before"] else "ambiguous"
    tmp = os.path.join(os.path.dirname(path), ".tmp-undo-%s" % uuid.uuid4().hex[:8])
    try:
        _write_new(tmp, _read(backup), st.st_mode & 0o7777)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return "ambiguous"
    return "restored"


def undo_repairs(journal_path, home_id, support_real):
    """Restore what a repair journal replaced, byte for byte.

    Returns {"restored","skipped","ambiguous","invalid","status"}; raises
    JournalError for an unusable journal. A card that is not provably the file
    this run published (device, inode and checksum) is left alone.
    """
    recs, status = read_journal(journal_path, REPAIR_FIELDS)
    if not recs or recs[0].get("op") != "begin":
        raise JournalError("journal has no begin record")
    begin = recs[0]
    if begin["version"] != JOURNAL_VERSION:
        raise JournalError("journal version %s is not supported" % begin["version"])
    if begin["home_id"] != home_id:
        raise JournalError("journal belongs to another home directory")
    if any(r["op_id"] != begin["op_id"] for r in recs):
        raise JournalError("journal mixes several operations")
    targets = set(begin["targets"])
    failed = {r["path"] for r in recs if r["op"] == "failed"}
    journal_dir = os.path.dirname(os.path.realpath(journal_path))
    out = {"restored": 0, "skipped": 0, "ambiguous": 0, "invalid": 0,
           "status": status}
    for r in recs[1:]:
        if r["op"] == "begin":
            raise JournalError("journal has a second begin record")
        if r["op"] != "intent":
            continue
        p = r["path"]
        parent = os.path.realpath(os.path.dirname(p))
        base = os.path.basename(p)
        if not base.startswith(CARD_PREFIX) or not base.endswith(".json") \
                or not inside(parent, support_real) or parent not in targets:
            out["invalid"] += 1
            continue
        if p in failed:
            out["skipped"] += 1
            continue
        out[_undo_one(os.path.join(parent, base), r, journal_dir)] += 1
    return out
