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

# State directory, first match wins:
#   1. the CLAUDE_ACCOUNT_MOVE_HOME environment variable;
#   2. a line CLAUDE_ACCOUNT_MOVE_HOME=<path> in the optional file local.env
#      next to this launcher (read line by line, never sourced or executed);
#   3. $HOME/.claude-account-move, the default of the program itself.
STATE_DIR="${CLAUDE_ACCOUNT_MOVE_HOME:-}"
if [ -z "$STATE_DIR" ] && [ -f local.env ] && [ -r local.env ]; then
  n=0
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    case "$line" in
      "" | "#"*) continue ;;
      CLAUDE_ACCOUNT_MOVE_HOME=*) ;;
      *)
        echo "warning: local.env line $n ignored (only CLAUDE_ACCOUNT_MOVE_HOME=<path> is read)" >&2
        continue
        ;;
    esac
    value="${line#CLAUDE_ACCOUNT_MOVE_HOME=}"
    case "$value" in
      \"?*\") value="${value#\"}"; value="${value%\"}" ;;
      \'?*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    case "$value" in
      '$HOME/'*) value="$HOME/${value#'$HOME/'}" ;;
      '~/'*) value="$HOME/${value#'~/'}" ;;
    esac
    if [ -z "$value" ]; then
      echo "warning: local.env line $n ignored (empty value)" >&2
      continue
    fi
    STATE_DIR="$value"
  done < local.env
fi
STATE_DIR="${STATE_DIR:-$HOME/.claude-account-move}"

./claude-account-move auto --state-dir "$STATE_DIR" --pause "$@"
exit $?
