# Changelog

## Unreleased

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
