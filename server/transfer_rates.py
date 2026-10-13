"""Message rates counted from CAN frames, for the subjects Cynitor does not decode.

While it decodes on demand (CANSession.set_decode_on_demand), only the
subjects someone watches or records go through pycyphal: a busy bus carries
more than can be decoded. Every other subject is counted here as its frames
pass, by the subject-ID and node-ID in the CAN ID of each transfer's last
frame. A few dictionary operations a frame, so a full bus can be counted.
"""

from __future__ import annotations

import collections
import time
from typing import Callable

# In the extended CAN ID of a Cyphal/CAN v1.0 message frame these are all 0:
# bit 25 (a service), bit 24 (an anonymous node, which has no node-ID to count
# under), bit 23 (reserved) and bit 7 (set by Cyphal v1.1; see cyphal_v11).
_NOT_A_MESSAGE = (1 << 25) | (1 << 24) | (1 << 23) | (1 << 7)
_SUBJECT_AND_NODE = (0x1FFF << 8) | 0x7F
# In a frame's last byte: start and end of transfer, and the toggle, which
# starts at 1 in Cyphal v1 and at 0 in DroneCAN.
_START, _END, _TOGGLE = 0x80, 0x40, 0x20


class TransferRates:
    """Counts message transfers by (subject-ID, node-ID); `sample` turns the counts into rates."""

    WINDOW_S = 10.0    # a rate is over the last ten seconds, as the scanner's is
    QUIET_S = 2.0      # a publisher whose last transfer is older than this, and than
    QUIET_PERIODS = 3  # ... three of its periods, no longer counts: the scanner's rule

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # Written by the thread the frames come on; `sample` takes it whole.
        self._counts: dict[int, int] = {}      # subject and node, as in the CAN ID -> transfers since the last sample
        self._last_seen: dict[int, float] = {}  # -> when the last one came, as its frame is stamped
        self._samples: dict[int, collections.deque] = {}  # -> (time, transfers) of the samples in the window
        self._since: dict[int, float] = {}     # -> the time its counting began
        self._sampled_at = clock()

    def on_frame(self, msg) -> None:
        """Count ``msg`` (a python-can message) if it ends a Cyphal v1.0 message transfer."""
        data = msg.data
        if not data or not msg.is_extended_id or msg.is_error_frame:
            return
        tail = data[-1]
        if not tail & _END or tail & (_START | _TOGGLE) == _START:  # not a last frame, or DroneCAN's
            return
        can_id = msg.arbitration_id
        if can_id & _NOT_A_MESSAGE:
            return
        key = can_id & _SUBJECT_AND_NODE
        self._counts[key] = self._counts.get(key, 0) + 1
        self._last_seen[key] = msg.timestamp

    def sample(self) -> dict[tuple[int, int], tuple[float, float]]:
        """{(subject-ID, node-ID): (transfers a second, time of the last one)} of the publishers still sending.

        To be called about once a second, from one thread.
        """
        now = self._clock()
        since, self._sampled_at = self._sampled_at, now
        counts, self._counts = self._counts, {}
        for key, count in list(counts.items()):
            if key not in self._samples:
                self._samples[key] = collections.deque()
                self._since[key] = since
            self._samples[key].append((now, count))
        rates: dict[tuple[int, int], tuple[float, float]] = {}
        for key, samples in list(self._samples.items()):
            while samples and samples[0][0] <= now - self.WINDOW_S:
                samples.popleft()
            elapsed = now - max(self._since[key], now - self.WINDOW_S)
            rate = sum(count for _, count in samples) / elapsed if samples and elapsed > 0 else 0.0
            quiet_after = max(self.QUIET_S, self.QUIET_PERIODS / rate) if rate > 0 else self.QUIET_S
            if not samples or now - samples[-1][0] > quiet_after:
                del self._samples[key], self._since[key]
                self._last_seen.pop(key, None)
                continue
            rates[(key >> 8, key & 0x7F)] = (rate, self._last_seen.get(key, 0.0))
        return rates
