"""ASCII progress for long commands. Writes only through Out (stdout, or
stderr under --json, nothing when quiet). On a TTY the bar is redrawn in
place and a daemon thread keeps the seconds ticking; elsewhere every step
is one plain line and a long step adds a "still" line every 30 s."""
import threading
import time

WIDTH = 24
SPIN = "|/-\\"

HINTS = {"identity_invalid": "an account file is missing or unreadable",
         "app_not_switched": "sign in to the app with the new account",
         "cli_lags": "run `claude /login` in a terminal"}


def render(pct, text, elapsed):
    pct = max(0, min(100, int(pct)))
    filled = WIDTH * pct // 100
    return "[%s%s] %3d%%  %s  (%ds)" % ("#" * filled, "." * (WIDTH - filled),
                                        pct, text, int(elapsed))


class Progress(object):
    def __init__(self, out, total=0, tick=1.0, still=30.0):
        self.out = out
        self.total = total
        self.n = 0
        self.tick = tick
        self.still = still
        self.t0 = time.monotonic()
        self.open = False                # a TTY line is drawn and not ended
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.cur = None                  # (kind, name, draw function)
        self.last = self.t0              # last activity, for "still" lines
        self.spin = 0
        s = out.stream
        self.tty = bool(s is not None and hasattr(s, "isatty") and s.isatty())
        out.progress = self

    def _start(self):
        if self.thread is None and self.out.stream is not None:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def _run(self):
        while not self.stop.wait(self.tick):
            with self.lock:
                if self.stop.is_set() or self.cur is None:
                    continue
                kind, name, draw = self.cur
                now = time.monotonic()
                if self.tty:
                    if self.open:
                        self.spin += 1
                        self._write("\r" + draw(now) + " " + SPIN[self.spin % 4]
                                    + "\x1b[K")
                elif kind == "step" and now - self.last >= self.still:
                    self.last = now
                    self._write("  still %s (%ds)\n" % (name, int(now - self.t0)))

    def _write(self, text):
        s = self.out.stream
        if s is not None:
            s.write(text)
            s.flush()

    def _emit(self, kind, name, draw):
        with self.lock:
            self.cur = (kind, name, draw)
            self.last = time.monotonic()
            line = draw(self.last)
            if self.tty:
                self._write("\r" + line + "\x1b[K")
                self.open = True
            elif self.out.stream is not None:
                self._write(line + "\n")
        self._start()

    def end_line(self):
        """Finish a drawn TTY line so ordinary text starts on a fresh one."""
        with self.lock:
            if self.open:
                self._write("\n")
            self.open = False
            self.cur = None            # silent until the next step or wait

    def step(self, name):
        """Advance to the next step and show it."""
        self.n += 1
        n = self.n
        pct = 100 if name == "done" else 100 * (n - 1) // max(1, self.total)
        text = "step %d/%d  %s" % (n, self.total, name)
        self._emit("step", name, lambda now: render(pct, text, now - self.t0))

    def wait(self, what, elapsed, limit, plain=False):
        """Elapsed against the limit. Off a TTY only when plain is set."""
        if not (self.tty or plain):
            return
        base = time.monotonic() - elapsed
        text = "%s  limit %ds" % (what, int(limit))

        def draw(now):
            el = now - base
            return render(100 * el / limit if limit > 0 else 0, text, el)
        self._emit("wait", what, draw)

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
            self.thread = None
        self.end_line()
        self.out.progress = None
