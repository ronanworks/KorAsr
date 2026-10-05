"""start.py's log tee: every line of logs/service.log starts with the wall-clock time."""

import io
import re
import threading
import unittest
from datetime import datetime, timedelta

from backend.log_tee import TimestampedTee, timestamp

STAMP = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3} ")


class _Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 2, 10, 53, 10, 718005)

    def __call__(self):
        current = self.now
        self.now += timedelta(milliseconds=1)
        return current


class TimestampedTeeTests(unittest.TestCase):
    def setUp(self):
        self.log, self.console = io.StringIO(), io.StringIO()
        self.tee = TimestampedTee(self.console, self.log, clock=_Clock())

    def test_format_is_wall_clock_with_milliseconds(self):
        self.assertEqual(timestamp(datetime(2026, 10, 2, 10, 53, 10, 718005)), "10:53:10.718 ")

    def test_print_style_writes_get_one_stamp_per_line(self):
        print("[WS] Session abc connected", file=self.tee)        # write(text) + write("\n")
        print("[ASR]", "dur=1.00s", file=self.tee)                  # several pieces, one line
        self.assertEqual(self.log.getvalue(),
                         "10:53:10.718 [WS] Session abc connected\n10:53:10.719 [ASR] dur=1.00s\n")
        self.assertEqual(self.console.getvalue(), self.log.getvalue())

    def test_half_lines_are_stamped_only_at_the_line_start(self):
        self.tee.write("abc")
        self.tee.write("def\n")
        self.tee.write("a\nb")
        self.tee.write("c\n\n")
        lines = self.log.getvalue().splitlines()
        self.assertEqual(lines, ["10:53:10.718 abcdef", "10:53:10.719 a", "10:53:10.720 bc", "10:53:10.720 "])

    def test_every_line_of_a_multi_line_write_is_stamped(self):
        self.tee.write("Traceback (most recent call last):\n  File \"x.py\"\nValueError: boom\n")
        self.assertTrue(all(STAMP.match(line) for line in self.log.getvalue().splitlines()))
        self.assertEqual(len(self.log.getvalue().splitlines()), 3)

    def test_flush_writes_a_partial_line_whose_continuation_is_not_stamped_again(self):
        self.tee.write("loading 50%")
        self.assertEqual(self.log.getvalue(), "")
        self.tee.flush()
        self.tee.write(" 100%\n")
        self.assertEqual(self.log.getvalue(), "10:53:10.718 loading 50% 100%\n")

    def test_threads_do_not_split_each_others_lines(self):
        barrier = threading.Barrier(2)

        def worker(name):
            barrier.wait()
            for i in range(200):
                self.tee.write(f"[{name}] line {i}")
                self.tee.write("\n")

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("ASR", "WS ")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        lines = self.log.getvalue().splitlines()
        self.assertEqual(len(lines), 400)
        self.assertTrue(all(re.match(r"^\d{2}:\d{2}:\d{2}\.\d{3} \[(ASR|WS )\] line \d+$", line) for line in lines))

    def test_console_none_still_logs(self):
        tee = TimestampedTee(None, self.log, clock=_Clock())   # pythonw has no console
        print("hello", file=tee)
        tee.flush()
        self.assertEqual(self.log.getvalue(), "10:53:10.718 hello\n")

    def test_non_text_is_ignored(self):
        self.assertEqual(self.tee.write(b"bytes"), 0)
        self.assertEqual(self.tee.write(""), 0)
        self.assertEqual(self.log.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
