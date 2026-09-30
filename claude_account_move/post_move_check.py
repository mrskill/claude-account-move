"""Verification AFTER the move: did every session card, transcript, deletion
mark, scheduled task and setting recorded before arrive? Read-only.

Every number is printed with its denominator. Loss is judged by names and
session ids recorded in the snapshot taken before the move, never by counts
alone.

Verdict per store: 3 on a real loss, 5 when everything exists somewhere but
not yet in the target pair and a sync job is alive, 0 otherwise.
"""
import os

from . import panel_snapshot as snap
from . import post_move_verify as verify
from . import pre_move_check as pre
from . import store as st
from .common import EXIT_FAIL, EXIT_OK, EXIT_WAIT, STORES


def classify(cards, tmap):
    """Split cards into buckets that mean DIFFERENT things after a move.

    cards: {card file: cliSessionId or ""} one entry per name (best copy).
    Returns (empty_live, dead, no_transcript), each sorted:
      empty_live    empty cliSessionId AND a transcript named by the card's own
                    id exists: the session is on disk but cannot be opened
      dead          empty id and no transcript: an empty panel record
      no_transcript id set, but no such transcript on disk
    """
    empty_live, dead, no_transcript = [], [], []
    for f, cid in cards.items():
        if not cid:
            sid = f[len(st.CARD_PREFIX):-len(".json")].lower()
            (empty_live if sid in tmap else dead).append(f)
        elif cid.lower() not in tmap:
            no_transcript.append((f, cid))
    return sorted(empty_live), sorted(dead), sorted(no_transcript)


def effective_lagging(lagging, index, store_name, source, target, names_by_dir,
                      union):
    """Drop lagging directories that are NOT a failure.

    An EMPTY directory that belongs only to the source account does not fail
    the move while some directory holds every name of the union: the sessions
    are all there, the old account's folder simply stayed empty.
    """
    if not source or not union:
        return dict(lagging)
    s = index.stores[store_name]
    full = any(union <= names for names in names_by_dir.values())
    out = {}
    for real, miss in lagging.items():
        accs = {a["acc"] for a in s.aliases if a["target"] == real}
        empty = not names_by_dir.get(real)
        if full and empty and accs and accs <= {source} and target not in accs:
            continue
        out[real] = miss
    return out


def store_verdict(lagging, empty_live, dead, union_len, expect, *, missing,
                  lost_ids):
    """3 on a real loss, 0 otherwise.

    missing: expected directories gone from disk; None = not measured and then
    never blocks (a lost measurement must not become a green verdict).
    lost_ids: ids that had a transcript before the move and have none now.
    `dead` is accepted but deliberately never blocks; both arguments after the
    `*` are required so an old call cannot silently skip a check.
    """
    if lagging or empty_live or missing or lost_ids:
        return EXIT_FAIL
    if expect is not None and union_len < expect:
        return EXIT_FAIL
    return EXIT_OK


def _check(out, cid, ok, detail):
    out.append({"id": cid, "ok": bool(ok), "detail": detail})


def evaluate(paths, before, index, tmap, identity, target, source,
             sync_alive=False, active_override=None, allow_same_account=False):
    """Compare the present state with the snapshot. Returns (report, exit code)."""
    checks, notes = [], []
    code = EXIT_OK
    lost = waiting = False

    ok, detail = verify.compare_identity(before, identity, target,
                                         allow_same_account)
    _check(checks, "accounts", ok, detail)
    lost |= not ok

    ev_before = before.get("transcripts") or {}
    ev_now = st.transcript_evidence(tmap, sorted(ev_before), index.obs)
    o = index.obs
    _check(checks, "observation_complete", o.complete(),
           "%d unreadable cards, %d errors" % (o.unreadable_cards, len(o.errors)))
    lost |= not o.complete()

    pair = st.live_pair(paths, account=target, override=active_override)
    active_dir = st.pair_dir(paths, pair[0], pair[1]) if pair else None
    cc = index.stores[STORES[0]]
    if active_dir and active_dir not in cc.dirs:
        active_dir = None
    active_present = active_dir is not None
    if not active_present:
        notes.append("the app log names no pair for the target account yet")
    report = {"stores": {}, "active_pair": {"present": active_present,
                                            "path": active_dir}}

    for name in STORES:
        s = index.stores[name]
        exp = (before.get("stores") or {}).get(name) or {}
        names_before = set((exp.get("cards") or {}))
        union = s.union_names()
        base_real = os.path.realpath(s.base)
        dirs_missing = [r for r in exp.get("physical_dirs") or []
                        if not os.path.isdir(os.path.join(base_real, r))]
        have_aliases = {"%s/%s" % (a["acc"], a["org"]) for a in s.aliases}
        aliases_lost = [a for a in exp.get("aliases") or [] if a not in have_aliases]
        names_by_dir = {r: set(d["cards"]) for r, d in s.dirs.items()}
        lagging = {r: sorted(union - n) for r, n in names_by_dir.items()
                   if union - n}
        lag_eff = effective_lagging(lagging, index, name, source, target,
                                    names_by_dir, union)

        best = {}
        for n in union:
            b = s.best_card(n)
            best[n] = b["cid"]
        empty_live, dead, no_tr = classify(best, tmap)
        pre_ids = {str(x).lower() for x in exp.get("cards_with_transcript") or []}
        lost_ids = sorted(i for i in pre_ids if i not in tmap)
        tomb_missing = sorted(set(exp.get("tombstone_ids") or [])
                              - s.tombstone_ids())
        missing_anywhere = sorted(names_before - union)

        if name == STORES[0]:
            t_names = set(s.dirs[active_dir]["cards"]) if active_present else None
            t_dirs = [active_dir] if active_present else []
        else:
            t_dirs = verify.target_dirs(index, name, target)
            t_names = set().union(*[names_by_dir[r] for r in t_dirs]) if t_dirs else None
        missing_in_target = None if t_names is None else sorted(names_before - t_names)
        # A lagging TARGET directory with a live sync job is delivery in
        # progress (wait), not loss; everything else lagging is a failure.
        t_set = set(t_dirs)
        lag_wait = ({r: m for r, m in lag_eff.items() if r in t_set}
                    if sync_alive and not missing_anywhere else {})
        lag_loss = {r: m for r, m in lag_eff.items() if r not in lag_wait}
        empty_cid_target = 0
        for r in t_dirs:
            for n, c in s.dirs[r]["cards"].items():
                if not c["cid"]:
                    sid = n[len(st.CARD_PREFIX):-len(".json")].lower()
                    other = [x["cid"].lower() for _, x in s.copies_of(n) if x["cid"]]
                    if sid in tmap or any(i in tmap for i in other):
                        empty_cid_target += 1

        wrong_cid, regressed = [], []
        for n, rec in (exp.get("cards") or {}).items():
            # Every target copy must keep the recorded binding, not only the
            # best one: a second target folder with a wrong id still opens the
            # wrong session.
            copies = [(r, s.dirs[r]["cards"][n]) for r in t_dirs
                      if n in s.dirs[r]["cards"]]
            if not copies:
                continue
            want = (rec.get("cid") or "").lower()
            if want and any(c["cid"].lower() != want for _, c in copies):
                wrong_cid.append(n)
                continue
            tc = max((c for _, c in copies),
                     key=lambda c: (1 if c["cid"] else 0, c["act"], c["turns"]))
            if tc["act"] < rec.get("act", 0) or tc["turns"] < rec.get("turns", 0):
                regressed.append(n)

        st_report = {
            "dirs": len(s.dirs), "physical_dirs": sorted(s.rel(r) for r in s.dirs),
            "aliases": len(s.aliases), "union_names": len(union),
            "lagging_dirs": len(lag_eff), "cards": len(best),
            "dirs_expected": len(exp.get("physical_dirs") or []),
            "dirs_missing": len(dirs_missing), "aliases_lost": len(aliases_lost),
            "names_before": len(names_before),
            "names_in_target": None if t_names is None else len(t_names),
            "names_missing_anywhere": len(missing_anywhere),
            "names_missing_in_target": None if missing_in_target is None
            else len(missing_in_target),
            "missing_list": (missing_in_target or [])[:50],
            "transcripts_gone_since_snapshot": len(lost_ids),
            "empty_cliSessionId_with_transcript": len(empty_live),
            "target_copy_empty_cid": empty_cid_target,
            "target_copy_wrong_cid": len(wrong_cid),
            "target_copy_regressed": len(regressed),
            "dead_cards": len(dead), "id_without_transcript": len(no_tr),
            "tombstone_missing_in_union": len(tomb_missing),
        }
        report["stores"][name] = st_report

        v = store_verdict(lag_loss, empty_live, dead, len(union), None,
                          missing=dirs_missing, lost_ids=lost_ids)
        _check(checks, "%s:loss" % name, v == EXIT_OK and not missing_anywhere
               and not aliases_lost and not tomb_missing and not empty_cid_target
               and not wrong_cid,
               "%d of %d names present anywhere; dirs missing %d of %d; aliases "
               "lost %d; transcripts gone %d of %d; empty id with transcript %d; "
               "target copies with empty id %d; target copies bound to another "
               "session %d; deletion marks missing %d"
               % (len(names_before) - len(missing_anywhere), len(names_before),
                  len(dirs_missing), len(exp.get("physical_dirs") or []),
                  len(aliases_lost), len(lost_ids), len(pre_ids), len(empty_live),
                  empty_cid_target, len(wrong_cid), len(tomb_missing)))
        if v != EXIT_OK or missing_anywhere or aliases_lost or tomb_missing \
                or empty_cid_target or wrong_cid:
            lost = True
        if regressed:
            _check(checks, "%s:target_fresh" % name, False,
                   "%d target copies are older than recorded (stale copy): %s"
                   % (len(regressed), ", ".join(sorted(regressed)[:3])))
            if sync_alive and not missing_anywhere:
                waiting = True
            else:
                lost = True
        if missing_in_target:
            _check(checks, "%s:target_names" % name, False,
                   "%d of %d names missing in the target pair (present elsewhere: %s)"
                   % (len(missing_in_target), len(names_before),
                      "yes" if not missing_anywhere else "partly"))
            if missing_anywhere:
                lost = True
            elif sync_alive:
                waiting = True
            else:
                lost = True
        elif t_names is None and names_before:
            _check(checks, "%s:target_names" % name, False,
                   "no target directory for this store yet: %d recorded names "
                   "cannot be delivered (run `sync --apply` or use the app)"
                   % len(names_before))
            if sync_alive:
                waiting = True
            else:
                lost = True
        else:
            _check(checks, "%s:target_names" % name, True,
                   "nothing recorded for this store" if t_names is None else
                   "target holds all %d recorded names" % len(names_before))
        groups = pre.duplicate_groups(index)[0]
        if name == STORES[0] or groups:
            gs = [g for g in groups if g["store"] == name]
            _check(checks, "%s:duplicates" % name, not gs,
                   "sessions with several cards: %d" % len(gs))
            lost |= bool(gs)
        if lag_loss:
            _check(checks, "%s:lagging" % name, False,
                   "%d directories lack names that exist elsewhere" % len(lag_loss))
            lost = True
        if lag_wait:
            _check(checks, "%s:lagging_target" % name, False,
                   "target directory still receiving names (sync job alive)")
            waiting = True

    tmissing, ttotal = verify.compare_tasks(before, index, target, active_dir)
    _check(checks, "tasks", not tmissing,
           "%d of %d recorded scheduled tasks missing in the target%s"
           % (len(tmissing), ttotal, ": " + ", ".join(tmissing[:5]) if tmissing else ""))
    lost |= bool(tmissing)

    changed, now_settings = verify.compare_settings(before, paths)
    if changed is None:
        _check(checks, "settings", True, "NOT MEASURED (no settings recorded before)")
    else:
        _check(checks, "settings", not changed,
               "settings changed: %s" % (", ".join(changed) or "none"))
        lost |= bool(changed)
    cleanup = (now_settings or {}).get("cleanupPeriodDays")
    if cleanup is not None and isinstance(cleanup, (int, float)) and cleanup < 3650:
        notes.append("cleanupPeriodDays is %s: Claude Code removes older "
                     "sessions after that many days" % cleanup)

    bad_tr = []
    unchecked = 0
    for sid, rec in ev_before.items():
        cur = ev_now.get(sid) or {"size": 0, "last_ms": None}
        if not tmap.get(sid):
            bad_tr.append("%s (missing)" % sid)
            continue
        if cur["size"] < rec.get("size", 0):
            bad_tr.append("%s (truncated %d < %d bytes)" % (sid, cur["size"], rec["size"]))
            continue
        if rec.get("last_ms") is not None and cur["last_ms"] is None:
            bad_tr.append("%s (no readable message any more)" % sid)
            continue
        if rec.get("last_ms") is not None and cur["last_ms"] < rec["last_ms"]:
            bad_tr.append("%s (last message became older)" % sid)
            continue
        if rec.get("prefix_sha256"):
            ok_prefix = any(st.prefix_digest(f, rec["prefix_len"]) == rec["prefix_sha256"]
                            for f in tmap[sid])
            if not ok_prefix:
                bad_tr.append("%s (content of the first %d bytes changed)"
                              % (sid, rec["prefix_len"]))
        elif rec.get("size"):
            unchecked += 1                  # snapshot of an older version: no digest
    _check(checks, "transcripts", not bad_tr,
           "%d of %d recorded transcripts intact (size, last message, content of "
           "the recorded prefix)%s%s"
           % (len(ev_before) - len(bad_tr), len(ev_before),
              ": " + ", ".join(bad_tr[:3]) if bad_tr else "",
              "; %d without a content digest (older snapshot)" % unchecked
              if unchecked else ""))
    lost |= bool(bad_tr)
    report["transcripts_bad"] = len(bad_tr)

    newest_before = before.get("newest_message_ms")
    newest_now = snap.newest_message(index, tmap)
    if newest_before is None:
        _check(checks, "newest_message", True, "NOT MEASURED before the move")
    elif newest_now is None:
        _check(checks, "newest_message", False,
               "newest message was %d before and cannot be read now" % newest_before)
        lost = True
    else:
        _check(checks, "newest_message", newest_now >= newest_before,
               "newest message before %d, now %d (ms)" % (newest_before, newest_now))
        lost |= newest_now < newest_before

    report["checks"] = checks
    report["notes"] = notes
    report["observation"] = o.as_dict()
    report["write_verified"] = False
    if lost:
        code = EXIT_FAIL
    elif waiting:
        code = EXIT_WAIT
    return report, code
