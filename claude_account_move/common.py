"""Shared helpers: locations, JSON reading, time formatting, state directory."""
import contextlib
import fcntl
import hashlib
import subprocess
import json
import os
import re
import tempfile
from datetime import datetime, timezone

STORES = ("claude-code-sessions", "local-agent-mode-sessions")
CARD_PREFIX = "local_"
TOMB_PREFIX = "deleted_"
ENV_STATE = "CLAUDE_ACCOUNT_MOVE_HOME"
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                     r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
TRANSCRIPT_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
                           r"-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$")

# Exit codes shared by every command.
EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_INPUT = 2
EXIT_FAIL = 3
EXIT_NO_STORE = 4
EXIT_WAIT = 5
EXIT_LOGIN_TIMEOUT = 6
EXIT_NO_BASELINE = 7


class Paths(object):
    """All locations, derived from one home directory."""

    def __init__(self, home=None, state=None):
        h = home or os.environ.get("HOME") or os.path.expanduser("~")
        self.home = os.path.abspath(h)
        self.support = os.path.join(self.home, "Library", "Application Support",
                                    "Claude")
        self.projects = os.path.join(self.home, ".claude", "projects")
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        self.cli_config = os.path.join(self.home, ".claude.json")
        self.app_config = os.path.join(self.support, "config.json")
        self.logs = os.path.join(self.home, "Library", "Logs")
        self.main_log = os.path.join(self.logs, "Claude", "main.log")
        st = state or os.environ.get(ENV_STATE) or os.path.join(
            self.home, ".claude-account-move")
        self.state = os.path.abspath(os.path.expanduser(st))


def inside(child, parent):
    """True when child equals parent or lies below it (both already resolved)."""
    child = os.path.normcase(child)
    parent = os.path.normcase(parent)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def overlap_problem(path, paths):
    """Why a resolved write destination is unsafe, or None."""
    real_path = os.path.realpath(path)
    protected = [paths.support, paths.projects, paths.settings,
                 paths.cli_config, paths.logs]
    for p in protected:
        real = os.path.realpath(p)
        if inside(real_path, real) or inside(real, real_path):
            return "state path %s overlaps protected path %s" % (real_path, real)
    return None


def state_dir_problem(paths):
    """Why the state directory is unsafe, or None.

    The tool writes only into its state directory. That directory must never be
    a protected input, lie inside one, or contain one.
    """
    return overlap_problem(paths.state, paths)


def safe_subdir(paths, *parts):
    """Create state/<parts> one component at a time and return its path.

    No component may be a symlink, and the resolved result must equal the
    expected location below the resolved state root and must not overlap a
    protected path. This is the write boundary for everything below the state
    root, not only for the root itself.
    """
    os.makedirs(paths.state, exist_ok=True)
    root_real = os.path.realpath(paths.state)
    cur = paths.state
    for part in parts:
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise OSError("refusing symlink inside the state directory: %s" % cur)
        os.makedirs(cur, exist_ok=True)
    if os.path.realpath(cur) != os.path.join(root_real, *parts):
        raise OSError("state subdirectory resolves elsewhere: %s" % cur)
    problem = overlap_problem(cur, paths)
    if problem:
        raise OSError(problem)
    return cur


def open_new_file(path):
    """Open a NEW file for appending; fails if it exists or is a symlink."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "a", encoding="utf-8")


def read_json(path):
    """(data, error). error is None on success."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, "missing"
    except Exception as exc:  # unreadable or malformed
        return None, "%s: %s" % (type(exc).__name__, str(exc)[:60])


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write(path, data):
    """Write bytes to path through a temp file in the same directory.

    Refuses to write through a symlink placed at the destination.
    """
    if os.path.islink(path):
        raise OSError("refusing to write through symlink: %s" % path)
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path, obj):
    atomic_write(path, (json.dumps(obj, indent=1, sort_keys=True,
                                   ensure_ascii=False) + "\n").encode("utf-8"))


def uuid_or_none(value):
    if isinstance(value, str) and UUID_RE.match(value):
        return value.lower()
    return None


def read_identity(paths):
    """Typed accounts of the CLI and of the desktop app.

    Each side is {"value": uuid or None, "status": ok|missing|absent|invalid}.
    """
    out = {}
    cli, err = read_json(paths.cli_config)
    if err == "missing":
        cli_state = ("missing", None, None)
    elif err:
        cli_state = ("invalid", None, None)
    else:
        oa = (cli or {}).get("oauthAccount") if isinstance(cli, dict) else None
        raw = oa.get("accountUuid") if isinstance(oa, dict) else None
        org = oa.get("organizationUuid") if isinstance(oa, dict) else None
        if raw is None:
            cli_state = ("absent", None, uuid_or_none(org))
        elif uuid_or_none(raw):
            cli_state = ("ok", uuid_or_none(raw), uuid_or_none(org))
        else:
            cli_state = ("invalid", None, None)
    out["cli"] = {"status": cli_state[0], "value": cli_state[1]}
    out["cli_org"] = cli_state[2]
    app, err = read_json(paths.app_config)
    if err == "missing":
        out["app"] = {"status": "missing", "value": None}
    elif err:
        out["app"] = {"status": "invalid", "value": None}
    else:
        raw = app.get("lastKnownAccountUuid") if isinstance(app, dict) else None
        if raw is None:
            out["app"] = {"status": "absent", "value": None}
        elif uuid_or_none(raw):
            out["app"] = {"status": "ok", "value": uuid_or_none(raw)}
        else:
            out["app"] = {"status": "invalid", "value": None}
    return out


def iso_to_ms(ts):
    if not ts or not isinstance(ts, str):
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00"))
                   .astimezone(timezone.utc).timestamp() * 1000)
    except Exception:
        return None


def fmt_ms(ms):
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def now_rfc3339():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class LockBusy(Exception):
    """Another write operation on this home directory is already running."""


@contextlib.contextmanager
def write_lock(paths):
    """One writer at a time per Claude storage, whatever state directory is used.

    The lock is an flock on the resolved Claude support folder itself, so it is
    derived from the storage, not from the state directory, and no lock file is
    created anywhere.
    """
    target = os.path.realpath(paths.support)
    if not os.path.isdir(target):
        yield
        return
    fd = os.open(target, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LockBusy("another write operation on this Claude storage is running")
        yield
    finally:
        os.close(fd)


APP_BUNDLE = "/Applications/Claude.app/Contents/"
# Helpers of the bundle that can outlive the app: the Chrome extension starts
# chrome-native-host by itself, and the crash handler may linger after a quit.
# Neither one holds the storage, so neither blocks a write.
NATIVE_HOST = APP_BUNDLE + "Helpers/chrome-native-host"
CRASHPAD_SUFFIX = "/chrome_crashpad_handler"
_ARGS_SPLIT_RE = re.compile(r"\s--")


def _lingering_helper(command):
    """True for a process of the bundle that may outlive the app (see above)."""
    exe = _ARGS_SPLIT_RE.split(command.strip(), 1)[0].strip()
    if exe == NATIVE_HOST or exe.startswith(NATIVE_HOST + " "):
        return True
    return exe.startswith(APP_BUNDLE) and exe.endswith(CRASHPAD_SUFFIX)


def _short(text, limit=120):
    return text if len(text) <= limit else text[:limit - 3] + "..."


def claude_app_blockers():
    """Descriptions of what says the Claude app is running; [] when it is not.

    Two checks: a process named exactly Claude, and any process started from
    the app bundle (its helpers), except the helpers that outlive the app
    (_lingering_helper). A failure to run the check, or an answer that cannot
    be read, counts as "running": the answer must be known before the storage
    is written. The process-lookup command can be replaced with
    CLAUDE_ACCOUNT_MOVE_PGREP (tests).
    """
    cmd = os.environ.get("CLAUDE_ACCOUNT_MOVE_PGREP", "pgrep")
    found = []
    for args in (["-x", "Claude"], ["-fl", APP_BUNDLE]):
        try:
            r = subprocess.run([cmd] + args, capture_output=True)
        except OSError as exc:
            return ["cannot run %s: %s" % (cmd, exc)]
        if r.returncode == 1:
            continue
        if r.returncode != 0:
            return ["%s failed with exit code %d" % (cmd, r.returncode)]
        lines = [ln.strip() for ln in
                 r.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
        if args[0] == "-x":
            found.append(_short(lines[0]) if lines else "a process named Claude")
            continue
        if not lines:
            found.append("a process of " + APP_BUNDLE)
        for ln in lines:
            parts = ln.split(None, 1)
            command = parts[1] if len(parts) == 2 else ln
            if not _lingering_helper(command):
                found.append(_short(ln))
    return found


def claude_app_running():
    """True when the Claude desktop app is running, or when that cannot be told."""
    return bool(claude_app_blockers())


def app_blockers_note():
    """Sentence for a refusal message: what exactly blocks. May be empty."""
    found = claude_app_blockers()
    if not found:
        return ""
    more = " (+%d more)" % (len(found) - 3) if len(found) > 3 else ""
    return " Blocking process: " + "; ".join(found[:3]) + more + "."
