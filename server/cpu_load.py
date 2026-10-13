"""How much CPU the server itself uses, for the dashboard to show.

Most of the server runs on one thread, the event loop that takes every CAN
frame and decodes every message. Near 100 % of a core it can take no more: a
bus that asks for more is not kept up with. The reading says how near that is.
"""

from __future__ import annotations

import time
from typing import Callable, Optional


class ProcessCpu:
    """This process's CPU use, in percent of one core, averaged over ``interval`` seconds or more.

    All its threads count (SQLite's, a CAN hub's), so it can pass 100. Any
    number of readers may ask: a new figure is worked out once ``interval``
    has passed since the last, and the same one given until then.
    """

    def __init__(self, interval: float = 1.0, cpu_clock: Callable[[], float] = time.process_time,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._interval = interval
        self._cpu_clock = cpu_clock
        self._clock = clock
        self._last = (clock(), cpu_clock())
        self._percent: Optional[float] = None

    def read(self) -> Optional[float]:
        """The latest figure; None until the first ``interval`` has passed."""
        now = self._clock()
        elapsed = now - self._last[0]
        if elapsed >= self._interval:
            used = self._cpu_clock()
            self._percent = round(100.0 * (used - self._last[1]) / elapsed, 1)
            self._last = (now, used)
        return self._percent
