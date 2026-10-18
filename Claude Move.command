#!/bin/bash
# Double-click launcher: the whole Claude account move in one window.
# Resolves symlinks, so an alias or symlink on the Desktop works too.
# The closing prompt ("press Enter") is printed by the program itself (--pause).
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  case "$SOURCE" in /*) ;; *) SOURCE="$DIR/$SOURCE" ;; esac
done
if ! cd -P "$(dirname "$SOURCE")"; then
  echo "Cannot open the program folder. Press Enter to close this window."
  read -r _
  exit 1
fi

STATE_DIR="${CLAUDE_ACCOUNT_MOVE_HOME:-$HOME/MEGAsync/Private/Work/claude-account-move-state}"

./claude-account-move auto --state-dir "$STATE_DIR" --pause "$@"
exit $?
