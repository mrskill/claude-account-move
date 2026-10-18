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
conflicts). Scheduled tasks are merged by id (see plan_tasks/apply_tasks): entries missing
in the target scheduled-tasks.json are added, target entries are kept, the old
file is backed up first, the new one is written atomically.
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
from .store import coerce_task, task_type_issues  # noqa: F401


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


JOURNAL_VERSION = 1
OP_FIELDS = {
    "begin": (("version", int), ("op_id", str), ("home_id", str), ("targets", list)),
    "intent": (("path", str), ("src", str), ("sha256", str), ("dev", int),
               ("ino", int)),
    "done": (("path", str),),
    "failed": (("path", str),),
    "task_intent": (("path", str), ("backup", str), ("sha256", str),
                    ("dev", int), ("ino", int)),
    "task_done": (("path", str),),
    "task_failed": (("path", str),),
}


class Journal(object):
    def __init__(self, path, op_id="unbound"):
        self.fh = open_new_file(path)      # exclusive, never through a symlink
        self.op_id = op_id

    def add(self, **rec):
        rec["op_id"] = self.op_id
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
    return apply_all(plan, [], journal_path, home_id)[:2]


def apply_all(plan, tplan, journal_path, home_id="unbound"):
    """Cards, then task files, one journal. Returns (copied, failed, tw, tf)."""
    copied, failed, tw, tf = 0, 0, 0, 0
    op_id = uuid.uuid4().hex
    jr = Journal(journal_path, op_id)
    try:
        targets = sorted({os.path.realpath(i["dst_dir"]) for i in list(plan) + list(tplan)})
        jr.add(op="begin", version=JOURNAL_VERSION, home_id=home_id,
               targets=targets, at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
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
        tw, tf = apply_tasks(tplan, jr)
    finally:
        jr.close()
    return copied, failed, tw, tf


def valid_record(rec, op_fields=None):
    """True when a journal record has every field its operation requires."""
    if not isinstance(rec, dict) or not isinstance(rec.get("op"), str):
        return False
    fields = (op_fields or OP_FIELDS).get(rec["op"])
    if fields is None or not isinstance(rec.get("op_id"), str):
        return False
    for key, typ in fields:
        v = rec.get(key)
        if not isinstance(v, typ) or (typ is int and isinstance(v, bool)):
            return False
    if rec["op"] == "begin" and not all(isinstance(t, str) for t in rec["targets"]):
        return False
    return True


def read_journal(path, op_fields=None):
    """(records, status). status: ok | torn_tail | damaged.

    The intact, fully validated prefix is always returned. A last line that is
    not valid JSON is a torn tail (an interrupted write); a bad line followed
    by more lines, or a record that is valid JSON but misses required fields,
    is damage.
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
        except ValueError:
            return recs, ("torn_tail" if n == len(lines) - 1 else "damaged")
        if not valid_record(rec, op_fields):
            return recs, "damaged"
        recs.append(rec)
    return recs, "ok"


def _safe_remove(path, dev, ino, digest):
    """Delete `path` only if it is the very file we published.

    The path is first moved to a private name with one atomic rename. Only the
    file now under that private name is inspected, so nobody can swap the
    destination between the ownership check and the deletion. A file that turns
    out not to be ours is put back without overwriting anything; if the name is
    taken again it stays under the private name and is reported.
    Returns "removed" | "ambiguous" | "gone".
    """
    quarantine = os.path.join(os.path.dirname(path), ".undo-%s-%s" % (
        uuid.uuid4().hex[:8], os.path.basename(path)))
    try:
        os.rename(path, quarantine)
    except FileNotFoundError:
        return "gone"
    try:
        st = os.lstat(quarantine)
        ours = (os.path.isfile(quarantine) and not os.path.islink(quarantine)
                and (st.st_dev, st.st_ino) == (dev, ino)
                and sha256_file(quarantine) == digest)
        after = os.lstat(quarantine)
        if (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            ours = False          # written through an already-open descriptor
    except OSError:
        ours = False
    if ours:
        os.unlink(quarantine)
        return "removed"
    try:
        os.link(quarantine, path)       # put it back, never overwriting
        os.unlink(quarantine)
    except OSError:
        pass                            # name taken again: keep the quarantined copy
    return "ambiguous"


def _undo_task(r, failed, targets, support_real, out):
    """Put the backed-up tasks file back, or remove the file sync created."""
    p = r["path"]
    parent = os.path.realpath(os.path.dirname(p))
    if os.path.basename(p) != TASKS_NAME or not inside(parent, support_real) \
            or parent not in targets:
        out["invalid"] += 1
        return
    if p in failed:
        out["skipped"] += 1
        return
    path = os.path.join(parent, TASKS_NAME)
    bak = r["backup"]
    if bak and (os.path.dirname(os.path.realpath(bak)) != parent
                or not os.path.isfile(bak)):
        out["ambiguous"] += 1          # backup gone: cannot restore, file kept
        return
    res = _safe_remove(path, r["dev"], r["ino"], r["sha256"])
    if res == "ambiguous":
        out["ambiguous"] += 1          # changed after apply: kept
    elif not bak:
        out["tasks_removed" if res == "removed" else "skipped"] += 1
    else:
        try:
            os.link(bak, path)         # never overwrites; same bytes as before
            os.unlink(bak)
            out["tasks_restored"] += 1
        except OSError:
            out["ambiguous"] += 1


def undo(journal_path, home_id, support_real):
    """Remove exactly the files this journal's operation published.

    Returns {"removed","skipped","ambiguous","invalid","status"}. Raises
    JournalError when the journal is unusable, belongs to another home
    directory, or mixes operations. Every record must carry the begin record's
    operation id, and every destination must lie in a target directory recorded
    at begin.
    """
    recs, status = read_journal(journal_path)
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
    failed = {r["path"] for r in recs if r["op"] in ("failed", "task_failed")}
    out = {"removed": 0, "skipped": 0, "ambiguous": 0, "invalid": 0,
           "status": status, "tasks_restored": 0, "tasks_removed": 0}
    for r in recs[1:]:
        if r["op"] == "begin":
            raise JournalError("journal has a second begin record")
        if r["op"] == "task_intent":
            _undo_task(r, failed, targets, support_real, out)
            continue
        if r["op"] != "intent":
            continue
        p = r["path"]
        parent = os.path.realpath(os.path.dirname(p))
        base = os.path.basename(p)
        if not base.startswith(CARD_PREFIX) or not base.endswith(".json") \
                or not inside(parent, support_real) or parent not in targets:
            out["invalid"] += 1           # outside what this operation targeted
            continue
        if p in failed:
            out["skipped"] += 1
            continue
        res = _safe_remove(os.path.join(parent, base), r["dev"], r["ino"],
                           r["sha256"])
        if res == "removed":
            out["removed"] += 1
        elif res == "gone":
            out["skipped"] += 1           # already gone, harmless
        else:
            out["ambiguous"] += 1         # same name, not proven ours: kept
    return out


TASKS_NAME = "scheduled-tasks.json"


def _load_task_list(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("scheduledTasks"), list):
        raise ValueError("unexpected scheduled-tasks.json shape")
    return data


def _new_binary(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "wb")


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def plan_tasks(index, target_account, active_dir, snapshot_ids, source_account=None,
               pair=None, card_plan=()):
    """[{store, dst_dir, add: [entries], coerce: n, create_dir}].

    Only ids recorded in the move snapshot (`snapshot_ids`: {store: set of ids})
    that are missing in the target are added, so a task the user deleted on
    purpose after the snapshot is never brought back. Targets are the target
    pair directories (existing, or about to be created for cards by the card
    plan); every other directory of the store, including other organizations
    of the target account, is a read-only source. For an id with several
    copies the one under the snapshot's source account wins, then the newest
    file; a target entry always wins over any source entry.
    """
    created = {(i["store"], i["dst_dir"]) for i in card_plan if i.get("create_dir")}
    out = []
    for name in STORES:
        s = index.stores[name]
        wanted = set(snapshot_ids.get(name) or ())
        if pair:
            dirs = [os.path.realpath(os.path.join(s.base, pair[0], pair[1]))]
            dirs = [d for d in dirs if d in s.dirs or (name, d) in created]
        else:
            dirs = [d for d in verify.target_dirs(index, name, target_account,
                                                  active_dir) if d in s.dirs]
        dirs += [d for (n_, d) in sorted(created) if n_ == name and d not in dirs]
        dirs = [d for d in dirs if d in s.dirs or (name, d) in created]
        real_dirs = {os.path.realpath(d) for d in dirs}
        srcs = [d for d in sorted(s.dirs)
                if os.path.realpath(d) not in real_dirs and s.dirs[d]["tasks"]]
        src_acc = (source_account or "").lower()

        def rank(sd):
            acc = os.path.basename(os.path.dirname(sd)).lower()
            return (1 if src_acc and acc == src_acc else 0,
                    _mtime(os.path.join(sd, TASKS_NAME)))
        srcs.sort(key=rank, reverse=True)           # best source first
        for dst in dirs:
            have, coerce = set(), 0
            if dst in s.dirs and s.dirs[dst]["tasks"]:
                for t in _load_task_list(os.path.join(dst, TASKS_NAME))["scheduledTasks"]:
                    have.add(str(t.get("id")))
                    coerce += int(coerce_task(dict(t)))
            add = []
            for sd in srcs:
                for t in _load_task_list(os.path.join(sd, TASKS_NAME))["scheduledTasks"]:
                    tid = str(t.get("id"))
                    if tid in wanted and tid not in have:
                        have.add(tid)
                        add.append(t)
            if add or coerce:
                out.append({"store": name, "dst_dir": dst, "add": add,
                            "coerce": coerce, "create_dir": (name, dst) in created})
    return out


def apply_tasks(plan, jr=None):
    """Merge into each target file, journaling first. Returns (written, failed)."""
    written, failed = 0, 0
    op = uuid.uuid4().hex[:8]
    for item in plan:
        dst = os.path.join(item["dst_dir"], TASKS_NAME)
        tmp = os.path.join(item["dst_dir"], ".tmp-tasks-%s" % op)
        bak = ""
        published = False
        try:
            if item.get("create_dir"):
                os.makedirs(item["dst_dir"], exist_ok=True)
            if os.path.exists(dst):
                data = _load_task_list(dst)
                bak = dst + ".bak-move-" + op
                with open(dst, "rb") as fh, _new_binary(bak) as bf:
                    bf.write(fh.read())
                    bf.flush()
                    os.fsync(bf.fileno())
            else:
                data = {"scheduledTasks": []}
            data["scheduledTasks"].extend(item["add"])
            for t in data["scheduledTasks"]:
                coerce_task(t)
            body = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            with _new_binary(tmp) as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            st = os.stat(tmp)
            if jr:
                jr.add(op="task_intent", path=dst, backup=bak,
                       sha256=sha256_file(tmp), dev=st.st_dev, ino=st.st_ino)
            os.replace(tmp, dst)
            published = True               # from here the file is live
            written += 1
            if jr:
                jr.add(op="task_done", path=dst)
        except (OSError, ValueError, AttributeError):
            if published:
                continue                   # keep backup and intent: undo restores
            if jr:
                try:
                    jr.add(op="task_failed", path=dst)
                except OSError:
                    pass
            failed += 1
            if bak:
                try:
                    os.unlink(bak)
                except OSError:
                    pass
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return written, failed
