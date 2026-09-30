"""Optional step: put the date and time of the LAST real message into every
session title, so sessions can be told apart in the panel.

Format: the human part of the title plus one suffix, " <dot> DD.MM HH:MM", where
<dot> is the middle dot (U+00B7) and the time is local time of this machine. An
older stamp (also one followed by a short hex id and apostrophes) is cut off
first, so stamping twice never doubles the suffix. After a change of the time
zone every old stamp shifts by the same whole number of hours.

What is touched: every card of every physical folder of both stores (a symlink
is read once, by its resolved path). One stamp per session: the newest last
message over the transcripts of all copies. Cards without a transcript, with an
empty title, in a file format that cannot be reproduced byte for byte, or
already carrying the right stamp are left alone. Only the `title` field
changes; the file keeps its exact layout.

Time comes from the transcript index built once (never a search per session).

Writing: dry run by default. `--apply` writes each card through a temporary file
and an atomic replace, after checking twice (before the temp file and right
before the replace) that the card still holds exactly the bytes that were
planned; a card the app or anyone else changed in between is left alone and
reported. A journal line with the checksums and the inode of the new file is
flushed before each replacement. `--undo` restores a card byte for byte only
when its inode and content still equal the journal's (the same ownership rule as
`sync --undo`), using one atomic rename to a private name first so nothing can
be swapped between the check and the restore.

The short window between the last check and the replace cannot be closed without
the app being quiet; stamping before signing out, while the app is idle, is the
safest time.
"""
import datetime
import hashlib
import json
import os
import re
import tempfile
import time
import uuid

from . import store as st
from .common import CARD_PREFIX, STORES, inside, open_new_file
from .sync_cards import Journal, JournalError, read_journal

MIDDLE_DOT = chr(0xB7)
STAMP_RE = re.compile(r"\s" + MIDDLE_DOT + r"\s\d\d\.\d\d\s\d\d:\d\d"
                      r"(?:\s[0-9a-f]{6,12}'*)?$")
JOURNAL_VERSION = 1
DUMP_STYLES = [
    {"ensure_ascii": False, "separators": (",", ":")},
    {"ensure_ascii": False},
    {"ensure_ascii": False, "indent": 2},
    {"ensure_ascii": True, "separators": (",", ":")},
    {"ensure_ascii": True},
    {"ensure_ascii": True, "indent": 2},
]
STAMP_FIELDS = {
    "begin": (("version", int), ("op_id", str), ("home_id", str), ("targets", list)),
    "intent": (("path", str), ("title_before", str), ("title_after", str),
               ("style", int), ("nl", bool), ("sha_before", str),
               ("sha_after", str), ("dev", int), ("ino", int)),
    "done": (("path", str),),
    "failed": (("path", str),),
}


def stamped(title, dt):
    """Human part of the title plus exactly one stamp."""
    base = (title or "").rstrip()
    while True:
        cut = STAMP_RE.sub("", base).rstrip()
        if cut == base:
            break
        base = cut
    return "%s %s %s" % (base, MIDDLE_DOT, dt.strftime("%d.%m %H:%M"))


def sha_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def style_of(raw, d):
    """(index into DUMP_STYLES, trailing newline) that reproduces raw exactly."""
    for i, style in enumerate(DUMP_STYLES):
        s = json.dumps(d, **style)
        if s == raw:
            return i, False
        if s + "\n" == raw:
            return i, True
    return None, False


def dump_with(d, style, nl):
    return json.dumps(d, **DUMP_STYLES[style]) + ("\n" if nl else "")


def read_raw(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def plan_stamps(index, tmap):
    """(items, stats). Item: path, title, new, when, raw.

    Everything about a card (title, session id, rollback bytes) comes from ONE
    freshly read document, never from the earlier scan, so a card the app
    changed in between is planned coherently. Problems met here (unreadable
    card or transcript, conflicting session ids behind one card name) go to
    `index.obs`; the caller must refuse to write while it is incomplete.
    """
    stats = {"cards": 0, "no_transcript": 0, "empty_title": 0, "conflict": 0,
             "unknown_format": 0, "current": 0, "planned": 0}
    groups = {}
    for store in STORES:
        for real, info in sorted(index.stores[store].dirs.items()):
            for name in info["cards"]:
                path = os.path.join(real, name)
                try:
                    raw = read_raw(path)
                    d = json.loads(raw)
                    if not isinstance(d, dict):
                        raise ValueError("card is not an object")
                except (OSError, ValueError):
                    index.obs.unreadable_cards += 1
                    index.obs.errors.append("card unreadable while planning: %s" % path)
                    continue
                groups.setdefault(name, []).append((path, raw, d))
    items = []
    for name, copies in sorted(groups.items()):
        cids = sorted({str(d.get("cliSessionId") or "").strip().lower()
                       for _, _, d in copies} - {""})
        if len(cids) > 1:
            stats["conflict"] += len(copies)
            index.obs.errors.append("conflicting session ids behind one card "
                                    "name: %s" % name)
            continue
        best = None
        for sid in cids:
            for f in tmap.get(sid) or []:
                try:
                    with open(f, "rb"):
                        pass
                except OSError:
                    index.obs.errors.append("transcript unreadable: %s" % f)
            ms = st.session_last_ms(tmap, sid)
            if ms is not None and (best is None or ms > best):
                best = ms
        for path, raw, d in copies:
            stats["cards"] += 1
            if best is None:
                stats["no_transcript"] += 1
                continue
            title = d.get("title")
            if not isinstance(title, str) or not title.strip():
                stats["empty_title"] += 1
                continue
            when = datetime.datetime.fromtimestamp(best / 1000).astimezone()
            new = stamped(title, when)
            if new == title:
                stats["current"] += 1
                continue
            if style_of(raw, d)[0] is None:
                stats["unknown_format"] += 1
                continue
            items.append({"path": path, "title": title, "new": new,
                          "when": when, "raw": raw})
            stats["planned"] += 1
    return items, stats


def new_journal_path(state_dir):
    return os.path.join(state_dir, "stamp-%s-%s.jsonl" % (
        time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:8]))


def _put_back(src, dst):
    """Give `src` its name `dst` back, never overwriting anything.

    Only a hard link is used (it fails when the name is taken or when no
    directory entry can be made). On any failure the file stays under `src`,
    which is a recoverable name next to the card, and the caller reports it.
    Returns True when the name was restored.
    """
    try:
        os.link(src, dst)
    except OSError:
        return False
    try:
        os.unlink(src)
    except OSError:
        pass
    return True


def _quarantine_name(path, tag):
    return os.path.join(os.path.dirname(path), ".%s-%s-%s" % (
        tag, uuid.uuid4().hex[:8], os.path.basename(path)))


def _sig(path):
    t = os.lstat(path)
    return (t.st_ino, t.st_size, t.st_mtime_ns)


def _publish(path, tmp, planned_raw, seen_ino):
    """Replace `path` by `tmp` only if the file replaced is the one checked.

    The card is first moved away with one atomic rename; only the moved file is
    inspected. If it is not exactly the planned document (same inode, same
    bytes) it is put back and nothing is published. After publishing, the moved
    file is checked once more for a late write through a descriptor that was
    already open: if one happened, the newer document is put back over the stamp.
    Returns "written" | "changed" | "failed".
    """
    hold = _quarantine_name(path, "stamp-old")
    try:
        os.rename(path, hold)
    except FileNotFoundError:
        return "changed"
    try:
        sig = _sig(hold)
        same = sig[0] == seen_ino and read_raw(hold) == planned_raw
    except OSError:
        same = False
    if not same:
        _put_back(hold, path)                # never overwriting; else it stays as `hold`
        return "changed"
    try:
        os.link(tmp, path)                   # fails if the name was re-created
    except FileExistsError:
        os.unlink(hold)                      # their newer card wins; ours is stale
        return "changed"
    except OSError:
        _put_back(hold, path)
        return "failed"
    try:
        late = _sig(hold) != sig
    except OSError:
        late = True
    if late:
        return "changed"                     # the moved file holds a newer document:
        # it stays next to the card as .stamp-old-*, nothing is overwritten
    os.unlink(hold)
    return "written"


def apply_stamps(items, journal_path, home_id, before_replace=None,
                 support_real=None):
    """Write the planned stamps.

    Returns {"written","changed","skipped","failed","unacknowledged"}.
    """
    res = {"written": 0, "changed": 0, "skipped": 0, "failed": 0,
           "unacknowledged": 0}
    op_id = uuid.uuid4().hex
    jr = Journal(journal_path, op_id)
    try:
        targets = sorted({os.path.realpath(os.path.dirname(i["path"])) for i in items})
        jr.add(op="begin", version=JOURNAL_VERSION, home_id=home_id, targets=targets,
               at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        for it in items:
            path = it["path"]
            tmp = None
            try:
                if support_real and not inside(os.path.realpath(os.path.dirname(path)),
                                               support_real):
                    res["skipped"] += 1          # outside the Claude support folder
                    continue
                raw = read_raw(path)
                if raw != it["raw"]:
                    res["changed"] += 1          # changed since the plan: not touched
                    continue
                seen_ino = os.stat(path).st_ino
                d = json.loads(raw)
                style, nl = style_of(raw, d)
                if style is None or not isinstance(d.get("title"), str):
                    res["skipped"] += 1
                    continue
                d["title"] = it["new"]
                text = dump_with(d, style, nl)
                mode = os.stat(path).st_mode & 0o7777
                fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                                           prefix=".stamp-", suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
                    tst = os.fstat(fh.fileno())
                os.chmod(tmp, mode)
                jr.add(op="intent", path=path, title_before=it["title"],
                       title_after=it["new"], style=style, nl=nl,
                       sha_before=sha_text(raw), sha_after=sha_text(text),
                       dev=tst.st_dev, ino=tst.st_ino)
                if before_replace is not None:
                    before_replace(path)
                outcome = _publish(path, tmp, raw, seen_ino)
                if outcome == "written":
                    res["written"] += 1
                    try:
                        jr.add(op="done", path=path)
                    except OSError:
                        res["unacknowledged"] += 1   # published; intent still proves it
                else:
                    res[outcome] += 1
                    jr.add(op="failed", path=path, error=outcome)
            except (OSError, ValueError) as exc:
                res["failed"] += 1
                try:
                    jr.add(op="failed", path=path, error=str(exc)[:80])
                except OSError:
                    pass
            finally:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
    finally:
        jr.close()
    return res


def _restore(path, rec):
    """(status, aside): `aside` is a card left next to its name, or None."""
    quarantine = _quarantine_name(path, "undo")
    status = _restore_with(path, rec, quarantine)
    return status, (quarantine if os.path.lexists(quarantine) else None)


def _restore_with(path, rec, quarantine):
    """Restore one card byte for byte if it is provably the file we wrote.

    The stamped card is moved aside first and is deleted ONLY after the restored
    card has been published and verified. On any failure it stays (or goes
    back), so no card is ever lost. Returns "restored" | "ambiguous" | "gone".
    """
    try:
        os.rename(path, quarantine)
    except FileNotFoundError:
        return "gone"

    def put_back():
        _put_back(quarantine, path)

    tmp = None
    try:
        stt = os.lstat(quarantine)
        sig = (stt.st_ino, stt.st_size, stt.st_mtime_ns)
        if not os.path.isfile(quarantine) or os.path.islink(quarantine):
            put_back()
            return "ambiguous"
        if (stt.st_dev, stt.st_ino) != (rec["dev"], rec["ino"]):
            # An interrupted run leaves the original untouched: nothing to undo.
            same = sha_text(read_raw(quarantine)) == rec["sha_before"]
            put_back()
            return "gone" if same else "ambiguous"
        raw = read_raw(quarantine)
        if sha_text(raw) != rec["sha_after"]:
            put_back()
            return "ambiguous"
        d = json.loads(raw)
        d["title"] = rec["title_before"]
        text = dump_with(d, rec["style"], rec["nl"])
        if sha_text(text) != rec["sha_before"]:
            put_back()
            return "ambiguous"
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".stamp-",
                                   suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, stt.st_mode & 0o7777)
        os.link(tmp, path)            # publish; fails if a newer card appeared
    except FileExistsError:
        _drop(tmp)
        return "ambiguous"            # a newer card exists; the stamped one stays aside
    except (OSError, ValueError):
        _drop(tmp)
        put_back()                    # never delete on a failed restore
        return "ambiguous"
    try:
        verified = sha_text(read_raw(path)) == rec["sha_before"]
        unchanged = (os.lstat(quarantine).st_ino, os.lstat(quarantine).st_size,
                     os.lstat(quarantine).st_mtime_ns) == sig
    except OSError:
        verified = unchanged = False
    if not (verified and unchanged):
        _drop(tmp)
        return "ambiguous"            # keep the stamped card aside for recovery
    _drop(tmp)
    os.unlink(quarantine)
    return "restored"


def _drop(tmp):
    if tmp:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def undo_stamps(journal_path, home_id, support_real):
    """Restore what a stamp journal wrote. Same rules as `sync --undo`.

    Returns {"restored","skipped","ambiguous","invalid","status"}; raises
    JournalError for an unusable journal.
    """
    recs, status = read_journal(journal_path, STAMP_FIELDS)
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
    out = {"restored": 0, "skipped": 0, "ambiguous": 0, "invalid": 0,
           "status": status, "aside": []}
    for r in recs[1:]:
        if r["op"] == "begin":
            raise JournalError("journal has a second begin record")
        if r["op"] != "intent":
            continue
        p = r["path"]
        parent = os.path.realpath(os.path.dirname(p))
        base = os.path.basename(p)
        if not base.startswith(CARD_PREFIX) or not base.endswith(".json") \
                or not inside(parent, support_real) or parent not in targets \
                or not 0 <= r["style"] < len(DUMP_STYLES):
            out["invalid"] += 1
            continue
        if p in failed:
            out["skipped"] += 1
            continue
        res, aside = _restore(os.path.join(parent, base), r)
        if aside:
            out["aside"].append(aside)       # left next to the card, recoverable
        if res == "restored":
            out["restored"] += 1
        elif res == "gone":
            out["skipped"] += 1
        else:
            out["ambiguous"] += 1
    return out
