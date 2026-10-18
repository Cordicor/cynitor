"""A simulated CAN bus with demo devices on it, for trying Cynitor without hardware.

The bus is an in-process python-can virtual channel, the "wire". The CAN hub
opens it as it opens an adapter, and the demo devices (see demo_devices) are
pycyphal nodes on it, as devices are on a real bus. So it works on every OS,
and its frames count toward the bus load and reach raw logs and frame
capture, as a real bus's do. Its rates are fixed (can_config.SIM_BUSES).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Awaitable, Callable, Optional

import can

logger = logging.getLogger(__name__)

_channel_numbers = itertools.count(1)


class SimulatedBus:
    """One simulated bus: its wire, and the demo devices on it.

    ``devices`` runs the devices, given a transport factory, until cancelled;
    demo_devices.run by default, which needs the compiled DSDL.
    """

    def __init__(self, bitrate: int, data_bitrate: Optional[int] = None,
                 devices: Optional[Callable[[Callable], Awaitable[None]]] = None) -> None:
        self.bitrate = bitrate
        self.data_bitrate = data_bitrate  # None: Classic CAN
        self.channel = f"cynitor-sim-{next(_channel_numbers)}"
        self._devices = devices
        self._task: Optional[asyncio.Task] = None

    def open_wire(self, _spec: str = "", _bitrate: int = 0, _data_bitrate: Optional[int] = None) -> can.BusABC:
        """The hub's end of the wire, which it opens as it would an adapter."""
        return can.ThreadSafeBus(interface="virtual", channel=self.channel)

    def make_transport(self, node_id: Optional[int]):
        """A pycyphal transport on the wire for ``node_id`` (None: anonymous), Classic or CAN FD as the bus is."""
        from pycyphal.transport.can import CANTransport
        from pycyphal.transport.can.media.pythoncan import PythonCANMedia
        spec = f"virtual:{self.channel}"
        if self.data_bitrate is None:
            media = PythonCANMedia(spec, self.bitrate)
        else:
            media = PythonCANMedia(spec, (self.bitrate, self.data_bitrate), 64)
        return CANTransport(media, local_node_id=node_id)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the devices; they run until stop()."""
        devices = self._devices
        if devices is None:
            from demo_devices import run
            devices = run
        self._task = asyncio.create_task(self._run(devices), name=f"{self.channel}-devices")

    async def _run(self, devices: Callable[[Callable], Awaitable[None]]) -> None:
        try:
            await devices(self.make_transport)
        except Exception:
            logger.exception("Simulated bus %s: the demo devices stopped", self.channel)

    async def stop(self) -> None:
        """Stop the devices. Idempotent."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
