# backend/log_tee.py
"""stdout/stderr mirror for start.py: console + logs/service.log, every line time-stamped.

Each line starts with the wall-clock time, e.g. "10:53:10.718 [WS] ...", so latency
between log lines can be read directly (the "===== service start @ <ISO time> ====="
banner gives the date). print() writes the text and the newline separately and the
ASR workers print from other threads, so text is kept per thread until its newline:
one stamp per line, and lines of different threads are not mixed. flush() writes a
pending partial line (progress output) and its continuation gets no second stamp.
"""
import re
import threading
from datetime import datetime

_LINE_END = re.compile(r"(?<=\n)")


def timestamp(now: datetime) -> str:
    """'HH:MM:SS.mmm ' (local wall-clock time)."""
    return now.strftime("%H:%M:%S.%f")[:-3] + " "


class TimestampedTee:
    def __init__(self, console, log_file, clock=datetime.now):
        self.console = console
        self.log_file = log_file
        self._clock = clock
        self._lock = threading.Lock()
        self._local = threading.local()

    def _state(self):
        state = getattr(self._local, "state", None)
        if state is None:
            state = self._local.state = {"pending": "", "mid_line": False}
        return state

    def write(self, data):
        if not isinstance(data, str):
            return 0
        if not data:
            return 0
        state = self._state()
        text = state["pending"] + data
        cut = text.rfind("\n") + 1
        state["pending"] = text[cut:]
        if cut:
            self._emit(state, text[:cut])
        return len(data)

    def _emit(self, state, text: str) -> None:
        stamp = timestamp(self._clock())
        out = []
        for piece in _LINE_END.split(text):
            if not piece:
                continue
            if not state["mid_line"]:
                out.append(stamp)
            out.append(piece)
            state["mid_line"] = not piece.endswith("\n")
        stamped = "".join(out)
        with self._lock:
            for stream in (self.log_file, self.console):
                if stream is None:
                    continue
                try:
                    stream.write(stamped)
                except Exception:
                    pass

    def flush(self):
        state = self._state()
        if state["pending"]:
            pending, state["pending"] = state["pending"], ""
            self._emit(state, pending)
        with self._lock:
            for stream in (self.log_file, self.console):
                if stream is None:
                    continue
                try:
                    stream.flush()
                except Exception:
                    pass

    def isatty(self):
        return False

    def fileno(self):
        # uvicorn / libraries that probe fileno() get the log file fd; better
        # than crashing on a missing fileno when console is None. Bytes written
        # straight to it (child processes such as npm) carry no timestamp.
        return self.log_file.fileno()
