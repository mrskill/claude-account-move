"""Readiness checks to run BEFORE signing out of the old Claude account.

Everything here is read-only. Each check returns (ok, detail); `detail` always
names the number it judged and its denominator, because "0 of 0" and "0 of
1000" are different results.

Checks:
  observation     the storage was read completely (no unreadable card or folder)
  sync_agent      optional: a heartbeat file of your own sync job is fresh
  static_access   the directory the app writes into exists and is writable by
                  permission bits (a write is NOT attempted: write_verified=false)
  cards_fresh     the app keeps cards up to date (median lag of live sessions)
  zombies         no session is deleted in the panel and still has a live card
  stale_copies    the active pair holds the newest copy of every card
  empty_cid_live  no card lost its cliSessionId while its transcript exists
  duplicates      no session has two different card files
"""
import os
import time

from . import store as st
from .common import STORES

LAG_MAX_S = 1800
LIVE_WINDOW_S = 3600
FRESH_SAMPLE = 25
SYNC_MAX_AGE_MIN = 30.0


def lag_verdict(rows, now_ms, max_lag_s=LAG_MAX_S, window_s=LIVE_WINDOW_S):
    """(ok, live sessions, median lag seconds). ok=None means NOT MEASURED.

    The median over live sessions is judged, not one card: a session with a
    turn in progress legitimately lags, because the app writes the card when a
    turn completes.
    """
    lags = sorted(r["lag_s"] for r in rows
                  if r.get("real_ms") and r.get("lag_s") is not None
                  and (now_ms - r["real_ms"]) / 1000.0 <= window_s)
    if not lags:
        return None, 0, None
    n = len(lags)
    med = float(lags[n // 2]) if n % 2 else (lags[n // 2 - 1] + lags[n // 2]) / 2.0
    return med <= max_lag_s, n, med


def freshness_rows(index, tmap, active_dir, limit=FRESH_SAMPLE):
    """Newest cards of the active directory: card time against transcript time."""
    info = index.stores[STORES[0]].dirs.get(active_dir)
    if not info:
        return []
    names = sorted(info["cards"], key=lambda n: info["cards"][n]["act"],
                   reverse=True)[:limit]
    rows = []
    for n in names:
        c = info["cards"][n]
        real = st.session_last_ms(tmap, c["cid"].lower()) if c["cid"] else None
        rows.append({"card": n, "card_ms": c["act"], "real_ms": real,
                     "lag_s": int((real - c["act"]) / 1000)
                     if real and c["act"] else None})
    return rows


def check_observation(index):
    o = index.obs
    if o.complete():
        return True, "storage read completely (%d problems)" % 0
    return False, ("storage read incompletely: %d unreadable cards, %d errors; "
                   "counts below are not trustworthy" % (o.unreadable_cards,
                                                          len(o.errors)))


def check_sync_agent(heartbeat, max_age_min=SYNC_MAX_AGE_MIN, now=None):
    if not heartbeat:
        return True, "not configured, skipped (no sync job declared)"
    now = time.time() if now is None else now
    try:
        age = (now - os.stat(heartbeat).st_mtime) / 60.0
    except OSError:
        return False, "heartbeat file missing: %s" % heartbeat
    if age < -1:
        return False, "heartbeat is from the future by %.1f min" % -age
    ok = age <= max_age_min
    return ok, "heartbeat %.1f min old (limit %.1f min)" % (age, max_age_min)


def check_static_access(index, active_dir):
    if not active_dir:
        return False, ("active pair NOT MEASURED: the app log names no pair "
                       "(use --active-pair ACCOUNT/ORG)")
    s = index.stores[STORES[0]]
    if active_dir not in s.dirs:
        return False, "active pair is not a directory inside the store"
    if not os.access(active_dir, os.W_OK | os.X_OK):
        return False, "active pair is not writable by permission bits"
    return True, ("active pair exists, inside the store, writable by "
                  "permission bits (write not attempted)")


def check_cards_fresh(index, tmap, active_dir, now_ms=None):
    rows = freshness_rows(index, tmap, active_dir)
    ok, live, med = lag_verdict(rows, (time.time() * 1000) if now_ms is None
                                else now_ms)
    if ok is None:
        return True, "NOT MEASURED: no live session (no transcript written in the last hour)"
    return ok, ("live sessions: %d, median card lag %.0f s (limit %d s)"
                % (live, med, LAG_MAX_S))


def check_zombies(index):
    total = sessions = 0
    for name in STORES:
        s = index.stores[name]
        dead = s.tombstone_ids()
        seen = set()
        for d in s.dirs.values():
            for n in d["cards"]:
                sid = n[len(st.CARD_PREFIX):-len(".json")]
                if sid in dead:
                    total += 1
                    seen.add(sid)
        sessions += len(seen)
    return total == 0, ("deleted-but-alive cards: %d occurrences, %d sessions"
                        % (total, sessions))


def check_stale_and_empty(index, tmap, active_dir):
    """(stale_ok/detail, empty_ok/detail) for the active pair of the first store."""
    s = index.stores[STORES[0]]
    info = s.dirs.get(active_dir)
    if not info:
        msg = "active pair NOT MEASURED"
        return (False, msg), (False, msg)
    older = empty_live = dead = 0
    for name, card in sorted(info["cards"].items()):
        if not card["cid"]:
            sid = name[len(st.CARD_PREFIX):-len(".json")].lower()
            if sid in tmap:
                empty_live += 1
            else:
                dead += 1
        for real, other in s.dirs.items():
            if real != active_dir and name in other["cards"] \
                    and other["cards"][name]["act"] > card["act"]:
                older += 1
                break
    total = len(info["cards"])
    stale = (older == 0, "active pair older than a neighbour: %d of %d cards"
             % (older, total))
    msg = ("empty cliSessionId with a live transcript: %d of %d cards"
           % (empty_live, total))
    if dead:
        msg += "; dead cards (no id, no transcript): %d (informational)" % dead
    return stale, (empty_live == 0, msg)


def duplicate_groups(index):
    """Sessions that own more than one distinct card file, per store."""
    groups, title_groups = [], 0
    for name in STORES:
        s = index.stores[name]
        by_cid, by_title = {}, {}
        for card_name in sorted(s.union_names()):
            best = s.best_card(card_name)
            if best["cid"]:
                by_cid.setdefault(best["cid"], []).append(card_name)
                if best["title"]:
                    by_title.setdefault(best["title"], set()).add(best["cid"])
        title_groups += sum(1 for v in by_title.values() if len(v) > 1)
        for cid, names in sorted(by_cid.items()):
            if len(names) > 1:
                groups.append({"store": name, "cid": cid, "cards": names})
    return groups, title_groups


def check_duplicates(index):
    groups, titles = duplicate_groups(index)
    msg = "one session with several cards: %d groups" % len(groups)
    msg += "; same title with different sessions: %d groups (count only)" % titles
    if groups:
        msg += "; " + "; ".join("%s: %s: %s" % (g["store"], g["cid"],
                                               ", ".join(g["cards"]))
                                for g in groups[:10])
    return not groups, msg


def check_identity(identity):
    """A ready baseline needs a readable source account (app and CLI)."""
    bad = [side for side in ("app", "cli") if identity[side]["status"] != "ok"]
    if bad:
        return False, ("source account not readable: %s (status %s); sign in "
                       "first, the move needs to know where it starts"
                       % (", ".join(bad), ", ".join(identity[b]["status"]
                                                    for b in bad)))
    return True, "source account known: app %s, CLI %s" % (
        identity["app"]["value"], identity["cli"]["value"])


def check_settings(paths):
    from . import panel_snapshot as snap
    state = snap.settings_state(paths)
    if state == "unreadable":
        return False, ("settings file exists but cannot be read as JSON: "
                       "permissions and hooks cannot be compared after the move")
    return True, ("no settings file" if state == "missing"
                  else "settings readable (permissions and hooks digested)")


def run_checks(paths, index, tmap, heartbeat=None, sync_max_age_min=SYNC_MAX_AGE_MIN,
               active_override=None, now_ms=None, identity=None):
    """List of {"id","name","ok","detail"} plus the active directory used."""
    expected = identity["app"]["value"] if identity else None
    pair = st.live_pair(paths, account=expected if active_override else None,
                        override=active_override)
    active_dir = st.pair_dir(paths, pair[0], pair[1]) if pair else None
    stale, empty = check_stale_and_empty(index, tmap, active_dir)
    plan = [
        ("observation", "storage read completely", check_observation(index)),
        ("source_identity", "source account readable",
         check_identity(identity) if identity else (True, "not checked")),
        ("settings_readable", "settings readable", check_settings(paths)),
        ("sync_agent", "sync job heartbeat",
         check_sync_agent(heartbeat, sync_max_age_min)),
        ("static_accessibility", "active pair accessible",
         check_static_access(index, active_dir)),
        ("cards_fresh", "app keeps cards fresh",
         check_cards_fresh(index, tmap, active_dir, now_ms)),
        ("zombies", "no deleted-but-alive sessions", check_zombies(index)),
        ("stale_copies", "active pair holds newest copies", stale),
        ("empty_cid_live", "no lost cliSessionId", empty),
        ("duplicates", "one card per session", check_duplicates(index)),
    ]
    out = []
    for cid, name, (ok, detail) in plan:
        out.append({"id": cid, "name": name, "ok": bool(ok), "detail": detail})
    return out, active_dir
