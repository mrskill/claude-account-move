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


def _bar(cells, pct, pos):
    """pct None draws an indeterminate bouncer; pos moves it."""
    if pct is None:
        span = max(1, cells - 3)
        p = pos % (2 * span)
        p = 2 * span - p if p > span else p
        return "." * p + "<=>" + "." * (span - p)
    full = cells * pct // 100
    return "#" * full + "." * (cells - full)


def fit(pct, head, tail, elapsed, spin, width, pos=0):
    """One compact TTY frame, `[bar] 38% 2/8 name 12s |`, at most
    max(0, width-1) characters, spinner included.
    Shrink order (intended): keep the step name, because it tells the reader
    what is going on, and shrink the bar first, down to 10 cells; only then
    cut the tail (step name or limit) short, then drop it, and last truncate
    hard."""
    room = max(0, width - 1)
    if pct is not None:
        pct = max(0, min(100, int(pct)))
    shown = "--%" if pct is None else "%d%%" % pct
    sp = (" " + spin) if spin else ""

    def build(cells, text):
        return "[%s] %s %s %ds%s" % (_bar(cells, pct, pos), shown, text,
                                     int(elapsed), sp)
    for use_tail in (True, False):
        for cells in range(WIDTH, 9, -1):
            line = build(cells, head + " " + tail if (use_tail and tail) else head)
            if len(line) <= room:
                return line
        if use_tail and tail:
            avail = room - len(build(10, head + " "))
            if avail >= 6:
                return build(10, head + " " + tail[:avail])
    return line[:room]


class Progress(object):
    """One line per step on a TTY: redrawn in place while the step runs, then
    finalized once (full bar, "done", its own seconds) before the next step."""

    def __init__(self, out, total=0, tick=1.0, still=30.0, throttle=0.25):
        self.out = out
        self.total = total
        self.n = 0
        self.tick = tick
        self.still = still
        self.throttle = throttle
        self.t0 = time.monotonic()
        self.step_t0 = self.t0
        self.open = False                # a TTY line is drawn and not ended
        self.muted = False               # say() cleared the line
        self.complete = False            # the step named "done" was reached
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.cur = None                  # (kind, name, draw function)
        self.frac = None                 # real progress of the step, or None
        self.last = self.t0              # last activity, for "still" lines
        self.last_cb = 0.0
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
                if self.stop.is_set() or self.cur is None or self.muted:
                    continue
                kind, name, draw = self.cur
                now = time.monotonic()
                if self.tty:
                    if self.open:
                        self.spin += 1
                        self._draw(now)
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

    def _frame(self, now, spin, final=False):
        kind, name, draw = self.cur
        pct, head, tail, el = draw(now)
        if final:
            pct = 100
        pos = int((now - self.step_t0) * 5)
        return fit(pct, head, tail, el, spin, self._width(), pos)

    def _draw(self, now):
        self._write("\r" + self._frame(now, SPIN[self.spin % 4]) + "\x1b[K")
        self.open = True

    def _finalize(self, label):
        """Write the finished line of the current step and end it."""
        if self.cur is None or not self.tty:
            return
        line = self._frame(time.monotonic(), label, final=(label == "done"))
        self._write("\r" + line + "\x1b[K\n")
        self.open = False

    def _emit(self, kind, name, draw, base_pct=0):
        with self.lock:
            if kind == "step" or (self.cur is not None
                                  and self.cur[0] == "step"):
                self._finalize("done")
            if kind == "step":
                self.step_t0 = time.monotonic()
                self.frac = None
            self.cur = (kind, name, draw)
            self.muted = False
            self.last = time.monotonic()
            if self.tty:
                self._draw(self.last)
            elif self.out.stream is not None:
                pct, head, tail, el = draw(self.last)
                if kind == "step":
                    pct, el = base_pct, self.last - self.t0
                    head = "step " + head
                self._write(render(pct, head + "  " + tail, el) + "\n")
        self._start()

    def update(self, done, total):
        """Real progress of the running step (callback for store.scan etc.).
        Throttled here: at most a few redraws per second."""
        if not total or total <= 0:
            return
        now = time.monotonic()
        self.frac = max(0, min(100, 100 * done / total))
        if self.tty and now - self.last_cb >= self.throttle:
            with self.lock:
                self.last_cb = now
                if self.cur is not None and not self.muted and self.open:
                    self._draw(now)

    def end_line(self):
        """Clear a drawn TTY line so ordinary text starts on a clean row."""
        with self.lock:
            if self.open:
                self._write("\r\x1b[K")
            self.open = False
            self.muted = True          # silent until the next step or wait

    def step(self, name):
        """Finish the previous step's line and start this step's line."""
        self.n += 1
        n = self.n
        base = 100 * (n - 1) // max(1, self.total)
        head = "%d/%d" % (n, self.total)
        done = name == "done"
        if done:
            self.complete = True

        def draw(now):
            pct = 100 if done else self.frac
            return (pct, head, name, now - self.step_t0)
        self._emit("step", name, draw, 100 if done else base)

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
        with self.lock:
            self._finalize("done" if self.complete else "stop")
            self.cur = None
        self.out.progress = None
