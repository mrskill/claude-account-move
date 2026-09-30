"""Comparisons against the "before" snapshot that are not about session cards:
account change, scheduled tasks, settings. Read-only.
"""
from . import panel_snapshot as snap
from .common import STORES


def compare_identity(before, identity, target, allow_same_account=False):
    """(ok, detail). The move only counts once the app left the source account."""
    src = (before.get("identity") or {}).get("app_account")
    cli = identity["cli"]["value"]
    app = identity["app"]["value"]
    if not cli or not app or not target:
        return False, "account not readable: cli=%s app=%s" % (
            identity["cli"]["status"], identity["app"]["status"])
    if cli != app or app != target:
        return False, "CLI account %s, app account %s, target %s differ" % (
            cli, app, target)
    if src and app == src and not allow_same_account:
        return False, "app is still on the source account %s" % src
    return True, "CLI and app both on target %s (source was %s)" % (target, src)


def target_dirs(index, store_name, account, active_dir=None):
    """Physical directories reachable through an alias of this account."""
    s = index.stores[store_name]
    out = set()
    for a in s.aliases:
        if a["acc"] == account:
            out.add(a["target"])
    if active_dir and store_name == STORES[0] and active_dir in s.dirs:
        out.add(active_dir)
    return sorted(out)


def compare_tasks(before, index, account, active_dir):
    """Scheduled tasks recorded before that are absent in the target directories."""
    missing, total = [], 0
    for name in STORES:
        want = ((before.get("stores") or {}).get(name) or {}).get("tasks") or {}
        if not want:
            continue
        s = index.stores[name]
        have = {}
        for real in target_dirs(index, name, account, active_dir):
            have.update(s.dirs[real]["task_map"])
        for tid, meta in want.items():
            total += 1
            cur = have.get(tid)
            if cur is None:
                missing.append("%s:%s (absent)" % (name, tid))
            elif meta.get("enabled") and not cur["enabled"]:
                missing.append("%s:%s (disabled)" % (name, tid))
            elif cur["approved"] < meta.get("approved", 0):
                missing.append("%s:%s (approvals %d of %d)" % (
                    name, tid, cur["approved"], meta.get("approved", 0)))
    return missing, total


def compare_settings(before, paths):
    """(changed keys or None, summary now). None = nothing to compare.

    Settings recorded before that are missing or unreadable now count as a
    change: permissions and hooks would be gone.
    """
    was = before.get("settings")
    now = snap.settings_summary(paths)
    if was is None:
        return None, now
    if now is None:
        return ["settings file missing or unreadable"], None
    return sorted(k for k in was if was.get(k) != now.get(k)), now
