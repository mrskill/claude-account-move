"""What is left on this Mac from every Claude account (read-only).

After an account was deleted, or after signing in with another one, the folders
of the old account usually stay on the disk. This module turns one scan of the
storage (`store.scan`), the transcripts (`store.transcripts`) and the two
account files (`common.read_identity`) into a report: per physical folder, per
account, and for the transcripts as a whole. Nothing is written anywhere and
no session title is ever put into the report.
"""
import os
from datetime import datetime, timezone

from .common import EXIT_FAIL, EXIT_OK, STORES

MAX_ERRORS = 20


def rfc3339(ms):
    """RFC 3339 UTC time of a millisecond stamp, or None when there is none.
    Raises ValueError for a stamp that is not a time (negative, out of range)."""
    if not ms:
        return None
    if ms < 0:
        raise ValueError("negative time stamp")
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError) as exc:
        raise ValueError(str(exc))


def _act(card):
    """The card's activity in ms; 0 (no activity) when it is not a time."""
    try:
        rfc3339(card["act"])
    except ValueError:
        return 0
    return card["act"]


def _bad_times(index):
    """Errors for cards whose lastActivityAt is not a time (path, no title)."""
    out = []
    for store in STORES:
        for real, d in sorted(index.stores[store].dirs.items()):
            for name, card in sorted(d["cards"].items()):
                if _act(card) != card["act"]:
                    out.append("card lastActivityAt is not a valid time: %s"
                               % os.path.join(real, name))
    return out


def _has_transcript(card, tmap):
    return bool(card["cid"]) and card["cid"].lower() in tmap


def _basis(index, tmap, picks):
    """(distinct cards, of them with a transcript, newest activity in ms).

    picks is a list of (store, real folder). A card name seen in several of the
    folders counts once per store, as its newest copy (the order `best_card`
    uses), so a count never grows because a session was copied.
    """
    best = {}
    for store, real in picks:
        for name, card in index.stores[store].dirs[real]["cards"].items():
            cur = best.get((store, name))
            if cur is None or (_act(card), card["turns"]) > (_act(cur), cur["turns"]):
                best[(store, name)] = card
    openable = sum(1 for c in best.values() if _has_transcript(c, tmap))
    newest = max((_act(c) for c in best.values()), default=0)
    return len(best), openable, newest


def _folder(index, tmap, store, real):
    """One physical folder: the numbers and every name that reaches it."""
    s = index.stores[store]
    d = s.dirs[real]
    with_t = without_t = no_id = 0
    for card in d["cards"].values():
        if not card["cid"]:
            no_id += 1
        elif _has_transcript(card, tmap):
            with_t += 1
        else:
            without_t += 1
    newest = max((_act(c) for c in d["cards"].values()), default=0)
    aliases = sorted(({"account": a["acc"], "org": a["org"], "kind": a["kind"]}
                      for a in s.aliases if a["target"] == real),
                     key=lambda a: (a["account"], a["org"]))
    return {"store": store, "folder": s.rel(real), "aliases": aliases,
            "cards": len(d["cards"]), "deleted": len(d["tombs"]),
            "with_transcript": with_t, "without_transcript": without_t,
            "no_session_id": no_id, "scheduled_tasks": len(d["task_map"]),
            "last_activity": rfc3339(newest)}


def _identity_errors(identity, paths):
    out = []
    if identity["cli"]["status"] == "invalid":
        out.append("CLI account file unreadable or malformed: %s" % paths.cli_config)
    if identity["app"]["status"] == "invalid":
        out.append("app account file unreadable or malformed: %s" % paths.app_config)
    return out


def build(paths, index, tmap, identity):
    """(report, totals). The report is the JSON document; exit 0 when everything
    was read, else 3. totals counts distinct sessions over all accounts, for the
    verdict line only."""
    picks = sorted(((store, real) for store in STORES
                    for real in index.stores[store].dirs),
                   key=lambda p: (STORES.index(p[0]), index.stores[p[0]].rel(p[1])))
    folders = [_folder(index, tmap, store, real) for store, real in picks]
    where = {pick: i for i, pick in enumerate(picks)}

    app = identity["app"]["value"]
    cli = identity["cli"]["value"]
    reach = {}                              # account -> [(store, real)]
    for store in STORES:
        for a in index.stores[store].aliases:
            reach.setdefault(a["acc"], []).append((store, a["target"]))
    for acc in (app, cli):
        if acc:
            reach.setdefault(acc, [])
    accounts = []
    for acc, own in reach.items():
        own = sorted(set(own), key=lambda p: where[p])
        cards, openable, newest = _basis(index, tmap, own)
        accounts.append({"account": acc, "app": acc == app, "cli": acc == cli,
                         "folders": [where[p] for p in own], "cards": cards,
                         "with_transcript": openable, "last_activity": rfc3339(newest),
                         "_ms": newest})
    # app account first, then the newest activity, accounts with none last
    accounts.sort(key=lambda a: (not a["app"], a["_ms"] == 0, -a["_ms"], a["account"]))
    for a in accounts:
        del a["_ms"]

    named = set()
    for s in index.stores.values():
        for d in s.dirs.values():
            named.update(c["cid"].lower() for c in d["cards"].values() if c["cid"])
    with_card = len(named & set(tmap))
    problems = _identity_errors(identity, paths) + _bad_times(index)
    errors = (index.obs.errors + problems)[:MAX_ERRORS]
    complete = index.obs.complete() and not problems
    all_cards, all_open, _ms = _basis(index, tmap, picks)
    rep = {"command": "inventory", "exit": EXIT_OK if complete else EXIT_FAIL,
            "read_complete": complete, "errors": errors,
            "app_account": app, "cli_account": cli,
            "folders": folders, "accounts": accounts,
            "transcripts": {"total": len(tmap), "with_card": with_card,
                            "without_card": len(tmap) - with_card}}
    return rep, {"cards": all_cards, "with_transcript": all_open}


def _plural(n, one, many):
    return "%d %s" % (n, one if n == 1 else many)


def _day(stamp):
    return stamp[:16].replace("T", " ") + " UTC" if stamp else "none"


def verdict(rep, totals):
    """One plain sentence: how many accounts hold data, how many sessions open."""
    holders = [a for a in rep["accounts"] if a["folders"]]
    if not holders:
        return ("No account has session data on this Mac; %s on disk."
                % _plural(rep["transcripts"]["total"], "transcript", "transcripts"))
    return ("%s session data on this Mac; %d of %s can still be opened "
            "(transcript on disk)"
            % (_plural(len(holders), "account has", "accounts have"),
               totals["with_transcript"], _plural(totals["cards"], "session", "sessions")))


def render_text(rep, totals):
    """Human lines. Counts and ids only, never a session title."""
    lines = ["Claude session data on this Mac (read-only, nothing was changed)", ""]
    for a in rep["accounts"]:
        marks = [m for m, on in (("app account", a["app"]), ("CLI account", a["cli"])) if on]
        lines.append("Account %s%s" % (a["account"],
                                       " [%s]" % ", ".join(marks) if marks else ""))
        lines.append("  sessions: %d, can be opened (transcript on disk): %d, last activity: %s"
                     % (a["cards"], a["with_transcript"], _day(a["last_activity"])))
        if not a["folders"]:
            lines.append("  no session folder on this Mac")
        for i in a["folders"]:
            f = rep["folders"][i]
            others = len({x["account"] for x in f["aliases"]} - {a["account"]})
            shared = " (shared with %s)" % _plural(others, "other account", "other accounts") \
                if others else ""
            lines.append("  %s/%s%s: sessions %d (with transcript %d, without %d, "
                         "no session id %d), deletion marks %d, scheduled tasks %d"
                         % (f["store"], f["folder"], shared, f["cards"],
                            f["with_transcript"], f["without_transcript"],
                            f["no_session_id"], f["deleted"], f["scheduled_tasks"]))
        lines.append("")
    t = rep["transcripts"]
    lines.append("Transcripts on disk: %d (%d named by a session card, %d without a card)"
                 % (t["total"], t["with_card"], t["without_card"]))
    if not rep["read_complete"]:
        lines.append("WARNING: the read was incomplete, the numbers may be too low:")
        lines.extend("  " + e for e in rep["errors"])
    lines.append("")
    lines.append(verdict(rep, totals))
    return lines
