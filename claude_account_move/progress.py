"""ASCII progress for long commands. Writes only through Out (stdout, or
stderr under --json, nothing when quiet). On a TTY the bar is redrawn in
place and a daemon thread keeps the seconds ticking; elsewhere every step
is one plain line and a long step adds a "still" line every 30 s."""
import os
import shutil
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


def fit(pct, head, tail, elapsed, spin, width):
    """One TTY frame, at most max(0, width-1) characters, spinner included.
    Shrink order (intended): keep the step name, because it tells the reader
    what is going on, and shrink the bar first, down to 10 cells; only then
    drop the tail (step name or limit), and last truncate hard."""
    room = max(0, width - 1)
    pct = max(0, min(100, int(pct)))
    sp = (" " + spin) if spin else ""
    for use_tail in (True, False):
        for cells in range(WIDTH, 9, -1):
            if use_tail:
                text = head + "  " + tail if tail else head
            else:
                text = head
            line = "[%s%s] %3d%%  %s  (%ds)%s" % (
                "#" * (cells * pct // 100), "." * (cells - cells * pct // 100),
                pct, text, int(elapsed), sp)
            if len(line) <= room:
                return line
    return line[:room]


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
                        self._write("\r" + self._frame(draw, now,
                                    SPIN[self.spin % 4]) + "\x1b[K")
                elif kind == "step" and now - self.last >= self.still:
                    self.last = now
                    self._write("  still %s (%ds)\n" % (name, int(now - self.t0)))

    def _write(self, text):
        s = self.out.stream
        if s is not None:
            s.write(text)
            s.flush()

    def _width(self):
        """Columns of the stream the bar is written to (stderr under --json)."""
        try:
            return os.get_terminal_size(self.out.stream.fileno()).columns
        except (AttributeError, OSError, ValueError):
            return shutil.get_terminal_size(fallback=(80, 24)).columns

    def _frame(self, draw, now, spin):
        width = self._width()
        pct, head, tail, el = draw(now)
        return fit(pct, head, tail, el, spin, width)

    def _emit(self, kind, name, draw):
        with self.lock:
            self.cur = (kind, name, draw)
            self.last = time.monotonic()
            if self.tty:
                self._write("\r" + self._frame(draw, self.last, " ") + "\x1b[K")
                self.open = True
            elif self.out.stream is not None:
                pct, head, tail, el = draw(self.last)
                self._write(render(pct, head + "  " + tail, el) + "\n")
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
        head = "step %d/%d" % (n, self.total)
        self._emit("step", name, lambda now: (pct, head, name, now - self.t0))

    def wait(self, what, elapsed, limit, plain=False):
        """Elapsed against the limit. Off a TTY only when plain is set."""
        if not (self.tty or plain):
            return
        base = time.monotonic() - elapsed
        tail = "limit %ds" % int(limit)

        def draw(now):
            el = now - base
            return (100 * el / limit if limit > 0 else 0, what, tail, el)
        self._emit("wait", what, draw)

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
            self.thread = None
        self.end_line()
        self.out.progress = None
