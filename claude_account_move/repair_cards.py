"""Optional repair step: rebind target-pair cards to the session recorded before the move.

`sync` never overwrites an existing card (a hard link fails when the name
exists), so a card in the target pair that is bound to another session stays
that way: the panel opens a session whose history is not the one the owner had.

The source of truth is the move snapshot (the one `finish` loads). For every
card name the snapshot recorded a session id. A target card whose session id
differs from the recorded one is replaced by a copy of the SAME card name, in
any directory, whose session id equals the recorded one. Card activity and
title are never used as proof (a different session can carry the same title and
a fresher activity). The recorded session's transcript must exist and its last
real message must not be older than the snapshot recorded; otherwise the card is
skipped with the reason. When no copy with the recorded id exists the card is
skipped (`no source copy`): a card is never synthesized.

Dry run by default. `--apply` (app closed) writes, per card:
  1. a byte-for-byte backup in the state directory,
  2. a staged copy of the source card next to the target card,
  3. a journal line (flushed BEFORE the replacement) with both checksums and
     the device and inode of the staged copy,
  4. one atomic `os.replace` of the staged copy over the target card, after the
     target card was checked once more to hold exactly the planned bytes.
A journal write that fails AFTER the replace is reported as `unacknowledged`
(the intent line already proves the replacement, so undo still restores it).
`--undo JOURNAL` puts the backed-up bytes back with one atomic replace, only
when the card still is the file this run published (same device and inode, same
checksum); anything else is reported as ambiguous and left alone.

The journal is JSON lines in the same shape as the sync journal (begin /
intent / done / failed, bound to the home directory and one operation id).
The short window between the last check and the replace cannot be closed
without the app being quiet; the app is required to be closed.
"""
import os
import time
import uuid

from . import post_move_verify as verify
from . import store as st
from .common import CARD_PREFIX, STORES, fmt_ms, inside, sha256_file
from .sync_cards import Journal, JournalError, _new_binary, _rank, read_journal

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


REASONS = {
    "no_source_copy": "no source copy",
    "no_transcript": "transcript of the recorded session is missing or unreadable",
    "no_recorded_last": "the snapshot recorded no last message time for the session",
    "transcript_older": "transcript of the recorded session is older than the snapshot",
}


def plan_repair(index, tmap, target_account, active_dir, before):
    """(items, skipped). Item: store, name, title, src, dst, now/recorded cid, last.

    `before` is the loaded move snapshot. A target card is planned when its
    session id differs from the id the snapshot recorded for that name and a
    copy of the name with the recorded id exists in another directory, the
    recorded session's transcript exists and its last real message is not older
    than recorded. Everything else that differs is returned in `skipped` with a
    `reason` key of REASONS. Cards with a `deleted_<id>` mark are left alone.
    """
    items, skipped = [], []
    for store in STORES:
        s = index.stores[store]
        recorded = (((before.get("stores") or {}).get(store) or {}).get("cards")) or {}
        if store == STORES[0]:
            dirs = [active_dir] if active_dir else []
        else:
            dirs = verify.target_dirs(index, store, target_account)
        dead = s.tombstone_ids()
        for dst_dir in dirs:
            if dst_dir not in s.dirs:
                continue
            for name in sorted(s.dirs[dst_dir]["cards"]):
                rec = recorded.get(name) or {}
                want = (rec.get("cid") or "").lower()
                if not want or name[len(CARD_PREFIX):-len(".json")] in dead:
                    continue
                old = s.dirs[dst_dir]["cards"][name]
                if old["cid"].lower() == want:
                    continue
                entry = {"store": store, "name": name, "title": old["title"],
                         "dst": os.path.join(dst_dir, name),
                         "old_cid": old["cid"], "new_cid": want,
                         "old_last": st.session_last_ms(tmap, old["cid"].lower()),
                         "new_last": None, "recorded_last": None}
                copies = [(d, c) for d, c in s.copies_of(name)
                          if d != dst_dir and c["cid"].lower() == want]
                if not copies:
                    entry["reason"] = "no_source_copy"
                    skipped.append(entry)
                    continue
                src_dir, _best = max(copies, key=lambda rc: _rank(rc[1]))
                entry["src"] = os.path.join(src_dir, name)
                rec_last = ((before.get("transcripts") or {}).get(want) or {}
                            ).get("last_ms")
                entry["recorded_last"] = rec_last
                try:
                    now_last = st.session_last_ms(tmap, want)
                except OSError:
                    now_last = None
                entry["new_last"] = now_last
                reason = None
                if not any(os.path.isfile(f) for f in tmap.get(want) or []) \
                        or now_last is None:
                    reason = "no_transcript"
                elif rec_last is None:
                    reason = "no_recorded_last"
                elif now_last < rec_last:
                    reason = "transcript_older"
                if reason:
                    entry["reason"] = reason
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
    return "rebind: %s \u00ab%s\u00bb target %s (last %s) -> %s (last %s)" % (
        item["name"], title, _short(item["old_cid"]), fmt_ms(item["old_last"]),
        _short(item["new_cid"]), fmt_ms(item["recorded_last"]))


def describe_skip(item):
    title = " ".join((item["title"] or "-").split())
    return "skipped: %s \u00ab%s\u00bb target %s: %s" % (
        item["name"], title, _short(item["old_cid"]), REASONS[item["reason"]])


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
    """Replace the planned cards.

    Returns {"replaced","changed","skipped","failed","unacknowledged"}.
    `changed`: the target or the source card no longer holds the bytes that were
    planned, nothing was touched. `unacknowledged`: the card was replaced but the
    `done` line could not be written; the `intent` line still proves it, so undo
    restores it.
    """
    res = {"replaced": 0, "changed": 0, "skipped": 0, "failed": 0,
           "unacknowledged": 0}
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
            published = False
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
                st_ = _write_new(tmp, new_raw, mode)
                jr.add(op="intent", path=dst, src=src, backup=backup,
                       sha_before=sha256_file(backup), sha_after=sha256_file(tmp),
                       dev=st_.st_dev, ino=st_.st_ino)
                if before_replace is not None:
                    before_replace(dst)
                if sha256_file(dst) != sha256_file(backup):
                    res["changed"] += 1                       # written meanwhile
                    jr.add(op="failed", path=dst, error="changed")
                    continue
                os.replace(tmp, dst)                          # atomic publish
                published = True
            except (OSError, ValueError) as exc:
                res["failed"] += 1                    # nothing was published
                try:
                    jr.add(op="failed", path=dst, error=str(exc)[:80])
                except OSError:
                    pass
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            if published:
                res["replaced"] += 1
                try:
                    jr.add(op="done", path=dst)
                except OSError:
                    res["unacknowledged"] += 1    # published; the intent proves it
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
