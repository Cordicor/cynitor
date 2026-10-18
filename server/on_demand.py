"""Decoding on demand: only the subjects watched or recorded are decoded, the others counted from their frames.

For a bus that carries more than can be decoded: the scanner listens only to
what a dashboard shows the values of or a recording takes (ScannerNode.set_decoding),
and every other subject's rate is counted from its CAN frames (transfer_rates)
and reported as an `undecoded` event. Held by the session (main.CANSession),
which starts it on connect when the switch is on and stops it on teardown.
"""

import asyncio
import logging
from typing import Any, Callable, Optional

from transfer_rates import TransferRates

logger = logging.getLogger(__name__)

# For a SocketCAN socket: let the kernel pass message frames only, as
# transfer_rates counts no others (bit 25 marks a service frame).
MESSAGE_FRAMES = [{"can_id": 0, "can_mask": 1 << 25, "extended": True}]


class OnDemandDecoding:
    """The count of the frames, and what is decoded, while decoding on demand runs.

    ``open_tap(on_frame, can_filters)`` opens a tap on the bus and returns what
    closes it; ``scanner()`` and ``event_logger()`` give the session's, or None.
    """

    def __init__(self, open_tap: Callable[..., Callable[[], None]],
                 scanner: Callable[[], Any], event_logger: Callable[[], Any]) -> None:
        self._open_tap = open_tap
        self._scanner = scanner
        self._event_logger = event_logger
        self.watched: dict[object, frozenset[int]] = {}  # by dashboard: the subjects it shows the values of
        self.rates: Optional[TransferRates] = None       # the frame-level count, while it runs
        self._close_tap: Optional[Callable[[], None]] = None
        self._task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self.rates is not None

    def start(self) -> None:
        self.rates = TransferRates()
        self._close_tap = self._open_tap(self.rates.on_frame, MESSAGE_FRAMES)
        self._task = asyncio.create_task(self._loop())
        self.apply()
        logger.info("Decoding on demand: only the subjects watched or recorded are decoded, the rest counted")

    def stop(self, decode_all: bool = True) -> None:
        """Stop counting; ``decode_all`` unless the session is ending anyway."""
        if self._task is not None:
            self._task.cancel()
        self._task = None
        if self._close_tap is not None:
            self._close_tap()
        self._close_tap = None
        scanner = self._scanner()
        if decode_all and self.rates is not None and scanner is not None:
            scanner.set_decoding(None)
            logger.info("Decoding every subject again")
        self.rates = None

    def set_watched(self, dashboard: object, subject_ids) -> None:
        """The subjects ``dashboard`` shows the values of; None once it has gone."""
        if subject_ids is None:
            self.watched.pop(dashboard, None)
        else:
            self.watched[dashboard] = frozenset(subject_ids)
        self.apply()

    def wanted(self) -> set[int]:
        """The subjects some dashboard watches or some recording takes."""
        wanted: set[int] = set().union(*self.watched.values())
        scanner, event_logger = self._scanner(), self._event_logger()
        if event_logger is not None and scanner is not None:
            for subject_id, message_type in scanner.subject_types.items():
                if subject_id not in wanted and event_logger.records_subject(
                        subject_id, scanner.active_publishers.get(subject_id, ()),
                        message_type.rpartition(".")[2]):
                    wanted.add(subject_id)
        return wanted

    def apply(self) -> None:
        scanner = self._scanner()
        if self.rates is not None and scanner is not None:
            scanner.set_decoding(self.wanted())

    async def _loop(self) -> None:
        """Every second: follow the recordings that started or stopped, and report the rates counted."""
        while True:
            await asyncio.sleep(1.0)
            try:
                self.apply()
                scanner = self._scanner()
                if scanner is not None and self.rates is not None:
                    await scanner.queue_rates(self.rates.sample())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a second's rates missed must not end the loop
                logger.error("Decoding on demand: %s", exc, exc_info=True)
