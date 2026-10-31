# Changelog

## Unreleased

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
