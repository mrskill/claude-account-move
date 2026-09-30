# claude-account-move

Version 0.1.2. Move Claude Code (the desktop app for Mac and the CLI) from one
Claude account to another without losing your sessions, and prove afterwards
that nothing was lost.

This is an independent tool. It is not an official Anthropic product and is not
endorsed by Anthropic. The storage layout it reads is an implementation detail
of the Claude desktop app and can change between app versions.

Author: **Mr. Skill** ([@mrskill](https://github.com/mrskill))

Russian version: [README.ru.md](README.ru.md)

## What it does

| Command | When | What happens |
|---|---|---|
| `prepare` | before you sign out | Runs readiness checks, records a "before" snapshot of this move (metadata only), prints a verdict. One pass. |
| `finish` | after you signed in to the app and ran `claude /login` | Waits until the CLI account equals the app account, then compares the storage with the snapshot of THIS move and prints a verdict. |
| `sync` | optional repair | Copies session cards that are missing in the new account's folder. Dry run by default (it prints the files). `sync --apply` adds files and `sync --undo` removes exactly the files a journal proves were added. |
| `stamp` | optional, see below | Puts the date and time of the last real message into the title of every session. Dry run by default (it lists "was -> will be"). `stamp --apply` writes, `stamp --undo` restores the cards byte for byte. |

`sync --apply`, `sync --undo`, `stamp --apply` and `stamp --undo` are the only
operations that write into the Claude storage.

The checks, all judged by names and ids recorded before the move, not by counts
alone (every number is printed with its denominator):

- storage read completely (no unreadable card, transcript or task file);
- the source account is readable (a move needs a known starting point) and the
  settings file, if there is one, is readable;
- the directory the app writes into exists and is writable by permission bits;
- the app keeps cards fresh (median lag of live sessions);
- no session is deleted in the panel and still has a live card;
- the active folder holds the newest copy of every card;
- no card lost its `cliSessionId` while its transcript exists (the session is on
  disk but cannot be opened);
- no session owns two different card files;
- after the move: every card name, every deletion mark and every scheduled task
  recorded before still exists; EVERY copy of a card in the target folders (not
  only the best one) is bound to the session id recorded before, and the best
  copy is not older; every recorded transcript still has its size, its last
  message and a byte-identical prefix (transcripts only grow, so the first
  bytes recorded, up to 64 MiB per file, must not change); settings are
  unchanged (every top-level key of the settings file is compared by a digest
  of its full content, so replacing one entry with another is seen); both
  accounts match the target.

A value that could be measured before the move and cannot be measured after it
is a failure, never a pass.

## What it does not do

- It never signs in for you and never asks you for a password or token. You sign
  in to the app and run `claude /login` yourself. It does not use or store
  credentials: it parses `~/.claude.json` and the app's `config.json` as whole
  JSON documents (so anything in them, including a token if present, passes
  through memory), keeps only the account and organization ids, and never writes,
  logs or transmits anything else from them.
- `prepare`, `finish` and the checks change nothing in the Claude storage or in
  your settings. Everything they write goes to the state directory (see below).
  They do not even write a probe file into the storage. The only commands that
  touch the storage are `sync --apply` (adds card files), `sync --undo` (deletes
  the files that a journal proves `--apply` added), `stamp --apply` (rewrites the
  `title` field of cards) and `stamp --undo` (puts the old bytes back).
- It does not migrate scheduled tasks: `finish` reports recorded tasks that are
  missing in the new account, but moving them is up to you.
- It does not decide for you between two different session ids behind one card
  name: `sync` lists such conflicts, copies nothing for them and exits `3`.
- It does not trust an explicit `--active-pair` blindly: the account in it must be
  the account the app is signed in to, otherwise the command stops with exit `2`.

## Requirements

- macOS (paths are the ones of the Claude desktop app on macOS);
- Python 3.9 or newer, standard library only;
- Claude desktop app and Claude Code CLI.

## Install

```sh
git clone https://github.com/mrskill/claude-account-move.git
cd claude-account-move
./claude-account-move --version      # claude-account-move 0.1.2
```

Or download a release archive, unpack it and run `./claude-account-move` from
the unpacked directory. To call it from anywhere, link it into a directory on
your `PATH`: `ln -s "$PWD/claude-account-move" /usr/local/bin/claude-account-move`.
The entry script resolves the link, so the package next to it is found.

## A move, step by step

The commands below use `./claude-account-move` from the cloned or unpacked
directory. If you linked it into your `PATH`, drop the `./`.

1. In a terminal, before leaving the old account:

   ```sh
   ./claude-account-move prepare
   ```

   Exit code `0` means ready. Anything else: read the `FAIL` lines, fix the
   cause, run `prepare` again. A snapshot taken when the verdict was not ready is
   kept for diagnosis but `finish` never picks it by itself.
2. Sign out of the Claude desktop app and sign in with the new account. You do
   this yourself.
3. In a terminal, sign the CLI in with the same new account: `claude /login`.
4. Run:

   ```sh
   ./claude-account-move finish
   ```

   It waits up to 15 minutes (`--wait-login SECONDS`) for the CLI account to
   equal the app account, then verifies. Exit code `0` means everything arrived.
5. If `finish` says that names are missing in the new account's folder (exit
   `3` with `target_names` failing, nothing missing anywhere), copy them and
   verify again:

   ```sh
   ./claude-account-move sync            # dry run: prints the files it would copy
   ./claude-account-move sync --apply    # copies; only adds files
   ./claude-account-move finish
   ```

   Then restart the Claude app once: it reads its session list only at start.
   Restarting ends running sessions, so save your work first.
6. Optional: put the last-message time into the session titles with
   `./claude-account-move stamp --apply` (see "stamp" below; it is best done
   before step 2, and after `finish` it shows after the next app start).

Useful options: `--json` (every command and every outcome, failures, usage
errors and an interrupt included, prints exactly one JSON report on stdout;
human text goes to stderr),
`--home DIR` (inspect another home directory), `--state-dir DIR`,
`--wait-sync SECONDS`, `--allow-same-account` (re-login to the same account),
`--move-id ID`, `--max-age-hours N` (default 72), `--active-pair ACCOUNT/ORG`
(when the app log cannot name the pair the app writes into; the account must be
the one the app is signed in to), `prepare --full-copy` (also copy every card
file into the snapshot; large).

### Optional: stamp the last-message time into session titles

```sh
./claude-account-move stamp           # dry run: lists "was  ->  will be", writes nothing
./claude-account-move stamp --apply   # writes, with a journal
./claude-account-move stamp --undo ~/.claude-account-move/stamp-<time>-<id>.jsonl
```

Every session title gets one suffix `<title> · DD.MM HH:MM` (middle dot), the
local time of the last real user or assistant message, read from the
transcripts. A second run does not double the suffix: an older stamp is cut off
first. All copies of one session get the same stamp (the newest of their
transcripts); cards without a transcript, with an empty title, in a file layout
that cannot be reproduced byte for byte, or already stamped correctly are left
alone. Only the `title` field changes; the file layout and permissions stay.

`--apply` writes each card through a temporary file and an atomic replace, and
checks twice (before writing and right before the replace) that the card still
holds exactly the planned bytes; a card changed in between (for example by the
app) is left alone, reported, and the exit code is `3`. `--undo` restores a card
byte for byte only when its inode and content still equal the journal's, under
the same ownership rules and home binding as `sync --undo`; a card changed since
is kept and reported.

When to run it: the panel re-reads cards at start and at a change of account, so
stamping BEFORE signing out (step 2) makes the stamps show right after the
switch. Stamping after `finish` (optional step 6) works too, but shows only
after the app is restarted (which ends running sessions). The stamp uses the
time zone of this machine. Stamp while the app is idle: the tiny window between
the last check and the replace cannot be closed from outside.

### Optional: a sync job of your own

If you run a background job that keeps the account folders in step, make it
touch a file on every run and pass it with `--heartbeat FILE` (or the
`CLAUDE_ACCOUNT_MOVE_SYNC_HEARTBEAT` variable). `prepare` then requires the file
to be younger than `--sync-max-age-min` (default 30), and `finish` answers `5`
("wait") instead of `3` while the job is alive and names are still arriving. With
no heartbeat configured that check is skipped.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | `prepare`: ready to move. `finish`: everything arrived and all checks passed. `sync`: done or dry run with nothing wrong. |
| 1 | Internal error of the tool, or a required state write failed (snapshot, pointer, journal, a symlink inside the state directory). Says nothing about loss or readiness. A failure to save the optional JSON report file is only warned about on stderr. |
| 2 | Input error: no command, state directory overlapping the Claude storage, your settings or logs, malformed or foreign `--active-pair`, a journal that cannot be used (unreadable, no begin record, belongs to another home directory). |
| 3 | Failure on substance. `prepare`: a readiness check is red. `finish`: something is missing or different (card, target binding, transcript, deletion mark, task, settings, account, folder). `sync`: input could not be read completely, no target folder could be determined, a copy failed, a conflict was found, or (`--undo`) a journal has a torn or damaged part or names files that are not proven to be ours (those are kept). `stamp`: input could not be read completely, a card changed between plan and write (left alone), a write failed, or (`--undo`) a card changed since the stamp (kept). |
| 4 | Storage not found (no Claude application support folder, or no account/organization folder in it). |
| 5 | `finish`: nothing is lost but not everything is delivered yet and your sync job is alive. Wait and run again. |
| 6 | `finish`: the wait for login ran out. `login.reason` in the JSON report says why: `cli_lags`, `app_not_switched`, `identity_invalid`. |
| 7 | `finish`: no usable "before" snapshot of this move (missing, damaged, not ready, without a source account, older than `--max-age-hours`, other home directory, unknown `--move-id`). |

Input errors come first: usage errors, an unsafe state directory and a malformed
`--active-pair` (not two UUIDs) are reported before the storage is looked at. An
`--active-pair` of the wrong account can only be recognised once the app account
has been read, so it is reported (also as 2) after the storage check. When
several causes apply, the code is chosen per command in this order:
`prepare`: 2, 4, 1, 3, 0. `finish`: 2, 4, 7, 6, 3, 5, 0 (a storage with no
account folder gives 4 even when no snapshot exists). `sync`: 2, 4, 1, 3, 0.

## Where state lives

Everything the tool writes, except what `sync --apply` copies into the Claude
storage, goes under `~/.claude-account-move/` (override with the
`CLAUDE_ACCOUNT_MOVE_HOME` variable or `--state-dir`):

```
moves/<move-id>/before.json      metadata of the cards, deletion marks, accounts, task ids, transcript sizes and last-message times, settings counts and digests
moves/<move-id>/manifest.json    checksum of before.json, ready flag, fingerprint of the home directory, creation time
moves/<move-id>/report-*.json    the JSON reports of each run
moves/<move-id>/cards-copy/      only with prepare --full-copy
current-ready-move-id            the newest ready snapshot
sync-<time>-<id>.jsonl           journal of one sync --apply (a new file per run)
stamp-<time>-<id>.jsonl          journal of one stamp --apply (a new file per run)
```

In the default metadata-only mode the snapshot holds account and organization
ids, session ids and card file names. It holds no titles, no message text, no
email address and no path of your home directory (only a short fingerprint of
it). With `prepare --full-copy` the snapshot also contains complete raw copies of
the card and task files, which can hold titles, prompts and other private
fields. Either way, treat the directory as private and do not publish it.
The tool refuses to use a state directory that overlaps the Claude storage, your
Claude settings or logs, and refuses to write through a symlink anywhere inside
it. Its own code never writes bytecode caches. (The macOS system Python may
itself cache the bytecode of its standard library under `~/Library/Caches` on its
first start with a given home directory; set `PYTHONDONTWRITEBYTECODE=1` to
prevent that too.)

## Rolling back

- `prepare`, `finish` and all checks changed nothing; there is nothing to undo.
  Delete `moves/<move-id>` whenever you like.
- `sync --apply` only adds files and never overwrites. Undo exactly what it added:
  `./claude-account-move sync --undo ~/.claude-account-move/sync-<time>-<id>.jsonl`.
  A file is removed only when the journal proves this run published it (same
  device and inode as the staged copy, same content), the journal belongs to this
  home directory and one operation, and the file lies in a target folder recorded
  when the operation began. The destination is first moved to a private name with
  one atomic rename and checked there, so nobody can swap the file between the
  check and the deletion; a file that turns out not to be ours is put back without
  overwriting anything (the name is briefly absent). A file that merely has the
  same name or bytes (a card the app wrote, a card you restored) is kept and
  reported as ambiguous. Every record must carry its required fields: a record
  that does not is damage, and only the intact prefix before it is used. An
  interrupted journal is used up to its last complete line and the torn part is
  reported. Undo itself writes to the storage (it deletes), so it is exempt from
  the read-only promise above.
- The old account's folders are never touched. To go back, sign in to the app and
  the CLI with the old account again.

## Limitations

- Read-only everywhere except `sync --apply` (adds card files and may create the
  agent-mode folder of the target account when it does not exist yet),
  `sync --undo` (removes what `--apply` added), `stamp --apply` (rewrites card
  titles) and `stamp --undo`.
- `stamp` refuses to run when the storage could not be read completely, and its
  suffix is in local time, so after a change of time zone every old stamp moves
  by the same whole number of hours.
- Scheduled-task files must match the expected shape (a list of objects with
  unique ids; a missing key is an empty list, an explicit null is not);
  anything else is reported as an unreadable observation, not as "no tasks".
- Transcript preservation is judged by size, last message and a prefix digest
  (first 64 MiB), which assumes transcripts are append-only. A change beyond the
  first 64 MiB of a larger file, or a rewrite that keeps the prefix, is not seen.
  Unparsable lines in the last 400 kB of a transcript make the observation
  incomplete (an unfinished last line of a live session is ignored).
- The active folder is taken from the last mention in the app log
  (`~/Library/Logs/Claude/main.log`), because the organization is not always the
  one in `~/.claude.json`. If the log is missing, pass `--active-pair`.
- Permissions are checked by permission bits; writing is not attempted, so a
  read-only mount that still reports write permission is not detected.
- The storage layout was derived from observation of the desktop app, not from
  documentation. Newer app versions may move things.
- Python 3.9 to 3.12 were tried; only macOS is supported.

## Development

```sh
python3 -m unittest discover -s tests
```

The tests build a synthetic home directory in a temporary folder and never
touch your real one. The privacy test proves its scanner on invented words only;
the real list of private names is kept outside the repository. To apply such a
list as well, point `CLAUDE_ACCOUNT_MOVE_DENYLIST` at a file of `re:<pattern>` or
`cs:<pattern>` lines.

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Mr. Skill.
