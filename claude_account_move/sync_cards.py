"""Optional repair step: copy session cards that are missing in the target pair.

`sync --apply` ADDS files and `sync --undo` REMOVES files it added; nothing else
in this package writes into the Claude storage. An existing file is never
overwritten, renamed or deleted by `--apply` (a hard link publishes each copy
and fails if the name exists). A new directory is created only for a store that
has none for the target account yet.

Journal (one new file per run, JSON lines, bound to the home directory):
  begin    operation id and home fingerprint
  intent   written and flushed BEFORE publication: destination, checksum, and the
           device and inode of the staged copy
  done     after publication
  failed   when publication did not happen

Ownership: the published card is a hard link of the staged copy, so it shares
the staged copy's inode. Undo deletes a destination only when its device and
inode equal the recorded ones and its content matches. A file that merely has
the same bytes (another writer, a restored card) is never deleted; it is
reported as ambiguous. A torn last line is reported, and the intact prefix is
still used.

Not copied: cards of sessions deleted in the panel (a `deleted_<id>` mark exists
somewhere), and cards whose copies disagree on the session id (reported as
conflicts). Scheduled tasks are not migrated.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import uuid

from . import post_move_verify as verify
from .common import CARD_PREFIX, STORES, inside, open_new_file, sha256_file


class JournalError(Exception):
    """The journal cannot be used at all (wrong home, no begin record)."""


def _rank(card):
    return (1 if card["cid"] else 0, card["act"], card["turns"])


def plan_sync(index, target_account, active_dir, pair=None):
    """(items, conflicts). Item: {store, name, src, dst_dir, create_dir}."""
    plan, conflicts = [], []
    for name in STORES:
        s = index.stores[name]
        if name == STORES[0]:
            dirs = [active_dir] if active_dir else []
        else:
            dirs = verify.target_dirs(index, name, target_account)
        create = False
        if not dirs and name != STORES[0] and pair and s.union_names():
            dirs = [os.path.join(os.path.realpath(s.base), pair[0], pair[1])]
            create = True
        dead = s.tombstone_ids()
        for dst in dirs:
            if not create and dst not in s.dirs:
                continue
            have = set() if create else set(s.dirs[dst]["cards"])
            for n in sorted(s.union_names() - have):
                if n[len(CARD_PREFIX):-len(".json")] in dead:
                    continue
                copies = s.copies_of(n)
                cids = {c["cid"].lower() for _, c in copies if c["cid"]}
                if len(cids) > 1:
                    conflicts.append({"store": name, "name": n,
                                      "session_ids": sorted(cids)})
                    continue
                best = max(copies, key=lambda rc: _rank(rc[1]))
                plan.append({"store": name, "name": n,
                             "src": os.path.join(best[0], n), "dst_dir": dst,
                             "create_dir": create})
    return plan, conflicts


def _clone(src, dst):
    """Copy with APFS cloning when available; fall back to a plain copy."""
    if sys.platform == "darwin":
        r = subprocess.run(["cp", "-c", src, dst], capture_output=True)
        if r.returncode == 0:
            return
    shutil.copyfile(src, dst)


class Journal(object):
    def __init__(self, path):
        self.fh = open_new_file(path)      # exclusive, never through a symlink

    def add(self, **rec):
        self.fh.write(json.dumps(rec, sort_keys=True) + "\n")
        self.fh.flush()
        os.fsync(self.fh.fileno())

    def close(self):
        self.fh.close()


def new_journal_path(state_dir):
    return os.path.join(state_dir, "sync-%s-%s.jsonl" % (
        time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:8]))


def apply_plan(plan, journal_path, home_id="unbound"):
    """Copy without ever replacing a file. Returns (copied, failed)."""
    copied, failed = 0, 0
    op_id = uuid.uuid4().hex
    jr = Journal(journal_path)
    try:
        jr.add(op="begin", version=1, op_id=op_id, home_id=home_id,
               at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        for item in plan:
            dst = os.path.join(item["dst_dir"], item["name"])
            tmp = os.path.join(item["dst_dir"], ".tmp-move-%s-%s"
                               % (op_id[:8], item["name"]))
            try:
                if item.get("create_dir"):
                    os.makedirs(item["dst_dir"], exist_ok=True)
                _clone(item["src"], tmp)
                st = os.stat(tmp)
                jr.add(op="intent", path=dst, src=item["src"],
                       sha256=sha256_file(tmp), dev=st.st_dev, ino=st.st_ino)
                os.link(tmp, dst)      # fails if dst exists: never overwrite
                jr.add(op="done", path=dst)
                copied += 1
            except OSError as exc:
                jr.add(op="failed", path=dst, error=str(exc)[:80])
                failed += 1
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    finally:
        jr.close()
    return copied, failed


def read_journal(path):
    """(records, status). status: ok | torn_tail | damaged.

    The intact prefix is always returned. A bad LAST line is a torn tail (an
    interrupted write); a bad line followed by more lines is damage.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise JournalError("journal unreadable: %s" % exc)
    if lines and lines[-1] == "":
        lines.pop()
    recs = []
    for n, line in enumerate(lines):
        try:
            rec = json.loads(line)
            if not isinstance(rec, dict) or not isinstance(rec.get("op"), str):
                raise ValueError("not a record")
        except ValueError:
            return recs, ("torn_tail" if n == len(lines) - 1 else "damaged")
        recs.append(rec)
    return recs, "ok"


def _valid_intent(r):
    return (isinstance(r.get("path"), str) and isinstance(r.get("sha256"), str)
            and isinstance(r.get("dev"), int) and isinstance(r.get("ino"), int)
            and not isinstance(r.get("dev"), bool))


def undo(journal_path, home_id, support_real):
    """Remove exactly the files this journal's operation published.

    Returns {"removed","skipped","ambiguous","status"}. Raises JournalError
    when the journal is unusable or belongs to another home directory.
    """
    recs, status = read_journal(journal_path)
    if not recs or recs[0].get("op") != "begin":
        raise JournalError("journal has no begin record")
    if recs[0].get("home_id") != home_id:
        raise JournalError("journal belongs to another home directory")
    failed = {r.get("path") for r in recs if r.get("op") == "failed"}
    out = {"removed": 0, "skipped": 0, "ambiguous": 0, "status": status}
    for r in recs:
        if r.get("op") != "intent":
            continue
        if not _valid_intent(r):
            out["skipped"] += 1
            continue
        p = r["path"]
        parent = os.path.realpath(os.path.dirname(p))
        base = os.path.basename(p)
        if p in failed or not base.startswith(CARD_PREFIX) \
                or not base.endswith(".json") or not inside(parent, support_real):
            out["skipped"] += 1
            continue
        try:
            st = os.lstat(p)
        except OSError:
            out["skipped"] += 1           # already gone
            continue
        owned = (os.path.isfile(p) and not os.path.islink(p)
                 and (st.st_dev, st.st_ino) == (r["dev"], r["ino"])
                 and sha256_file(p) == r["sha256"])
        if owned:
            try:
                os.unlink(p)
                out["removed"] += 1
            except OSError:
                out["skipped"] += 1
        else:
            out["ambiguous"] += 1         # same name, not proven ours: keep it
    return out
