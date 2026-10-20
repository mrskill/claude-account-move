"""One pass over the Claude desktop app storage.

The app keeps one card per session (`local_<id>.json`) in account/organization
directories. Many of those names are symlinks onto a few real directories, so
everything here is keyed by the resolved path: a card reachable through ten
aliases is one card.

Reading is strictly read-only. Problems that would make a count untrustworthy
(unreadable directory, malformed card, link pointing outside the store) are
recorded in `Index.obs` and never turned into silent zeros.
"""
import glob
import json
import os
import re

from .common import (CARD_PREFIX, STORES, TOMB_PREFIX, TRANSCRIPT_RE, UUID_RE,
                     inside, iso_to_ms)

TAIL_BYTES = 400000
INJECTED_PREFIXES = ("<system-reminder", "<local-command",
                     "<cross-session-message", "Stop hook feedback",
                     "SessionStart:", "UserPromptSubmit hook",
                     "[Request interrupted")
PAIR_RE = re.compile(r"claude-code-sessions/([0-9a-fA-F-]{36})/([0-9a-fA-F-]{36})")


class Observation(object):
    def __init__(self):
        self.errors = []
        self.unreadable_cards = 0
        self.excluded_outside = 0
        self.excluded_dangling = 0
        self.excluded_loops = 0

    def complete(self):
        return not (self.errors or self.unreadable_cards)

    def as_dict(self):
        return {"errors": self.errors[:20],
                "unreadable_cards": self.unreadable_cards,
                "excluded_outside": self.excluded_outside,
                "excluded_dangling": self.excluded_dangling,
                "excluded_loops": self.excluded_loops}


class StoreInfo(object):
    def __init__(self, name, base):
        self.name = name
        self.base = base
        self.exists = os.path.isdir(base)
        self.aliases = []   # {"acc","org","kind","target"}
        self.dirs = {}      # realpath -> {"cards": {}, "tombs": set(), "tasks": bool}

    def rel(self, real):
        return os.path.relpath(real, os.path.realpath(self.base))

    def union_names(self):
        out = set()
        for d in self.dirs.values():
            out |= set(d["cards"])
        return out

    def tombstone_ids(self):
        out = set()
        for d in self.dirs.values():
            out |= d["tombs"]
        return out

    def copies_of(self, name):
        """[(realpath, card fields)] for every physical directory holding name."""
        return [(r, d["cards"][name]) for r, d in sorted(self.dirs.items())
                if name in d["cards"]]

    def best_card(self, name):
        """Newest copy by (activity, completed turns), or None."""
        copies = self.copies_of(name)
        if not copies:
            return None
        return max((c for _, c in copies), key=lambda c: (c["act"], c["turns"]))


class Index(object):
    def __init__(self):
        self.stores = {}
        self.obs = Observation()

    def any_pairs(self):
        return any(s.aliases for s in self.stores.values())


def read_card(path):
    """Card fields we need; the whole document is parsed so damage is seen."""
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    if not isinstance(d, dict):
        raise ValueError("card is not an object")
    cid = d.get("cliSessionId")
    cid = cid.strip() if isinstance(cid, str) else ""
    act = d.get("lastActivityAt")
    turns = d.get("completedTurns")
    title = d.get("title")
    return {"cid": cid,
            "act": int(act) if isinstance(act, (int, float)) else 0,
            "turns": int(turns) if isinstance(turns, (int, float)) else 0,
            "title": title if isinstance(title, str) else ""}


# Timestamp fields the app validates as numbers (a digit string makes it reject
# the WHOLE file). Measured 16.10.2026: createdAt "1787838292655" -> ZodError.
TASK_INT_FIELDS = ("createdAt",)


def task_type_issues(entry):
    """[(field, expected)] for fields of a task entry with the wrong type.

    The ONE validator: `finish` reports its result, `sync` coerces what it can.
    """
    out = []
    for f in TASK_INT_FIELDS:
        if f in entry and (isinstance(entry[f], bool)
                           or not isinstance(entry[f], (int, float))):
            out.append((f, "a number"))
    if "enabled" in entry and not isinstance(entry["enabled"], bool):
        out.append(("enabled", "a boolean"))
    return out


def coerce_task(entry):
    """Digit strings in number fields become int. True if anything changed."""
    changed = False
    for f, expected in task_type_issues(entry):
        v = entry.get(f)
        if expected == "a number" and isinstance(v, str) and v.isdigit():
            entry[f] = int(v)
            changed = True
    return changed


def task_problems(path):
    """["<id>: <field> is <type>, expected ...", ...] for wrongly typed fields.

    Tolerant: an unreadable or differently shaped file gives [] here because
    read_tasks_file reports that case.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            tasks = json.load(fh)["scheduledTasks"]
        return ["%s: %s is %s, expected %s"
                % (t.get("id"), f, type(t[f]).__name__, expected)
                for t in tasks for f, expected in task_type_issues(t)]
    except Exception:
        return []


def read_tasks_file(path):
    """{task id: {"enabled", "approved"}}; raises when the file is damaged.

    A valid empty list is an empty result. Anything that is not the expected
    schema (collection of objects, each with an id) raises, so a layout change
    can never be mistaken for "no tasks".
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("tasks file is not an object")
    if "scheduledTasks" not in data:
        return {}
    tasks = data["scheduledTasks"]
    if not isinstance(tasks, list):
        raise ValueError("scheduledTasks is not a list")
    out = {}
    for t in tasks:
        if not isinstance(t, dict):
            raise ValueError("task entry is not an object")
        tid = t.get("id")
        if isinstance(tid, bool) or not isinstance(tid, (str, int)) or tid == "":
            raise ValueError("task entry without a usable id")
        if "enabled" in t and not isinstance(t["enabled"], bool):
            raise ValueError("task enabled is not a boolean")
        ap = t.get("approvedPermissions")
        if ap is not None and not isinstance(ap, list):
            raise ValueError("approvedPermissions is not a list")
        if str(tid) in out:
            raise ValueError("duplicate task id")
        out[str(tid)] = {"enabled": t.get("enabled") is not False,
                         "approved": len(ap) if ap else 0}
    return out


def _read_dir(real, obs, tick=None):
    info = {"cards": {}, "tombs": set(), "tasks": False, "task_map": {},
            "task_problems": []}
    try:
        names = sorted(os.listdir(real))
    except OSError as exc:
        obs.errors.append("listdir %s: %s" % (real, exc))
        return info
    for n in names:
        p = os.path.join(real, n)
        if n.startswith(CARD_PREFIX) and n.endswith(".json"):
            if tick:
                tick()
            try:
                info["cards"][n] = read_card(p)
            except Exception:
                obs.unreadable_cards += 1
                obs.errors.append("card unreadable: %s" % p)
        elif n.startswith(TOMB_PREFIX) and os.path.isfile(p) \
                and not os.path.islink(p):
            info["tombs"].add(n[len(TOMB_PREFIX):])
        elif n == "scheduled-tasks.json":
            info["tasks"] = True
            info["task_problems"] = task_problems(p)
            try:
                info["task_map"] = read_tasks_file(p)
            except Exception:
                obs.errors.append("scheduled-tasks.json unreadable: %s" % p)
    return info


def _candidate_dirs(base, base_real, obs=None):
    """Yield (acc, org, path, real, kind) for each org dir of one store.

    The single walk shared by scan() and _count_cards(), so the two cannot
    diverge. obs, when given, records errors and exclusions; OSError is
    handled per listing, never around the whole walk.
    """
    try:
        accs = sorted(os.listdir(base))
    except OSError as exc:
        if obs is not None:
            obs.errors.append("listdir %s: %s" % (base, exc))
        return
    for acc in accs:
        if not UUID_RE.match(acc):
            continue
        pa = os.path.join(base, acc)
        if os.path.islink(pa) and not os.path.exists(pa):
            if obs is not None:
                obs.excluded_dangling += 1
            continue
        if not os.path.isdir(pa):
            continue
        try:
            orgs = sorted(os.listdir(pa))
        except OSError as exc:
            if obs is not None:
                obs.errors.append("listdir %s: %s" % (pa, exc))
            continue
        for org in orgs:
            if not UUID_RE.match(org):
                continue
            po = os.path.join(pa, org)
            if os.path.islink(po) and not os.path.exists(po):
                if obs is not None:
                    obs.excluded_dangling += 1
                continue
            if not os.path.isdir(po):
                continue
            real = os.path.realpath(po)
            if not inside(real, base_real):
                if obs is not None:
                    obs.excluded_outside += 1
                continue
            kind = "link" if (os.path.islink(po) or os.path.islink(pa)) \
                else "dir"
            yield acc, org, po, real, kind


def _count_cards(paths):
    """Cheap pre-count of card files for the progress bar (listdir only)."""
    total = 0
    support_real = os.path.realpath(paths.support)
    for store in STORES:
        base = os.path.join(paths.support, store)
        base_real = os.path.realpath(base)
        if not os.path.isdir(base) or not inside(base_real, support_real):
            continue
        seen = set()
        for _acc, _org, _po, real, _kind in _candidate_dirs(base, base_real):
            if real in seen:
                continue
            seen.add(real)
            try:
                total += sum(1 for n in os.listdir(real)
                             if n.startswith(CARD_PREFIX) and n.endswith(".json"))
            except OSError:
                continue
    return total


def scan(paths, progress=None):
    """progress(done, total): optional, counts the card files read."""
    idx = Index()
    obs = idx.obs
    tick = None
    if progress:
        total, state = _count_cards(paths), [0]

        def tick():
            state[0] += 1
            progress(state[0], max(total, state[0]))
    for store in STORES:
        base = os.path.join(paths.support, store)
        s = StoreInfo(store, base)
        idx.stores[store] = s
        if not s.exists:
            continue
        base_real = os.path.realpath(base)
        if not inside(base_real, os.path.realpath(paths.support)):
            obs.excluded_outside += 1
            obs.errors.append("store root resolves outside the support folder: "
                              "%s -> %s" % (base, base_real))
            continue
        for acc, org, _po, real, kind in _candidate_dirs(base, base_real, obs):
            s.aliases.append({"acc": acc.lower(), "org": org.lower(),
                              "kind": kind, "target": real})
            if real not in s.dirs:
                s.dirs[real] = _read_dir(real, obs, tick)
    return idx


def transcripts(paths, progress=None):
    """{session id (lower case): [transcript files]} from both known places.
    progress(done, total): optional, counts the files looked at."""
    out = {}
    pats = [os.path.join(paths.projects, "*", "*.jsonl"),
            os.path.join(paths.support, "*-sessions", "*", "*", "local_*",
                         ".claude", "projects", "*", "*.jsonl")]
    found = [f for pat in pats for f in glob.glob(pat)]
    for i, f in enumerate(found, 1):
        if progress and (i % 50 == 0 or i == len(found)):
            progress(i, len(found))
        base = os.path.basename(f)
        if TRANSCRIPT_RE.match(base):
            out.setdefault(base[:-6], []).append(f)
    return out


def app_pairs(paths, account=None):
    """Pairs named in the app log, in order of appearance (acc, org)."""
    try:
        with open(paths.main_log, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024 * 1024))
            text = fh.read().decode("utf-8", "ignore")
    except OSError:
        return None
    hits = [(a.lower(), o.lower()) for a, o in PAIR_RE.findall(text)]
    if account:
        hits = [h for h in hits if h[0] == account.lower()]
    return hits


class PairError(Exception):
    """An explicit --active-pair that cannot be trusted."""


def parse_override(override):
    """(acc, org) from an explicit ACCOUNT/ORG, or PairError."""
    parts = override.lower().split("/")
    if len(parts) != 2 or not (UUID_RE.match(parts[0]) and UUID_RE.match(parts[1])):
        raise PairError("--active-pair must look like ACCOUNT/ORG (two UUIDs)")
    return parts[0], parts[1]


AMBIGUOUS_WINDOW_MS = 60 * 1000


def auto_pair(index, account):
    """Choose the active pair from storage when the app log names none.

    Returns (pair, note, candidates). pair is (acc, org) or None. note is one
    plain sentence. candidates is [(acc, org, newest_card_ms)] for the account,
    newest first. The choice is the org dir of the app account whose newest
    session card is the most recent; it is refused (pair None) when the account
    is unknown, has no cards, or the two newest org dirs are within 60 s.
    """
    if not account:
        return None, "the app account is not readable, so no pair can be chosen", []
    account = account.lower()
    s = index.stores[STORES[0]]
    best = {}
    for al in s.aliases:
        if al["acc"] != account:
            continue
        d = s.dirs.get(al["target"]) or {"cards": {}}
        newest = max([c["act"] for c in d["cards"].values()] or [0])
        key = (al["acc"], al["org"])
        best[key] = max(best.get(key, 0), newest)
    cands = sorted(((a, o, ms) for (a, o), ms in best.items()),
                   key=lambda x: -x[2])
    if not cands:
        return None, "no organization folder of account %s was found" % account[:8], []
    with_cards = [c for c in cands if c[2] > 0]
    if not with_cards:
        return None, ("organization folders of account %s hold no session "
                      "cards, so none can be chosen" % account[:8]), cands
    if len(with_cards) > 1 and with_cards[0][2] - with_cards[1][2] < AMBIGUOUS_WINDOW_MS:
        return None, ("two organization folders of account %s have cards within "
                      "60 s of each other; not guessing" % account[:8]), cands
    a, o, ms = with_cards[0]
    return (a, o), ("chose pair %s/%s: account matches the app account and this "
                    "folder holds the most recent session card"
                    % (a[:8], o[:8])), cands


def candidates_line(cands):
    """Plain list of candidates plus one ready-to-paste command line."""
    if not cands:
        return "no candidates"
    shown = ", ".join("%s/%s" % (a[:8], o[:8]) for a, o, _ms in cands)
    a, o, _ms = cands[0]
    return "candidates: %s. Paste one: --active-pair %s/%s" % (shown, a, o)


def live_pair(paths, account=None, override=None, index=None, notes=None,
              auto_account=None):
    """(acc, org) the app writes into, or None when it cannot be determined.

    The last mention in the app log wins. With `account`, only mentions of that
    account count; the organization is never taken from the CLI config. An
    explicit override must be well formed and, when an account is expected,
    must belong to that account: a typo must not turn the old account into the
    target. When the log names no pair and `index` is given, the pair is chosen
    from storage by auto_pair; the plain-words explanation is appended to
    `notes` (a list) whether the choice was made or refused.
    """
    if override:
        parts = parse_override(override)
        if account and parts[0] != account.lower():
            raise PairError("--active-pair account %s differs from the expected "
                            "account %s" % (parts[0], account.lower()))
        return parts
    hits = app_pairs(paths, account)
    if not hits:
        if index is not None:
            pair, note, cands = auto_pair(index, account or auto_account)
            if notes is not None:
                notes.append(note if pair else "%s. %s" % (note, candidates_line(cands)))
            return pair
        return None
    return hits[-1]


def has_pairs(paths):
    """Cheap test: is there at least one account/organization directory?"""
    for store in ("claude-code-sessions", "local-agent-mode-sessions"):
        base = os.path.join(paths.support, store)
        try:
            for acc in os.listdir(base):
                if UUID_RE.match(acc) and os.path.isdir(os.path.join(base, acc)):
                    for org in os.listdir(os.path.join(base, acc)):
                        if UUID_RE.match(org) and os.path.isdir(
                                os.path.join(base, acc, org)):
                            return True
        except OSError:
            continue
    return False


def pair_dir(paths, acc, org, store=STORES[0]):
    p = os.path.join(paths.support, store, acc, org)
    if not os.path.isdir(p):
        # the directory name may differ in case from the lower-cased log text
        base = os.path.join(paths.support, store)
        try:
            for a in os.listdir(base):
                if a.lower() == acc:
                    for o in os.listdir(os.path.join(base, a)):
                        if o.lower() == org:
                            return os.path.realpath(os.path.join(base, a, o))
        except OSError:
            pass
        return None
    return os.path.realpath(p)


def is_real_turn(rec):
    if not isinstance(rec, dict):
        return False
    if rec.get("type") not in ("user", "assistant") or rec.get("isMeta"):
        return False
    if rec.get("type") == "user":
        c = (rec.get("message") or {}).get("content")
        if isinstance(c, list):
            c = " ".join((x.get("text") or "") for x in c if isinstance(x, dict))
        if isinstance(c, str) and c.lstrip().startswith(INJECTED_PREFIXES):
            return False
    return True


def _last_ts(lines):
    for line in reversed(lines):
        if b'"timestamp"' not in line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if is_real_turn(rec) and rec.get("timestamp"):
            ms = iso_to_ms(rec["timestamp"])
            if ms:
                return ms
    return None


def last_message_ms(path):
    """Time of the last real turn, or None when there is none or it cannot be read."""
    try:
        return _last_message(path)
    except OSError:
        return None


def _last_message(path):
    """Time of the last real user/assistant turn inside a transcript.

    The tail of the file is read first; the whole file only when the tail holds
    no real turn. File modification time is never used. Raises OSError when the
    file cannot be read.
    """
    if True:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            start = max(0, size - TAIL_BYTES)
            fh.seek(start)
            data = fh.read()
            lines = data.split(b"\n")
            if start > 0:
                lines = lines[1:]
            ms = _last_ts(lines)
            if ms is not None or start == 0:
                return ms
            fh.seek(0)
            return _last_ts(fh.read().split(b"\n"))


def session_last_ms(tmap, sid):
    files = tmap.get(sid) or []
    vals = [v for v in (last_message_ms(f) for f in files) if v]
    return max(vals) if vals else None


PREFIX_CAP = 64 * 1024 * 1024


def prefix_digest(path, length):
    """sha256 of the first `length` bytes, or None if the file is shorter/unreadable.

    Transcripts only grow, so the prefix recorded before the move must still be
    byte-identical afterwards, whatever was appended meanwhile.
    """
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            left = length
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    return None
                h.update(chunk)
                left -= len(chunk)
    except OSError:
        return None
    return h.hexdigest()


def tail_parse_errors(path):
    """Number of complete lines in the tail that are not valid JSON."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            start = max(0, size - TAIL_BYTES)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return 0
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]               # first line may be cut
    if lines and lines[-1] == b"":
        lines.pop()                     # file ended with a newline
    elif lines:
        lines.pop()                     # unterminated last line: still being written
    bad = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            json.loads(line)
        except ValueError:
            bad += 1
    return bad


def transcript_evidence(tmap, sids, obs=None):
    """Per-session evidence: size, last message time, prefix digest.

    {sid: {"size", "last_ms", "prefix_len", "prefix_sha256"}} for the largest
    transcript file of each session. A file that cannot be read, or whose tail
    holds lines that are not valid JSON, is an observation error.
    """
    out = {}
    for sid in sids:
        files = tmap.get(sid) or []
        best, last = None, None
        for f in files:
            try:
                size = os.path.getsize(f)
                with open(f, "rb"):
                    pass
            except OSError:
                if obs is not None:
                    obs.errors.append("transcript unreadable: %s" % f)
                continue
            bad = tail_parse_errors(f)
            if bad and obs is not None:
                obs.errors.append("transcript has %d unparsable lines: %s" % (bad, f))
            ms = last_message_ms(f)
            if ms and (last is None or ms > last):
                last = ms
            if best is None or size > best[0]:
                best = (size, f)
        rec = {"size": 0, "last_ms": last, "prefix_len": 0, "prefix_sha256": None}
        if best:
            plen = min(best[0], PREFIX_CAP)
            rec.update(size=best[0], prefix_len=plen,
                       prefix_sha256=prefix_digest(best[1], plen) if plen else None)
        out[sid] = rec
    return out
