"""Synthetic Claude storage for tests. Never touches a real home directory."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "claude-account-move")
CC = "claude-code-sessions"
LAM = "local-agent-mode-sessions"


def uid(n, k=0):
    return "%08x-0000-4000-8000-%012x" % (n, k)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Home(object):
    def __init__(self, root):
        self.root = root
        self.home = os.path.join(root, "home")
        self.state = os.path.join(root, "state")
        self.support = os.path.join(self.home, "Library", "Application Support",
                                    "Claude")
        self.projects = os.path.join(self.home, ".claude", "projects", "proj")
        os.makedirs(self.home)

    def env(self, **extra):
        e = dict(os.environ)
        e.update({"HOME": self.home, "CLAUDE_ACCOUNT_MOVE_HOME": self.state,
                  "CLAUDE_ACCOUNT_MOVE_POLL_S": "0.05",
                  "CLAUDE_ACCOUNT_MOVE_LOGIN_POLL_S": "0.05"})
        e.pop("CLAUDE_ACCOUNT_MOVE_SYNC_HEARTBEAT", None)
        e.update(extra)
        return e

    def run(self, *args, **envx):
        r = subprocess.run([sys.executable, ENTRY] + list(args), env=self.env(**envx),
                           capture_output=True, text=True, timeout=120)
        return r

    def pair(self, store, acc, org):
        d = os.path.join(self.support, store, acc, org)
        os.makedirs(d, exist_ok=True)
        return d

    def card(self, pair_dir, n, cid, act=None, turns=3, title="t"):
        act = int(time.time() * 1000) - 5000 if act is None else act
        with open(os.path.join(pair_dir, "local_%s.json" % uid(n, 9)), "w") as fh:
            json.dump({"cliSessionId": cid, "lastActivityAt": act,
                       "completedTurns": turns, "title": title,
                       "padding": "x" * 2000}, fh)
        return "local_%s.json" % uid(n, 9)

    def transcript(self, sid, last_ms=None):
        os.makedirs(self.projects, exist_ok=True)
        last_ms = int(time.time() * 1000) if last_ms is None else last_ms
        with open(os.path.join(self.projects, sid + ".jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "user", "timestamp": iso(last_ms - 60000),
                                 "message": {"content": "hello"}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "timestamp": iso(last_ms),
                                 "message": {"content": "hi"}}) + "\n")
            fh.write(json.dumps({"type": "queue-operation",
                                 "timestamp": iso(last_ms + 99999)}) + "\n")

    def accounts(self, cli=None, app=None, raw_cli=None, raw_app=None):
        os.makedirs(self.support, exist_ok=True)
        if raw_cli is not None:
            with open(os.path.join(self.home, ".claude.json"), "w") as fh:
                fh.write(raw_cli)
        elif cli:
            with open(os.path.join(self.home, ".claude.json"), "w") as fh:
                json.dump({"oauthAccount": {"accountUuid": cli,
                                            "organizationUuid": uid(7)}}, fh)
        if raw_app is not None:
            with open(os.path.join(self.support, "config.json"), "w") as fh:
                fh.write(raw_app)
        elif app:
            with open(os.path.join(self.support, "config.json"), "w") as fh:
                json.dump({"lastKnownAccountUuid": app}, fh)

    def log(self, *pairs):
        d = os.path.join(self.home, "Library", "Logs", "Claude")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "main.log"), "w") as fh:
            for acc, org in pairs:
                fh.write("2026-01-01 info: path %s/%s/%s/local_x.json\n"
                         % (CC, acc, org))


def tree_digest(root):
    """Content, mode, mtime and link targets of every entry under root."""
    out = {}
    for dp, dns, fns in os.walk(root):
        for n in dns + fns:
            p = os.path.join(dp, n)
            st = os.lstat(p)
            if os.path.islink(p):
                out[p] = ("link", os.readlink(p))
            elif os.path.isfile(p):
                with open(p, "rb") as fh:
                    out[p] = ("file", st.st_mode, st.st_mtime_ns, fh.read())
            else:
                out[p] = ("dir", st.st_mode, st.st_mtime_ns)
    return out
