# Changelog

## Unreleased

- `heal-active` and the `auto` repair also protect the other store's root
  folder: a heal is refused (exit 3, `refused`, nothing moved) when a folder it
  would move or replace (by realpath) is, lies inside or contains another
  store's root, whatever rows the plan holds (`--from-log` plans one store, and
  a link target inside the other store's root is caught too).
- `heal-active` refuses a shared physical root before moving anything: when a
  heal row would move or replace a folder that is, lies inside or contains
  another store's directory for the pair (compared by realpath, e.g. one store
  root is a symlink to the other), the whole heal is refused with exit 3 and
  verdict `refused`, dry run included. It shares `heal_active.repair_conflict`
  with the `auto` repair (which now also compares against stores that are
  healed in the same round).
- `auto` post-start verification, round 3. The pair each store loaded must be a
  real directory whether or not the store has cards: missing or not a directory
  fails `active_pair_real` and stops `auto` with exit 3 (not healed). The log
  mark after the repair is taken when the second ask is printed. The repair
  refuses, moving nothing, when the physical folder it would move or replace
  (by realpath) is, lies inside or contains another store's real loaded pair
  (`heal_active.repair_conflict`, `loaded_pair_problem`).
- `auto` post-start verification, round 2 (review findings). Per store: after
  the start, `finish` judges each store by the pair that store loaded
  (`store_pairs`, internal), and the repair heals only a store whose loaded pair
  is a symlink (internal `stores` of `heal-active`); a real loaded pair is never
  touched. Freshness is a byte position in `main.log` taken when `auto` asks
  (`heal_active.log_mark`, `fresh_root_loads(paths, acc, mark)`): a rotated or
  truncated log counts as all new, no clock decides. A move that `auto` stopped
  after its first finish (no start, repair failed) is resumable again:
  `find_unfinished_move` treats a newer failed `report-auto.json` as unfinished.
- `auto` verifies the move after the app has started under the new account.
  Defect 1: `--active-pair` (the OLD account's pair) was copied into every step;
  now only prepare, the write probe and stamp use it, and heal-active, finish
  and sync after the login get `active_pair=None` (`_ns(post_login=True)`).
  Defect 2: the app chooses its org folder only at start, so after finish `auto`
  asks to open Claude, waits (`--wait-start`, default 300 s, env
  `CLAUDE_ACCOUNT_MOVE_START_POLL_S`) for a fresh store-root load of the new
  account in each store (`heal_active.fresh_root_loads`, same definition as
  `_root_loads`), and the verdict is a second finish with the app running. If
  that fails only on `active_pair_real`, one repair round (quit, heal the loaded
  pair, reopen, third finish). No fresh load in time: exit 3, never success.
  The first finish no longer stops `auto` on names the app restores at start.
- `prepare` no longer fails on a stale ENOTDIR line. A recent ENOTDIR line for a
  symlinked pair is cleared only when the last later store-root load in the same
  store (`Loaded N sessions from <store>/<account>/<org>`, no
  `enotdir`/`fail`/`error`, nothing deeper than the org) names another pair: the
  app logs no successful saves, and the root it loads is the root it writes
  into. With no later load, or when the last one names the same pair again, the
  hit counts. A card read or save of another pair, a load in the other store,
  `account changed`, and a failed or deeper load do not clear it. Live case
  02.11.2026: ENOTDIR lines of 01.11 for pair `d9bbd39a`, a symlink to the
  current pair `252e7efa`. `heal_active.recent_enotdir(paths, now, max_age_s)`
  reads the log once and no longer calls `log_pair`.
- `heal-active` no longer refuses every symlink inside the moved folder. A
  relative link whose target stays inside the folder points at the same file
  after the move and is now allowed (live case 31.10.2026:
  `node_modules/.bin/nanoid -> ../nanoid/bin/nanoid.js` inside a local-agent
  session's `outputs` blocked the move step), as is a dangling relative link
  inside the folder. The target is walked one component at a time from the
  link's own folder: it may never go above the moved folder at any step, and no
  folder on the way may be another symlink (only the last component may be a
  symlink or missing). Still refused: every absolute link, whatever it points
  at, and a relative link that points at its own folder or a parent (a loop,
  found by device and inode, so case-folding volumes cannot hide it). The check
  runs again for each store just before the swap; a failure there rolls back
  like any other changed-disk failure.
- Added `heal-active`: when the active pair `<store>/<account>/<org>` of
  `claude-code-sessions` or `local-agent-mode-sessions` is a symlink, the app
  reads through it but every card write fails with ENOTDIR and new cards are
  lost. The command (dry run by default, `--apply` writes, `--json`,
  `--active-pair`) removes the link, moves the real folder to the active path
  and leaves a symlink at its old path. It refuses while the app runs, refuses
  a dangling link or a target outside the store, changes all stores or none,
  and reports "nothing to do" when the pair is already a real directory.
- `finish` now exits `3` while the active pair of either store is a symlink,
  naming the store and path and telling to quit the app and run
  `heal-active --apply`.
- `auto` runs `heal-active` after the login and before `finish`.
- `prepare` fails readiness (exit `3`) on an ENOTDIR line in `main.log` from the
  last 2 days that names a pair which is still a symlink.
- `heal-active` takes the active pair from the app log first (newest save, read
  or ENOTDIR line after the last "account changed" line), else the app account
  with an organization that exists in the store; it never combines the app
  account with the CLI organization unchecked, and refuses naming the sources
  when they disagree.
- `heal-active --from-log` heals every pair named by an ENOTDIR line of the last
  10 minutes that is still a symlink, and may run while the app runs.
- Added `deploy/com.neurovision.heal-active.plist` (launchd, every 60 seconds).
- `heal-active` hardening: the folder is swapped into place with one atomic
  exchange (renamex_np RENAME_SWAP, renameat2 RENAME_EXCHANGE on Linux), so the
  active path is never missing; it refuses when the exchange is unavailable, when
  a relative symlink inside the folder would change its target, and when several
  organizations exist with no confirming log line (the CLI organization counts
  only when the CLI is signed in to the app account). `--from-log` ignores errors
  older than the last account change or overtaken by a write to another pair, and
  keeps one pair per physical folder. The lock is taken before the plan, the disk
  is re-checked (type, inode, link) before each step, and a step journal in the
  state directory lets the next run recover an interrupted heal; a failed
  rollback is reported with its paths and a non-zero exit.
- `heal-active --from-log` heals at most one pair per tick: the one named by the
  newest log line of any kind (after the last account change), and only when an
  ENOTDIR line of the last 10 minutes names it. Older pairs are never touched.
  Any symlink inside the moved folder is refused (except a plain sibling name).
  The journal step after the swap is inside the protected block and a rollback
  is verified before it is reported. `finish` checks the delivery pair and the
  heal-active pair, and fails when no pair can be determined.
