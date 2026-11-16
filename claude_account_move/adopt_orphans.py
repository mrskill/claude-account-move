"""Give a card to every session the app has no local card for (orphans).

An orphan is a transcript `<projects>/<dir>/<id>.jsonl` that

  (a) holds at least one real user prompt (a user line whose message content is
      a string, not a tool result and not an injected hook text),
  (b) is not the `cliSessionId` of any card in any directory of any tracked
      store (any account, any organization), and
  (c) carries a `session-owner` line, or has a version-5 session id.

Such a session lives in the panel of the old account outside the disk, so it
vanishes from the panel after an account change and no other check sees it.

`plan` and `find` only read. `apply_plan` writes one new card per orphan into
the ACTIVE pair of `claude-code-sessions`: a deep copy of a template (the newest
parsable card of that pair) with the fields of the session set. Every write is
journaled before it happens (intent), goes through a temp file in the same
folder and a no-replace hard link (never over an existing file), and is
acknowledged after it (done). `undo` removes only files that journal says it
created, and only while they still hold the recorded sha256 and inode.
Transcripts are never written.

The first lines of a transcript are enough to see a `session-owner` line (it is
the first line of every transcript measured): only the first HEAD_BYTES are
searched, so a store of thousands of transcripts is not read end to end.
"""
import copy
import glob
import hashlib
import json
import os
import re
import stat
import time
import uuid

from . import panel_snapshot as snap
from . import store as st
from .common import (CARD_PREFIX, STORES, TRANSCRIPT_RE, fmt_ms, inside,
                     iso_to_ms, open_new_file, safe_subdir, sha256_file)

JOURNAL_VERSION = 1
HEAD_BYTES = 64 * 1024
OWNER_RE = re.compile(rb'"type"\s*:\s*"session-owner"')
V5_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$")
FIRST_SHOWN = 80
TITLE_CUT = 60
TMP_PREFIX = ".claude-account-move-adopt-"


class JournalError(Exception):
    pass


class PairProblem(Exception):
    """No active pair, or it cannot be written: nothing is written."""


def _squash(text, limit):
    """One line, whitespace collapsed, cut to `limit` characters."""
    return " ".join(text.split())[:limit]


def card_ids(index):
    """Lower-case cliSessionId of every card of every directory of every store."""
    out = set()
    for s in index.stores.values():
        for d in s.dirs.values():
            for c in d["cards"].values():
                if c["cid"]:
                    out.add(c["cid"].lower())
    return out


def _head_has_owner(path):
    try:
        with open(path, "rb") as fh:
            return bool(OWNER_RE.search(fh.read(HEAD_BYTES)))
    except OSError:
        return False


def _real_prompt(rec):
    if rec.get("type") != "user" or rec.get("isMeta"):
        return None
    c = (rec.get("message") or {}).get("content")
    if not isinstance(c, str) or not c.strip():
        return None
    if c.lstrip().startswith(st.INJECTED_PREFIXES):
        return None
    return c


def read_transcript(path):
    """Facts of one transcript, or None when it has no real prompt or cannot be
    read. Read only."""
    first_prompt = None
    prompts = 0
    cwd = None
    first_ms = last_ms = None
    custom = ai = None
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ms = iso_to_ms(rec.get("timestamp"))
                if ms is not None:
                    if first_ms is None:
                        first_ms = ms
                    last_ms = ms
                if cwd is None and isinstance(rec.get("cwd"), str) and rec["cwd"]:
                    cwd = rec["cwd"]
                kind = rec.get("type")
                if kind == "custom-title":
                    t = rec.get("customTitle") or rec.get("title")
                    if isinstance(t, str) and t.strip():
                        custom = t
                elif kind == "ai-title":
                    t = rec.get("aiTitle") or rec.get("title")
                    if isinstance(t, str) and t.strip():
                        ai = t
                text = _real_prompt(rec)
                if text is not None:
                    prompts += 1
                    if first_prompt is None:
                        first_prompt = text
    except OSError:
        return None
    if not prompts:
        return None
    title = custom or ai or first_prompt
    return {"cwd": cwd, "first": _squash(first_prompt, FIRST_SHOWN),
            "title": _squash(title, TITLE_CUT), "prompts": prompts,
            "created_ms": first_ms, "last_ms": last_ms}


def _transcript_files(paths):
    """{id: path}: when several project folders hold the same id, the newest."""
    out = {}
    for f in sorted(glob.glob(os.path.join(paths.projects, "*", "*.jsonl"))):
        base = os.path.basename(f)
        if not TRANSCRIPT_RE.match(base):
            continue
        sid = base[:-len(".jsonl")]
        try:
            mt = os.stat(f).st_mtime_ns
        except OSError:
            continue
        if sid not in out or mt > out[sid][0]:
            out[sid] = (mt, f)
    return {sid: v[1] for sid, v in out.items()}


def find(paths, index):
    """Orphans, newest activity first: dicts with id, path, cwd, first, title,
    prompts, created_ms, last_ms. Read only."""
    have = card_ids(index)
    out = []
    for sid, path in sorted(_transcript_files(paths).items()):
        if sid in have:
            continue
        if not (V5_RE.match(sid) or _head_has_owner(path)):
            continue
        facts = read_transcript(path)
        if facts is None:
            continue
        facts.update(id=sid, path=path)
        out.append(facts)
    out.sort(key=lambda o: (-(o["last_ms"] or 0), o["id"]))
    return out


NOTE_NAMED_MAX = 20


def readiness_notes(orphans):
    """Plain note lines for `prepare`: the count and every orphan by name (first
    prompt and id), capped; [] when there are none."""
    if not orphans:
        return []
    lines = ["  note: %d sessions have no card in the app (orphans); they will "
             "vanish from the panel after the move:" % len(orphans)]
    for o in orphans[:NOTE_NAMED_MAX]:
        lines.append("    - %s (%s, %s)" % (o["first"], o["id"], o["cwd"] or "-"))
    if len(orphans) > NOTE_NAMED_MAX:
        lines.append("    ... and %d more" % (len(orphans) - NOTE_NAMED_MAX))
    return lines


def describe(o):
    """One plain line for one orphan."""
    return "orphan %s  cwd %s  last %s  prompts %d  first: %s" % (
        o["id"], o["cwd"] or "-", fmt_ms(o["last_ms"]), o["prompts"], o["first"])


# --------------------------------------------------------------- active pair ---

def active_pair(paths, identity, override=None):
    """(acc dir, org dir) of the active claude-code-sessions pair, or raise
    PairProblem with a plain reason."""
    from . import auto_move              # late: auto_move imports this module
    if override:
        want = st.parse_override(override)
    else:
        want = None
        app_acc = (identity.get("app") or {}).get("value")
        if app_acc:                    # the pair the app log names, as sync does
            want = st.live_pair(paths, account=app_acc)
        if want is None:               # else the organization of the CLI account
            cli_pair = auto_move.active_cc_pair(
                paths, (identity.get("cli") or {}).get("value"))
            want = tuple(x.lower() for x in cli_pair) if cli_pair else None
    if want is None:
        raise PairProblem("no active pair of claude-code-sessions is known (the "
                          "app log and the CLI account name none): nothing to "
                          "write into")
    pair = None
    root = os.path.join(paths.support, STORES[0])
    try:
        for a in sorted(os.listdir(root)):
            if a.lower() != want[0]:
                continue
            for o in sorted(os.listdir(os.path.join(root, a))):
                if o.lower() == want[1]:
                    pair = (a, o)
    except OSError:
        pair = None
    if pair is None:
        raise PairProblem("the active pair %s/%s does not exist in "
                          "claude-code-sessions" % want)
    why = auto_move.real_pair_problem(paths, STORES[0], pair)
    if why:
        raise PairProblem("the active pair is not a plain directory (%s); "
                          "run heal-active first" % why)
    return pair


def pick_template(pair_dir):
    """(path, parsed dict) of the newest parsable card of the pair, or raise
    PairProblem. Newest is by lastActivityAt, then by file time."""
    best = None
    try:
        names = sorted(os.listdir(pair_dir))
    except OSError as exc:
        raise PairProblem("cannot list the active pair: %s" % exc)
    for n in names:
        if not (n.startswith(CARD_PREFIX) and n.endswith(".json")):
            continue
        p = os.path.join(pair_dir, n)
        try:
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
            mtime = os.stat(p).st_mtime_ns
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        act = doc.get("lastActivityAt")
        act = act if isinstance(act, (int, float)) and not isinstance(act, bool) else 0
        key = (act, mtime, n)
        if best is None or key > best[0]:
            best = (key, p, doc)
    if best is None:
        raise PairProblem("no parsable card in the active pair to copy the card "
                          "format from: nothing written")
    return best[1], best[2]


def _swap_ids(value, lid, new_id, new_name):
    """Deep copy of `value` with every string equal to the template's own local id
    (bare, with local_ and with local_ ... .json) replaced by the new one."""
    if isinstance(value, str):
        if value == lid:
            return new_id
        if value == CARD_PREFIX + lid:
            return CARD_PREFIX + new_id
        if value == CARD_PREFIX + lid + ".json":
            return new_name
        return value
    if isinstance(value, list):
        return [_swap_ids(v, lid, new_id, new_name) for v in value]
    if isinstance(value, dict):
        return {k: _swap_ids(v, lid, new_id, new_name) for k, v in value.items()}
    return value


def build_card(template_path, template, orphan, new_id):
    """The new card (a dict): a deep copy of the template with the session set.
    Timestamps and counts are ints, never digit strings."""
    name = "%s%s.json" % (CARD_PREFIX, new_id)
    lid = os.path.basename(template_path)[len(CARD_PREFIX):-len(".json")]
    card = _swap_ids(copy.deepcopy(template), lid, new_id, name)
    card["cliSessionId"] = orphan["id"]
    card["cwd"] = orphan["cwd"] or card.get("cwd", "")
    card["title"] = orphan["title"]
    if orphan["created_ms"] is not None:
        card["createdAt"] = int(orphan["created_ms"])
    if orphan["last_ms"] is not None:
        card["lastActivityAt"] = int(orphan["last_ms"])
    card["completedTurns"] = int(orphan["prompts"])
    return name, card


# ------------------------------------------------------------------- journal ---

class Journal(object):
    """One new JSON-lines file per run, flushed and synced on every line."""

    def __init__(self, path, op_id, home_id):
        self.path = path
        self.op_id = op_id
        self.fh = open_new_file(path)
        self.home_id = home_id

    def write(self, op, **fields):
        rec = {"op": op, "op_id": self.op_id}
        rec.update(fields)
        self.fh.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
        self.fh.flush()
        os.fsync(self.fh.fileno())

    def close(self):
        self.fh.close()


def new_journal_path(jdir):
    return os.path.join(jdir, "adopt-journal-%s-%s.jsonl" % (
        time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:8]))


def _tmp_name(pair_dir, n):
    return os.path.join(pair_dir, "%s%d-%d-%s" % (
        TMP_PREFIX, os.getpid(), n, uuid.uuid4().hex[:6]))


def apply_plan(paths, orphans, pair, template_path, template):
    """Write one card per orphan. Returns {"journal","created","refused",
    "failed","entries"}. `created` entries carry the path, name and orphan id."""
    jdir = safe_subdir(paths)
    pair_dir = os.path.realpath(os.path.join(paths.support, STORES[0], pair[0], pair[1]))
    support_real = os.path.realpath(paths.support)
    if not inside(pair_dir, support_real):
        raise PairProblem("the active pair resolves outside the support folder")
    mode = stat.S_IMODE(os.stat(template_path).st_mode)
    op_id = uuid.uuid4().hex
    jr = Journal(new_journal_path(jdir), op_id, snap.path_id(paths.home))
    entries, created, refused, failed = [], [], [], []
    try:
        jr.write("begin", version=JOURNAL_VERSION, home_id=jr.home_id,
                 pair=list(pair), dir=pair_dir,
                 template=os.path.basename(template_path))
        for i, o in enumerate(orphans):
            new_id = str(uuid.uuid4())
            name, card = build_card(template_path, template, o, new_id)
            data = (json.dumps(card, ensure_ascii=False) + "\n").encode("utf-8")
            digest = hashlib.sha256(data).hexdigest()
            final = os.path.join(pair_dir, name)
            tmp = _tmp_name(pair_dir, i)
            jr.write("intent", orphan=o["id"], name=name, path=final,
                     sha256=digest, tmp=tmp)           # journaled before the write
            entry = {"orphan": o["id"], "name": name, "path": final,
                     "sha256": digest}
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp, mode)
                try:
                    os.link(tmp, final)                # never replaces an existing name
                except FileExistsError:
                    entry["status"] = "refused"
                    jr.write("refused", orphan=o["id"], name=name, path=final,
                             why="the name exists")
                    refused.append(entry)
                    entries.append(entry)
                    continue
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                if sha256_file(final) != digest:
                    raise OSError("the card read back differs from what was written")
                entry["inode"] = os.stat(final).st_ino
                entry["status"] = "created"
                jr.write("done", orphan=o["id"], name=name, path=final,
                         sha256=digest, inode=entry["inode"])
                created.append(entry)
            except OSError as exc:
                entry["status"] = "failed"
                entry["error"] = str(exc)
                jr.write("failed", orphan=o["id"], name=name, path=final,
                         error=str(exc))
                failed.append(entry)
            entries.append(entry)
    finally:
        jr.close()
    return {"journal": jr.path, "created": created, "refused": refused,
            "failed": failed, "entries": entries}


# ---------------------------------------------------------------------- undo ---

def _read_journal(path, paths):
    try:
        with open(path, encoding="utf-8") as fh:
            recs = [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError) as exc:
        raise JournalError("cannot read the journal: %s" % exc)
    if not recs or not isinstance(recs[0], dict) or recs[0].get("op") != "begin":
        raise JournalError("not an adopt journal (no begin record)")
    begin = recs[0]
    if begin.get("home_id") != snap.path_id(paths.home):
        raise JournalError("journal belongs to another home directory")
    for r in recs:
        if not isinstance(r, dict) or r.get("op_id") != begin.get("op_id"):
            raise JournalError("journal mixes operations")
    return recs


def undo(paths, journal):
    """Remove the cards this journal created, when unchanged (same sha256 and
    inode). Returns {"removed","gone","ambiguous","removed_paths","ambiguous_paths"}."""
    recs = _read_journal(journal, paths)
    support_real = os.path.realpath(paths.support)
    done = {r["path"]: r for r in recs if r.get("op") == "done"
            and isinstance(r.get("path"), str)}
    intents = {r["path"]: r for r in recs if r.get("op") == "intent"
               and isinstance(r.get("path"), str)}
    removed, gone = [], 0
    ambiguous = []
    for path, rec in sorted(done.items()):
        real_parent = os.path.realpath(os.path.dirname(path))
        if not inside(real_parent, support_real):
            ambiguous.append(path)
            continue
        try:
            lst = os.lstat(path)
        except FileNotFoundError:
            gone += 1
            continue
        except OSError:
            ambiguous.append(path)
            continue
        if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
            ambiguous.append(path)
            continue
        try:
            same = (sha256_file(path) == rec.get("sha256")
                    and lst.st_ino == rec.get("inode"))
        except OSError:
            same = False
        if not same:
            ambiguous.append(path)
            continue
        try:
            os.unlink(path)
            removed.append(path)
        except OSError:
            ambiguous.append(path)
    for path in sorted(set(intents) - set(done)):
        # an intent with no done line: the write may or may not have happened
        if os.path.lexists(path):
            ambiguous.append(path)
    return {"removed": len(removed), "gone": gone, "ambiguous": len(ambiguous),
            "removed_paths": removed, "ambiguous_paths": ambiguous}
