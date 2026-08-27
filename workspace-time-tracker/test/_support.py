"""Shared test scaffolding."""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
for path in (SRC, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

ENTRYPOINT = os.path.join(ROOT, "src", "main.py")


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


class FakeClock(object):
    """A wall clock the tests set by hand.

    Wall clock, not monotonic: entries carry local timestamps and the midnight rollover
    is a calendar rule, so tests need to place 'now' at a chosen date and time.
    """

    def __init__(self, start=None):
        if start is None:
            import datetime
            start = datetime.datetime(2026, 8, 27, 9, 0, 0).timestamp()
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now

    def set(self, year, month, day, hour=0, minute=0, second=0):
        import datetime
        self.now = datetime.datetime(year, month, day, hour, minute,
                                     second).timestamp()
        return self.now
