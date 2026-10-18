"""Bus load on SocketCAN, as can-utils' canbusload measures it (behind the CAN hub, can_hub.HubBusLoad).

Used by the session (main.py) for /api/status and the dashboard's sidebar.
"""

import asyncio
import logging
import re
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

def _get_can_bitrates(iface: str) -> tuple[int, Optional[int]]:
    """Bitrate and CAN FD data bitrate of a SocketCAN interface.

    The bitrate defaults to 500000 where there is none (vcan); the data
    bitrate is None unless the interface is set up for CAN FD.
    """
    bitrate, dbitrate = 500000, None
    try:
        result = subprocess.run(
            ["ip", "-details", "link", "show", iface],
            check=False, capture_output=True, text=True,
        )
        if result.returncode == 0:
            match = re.search(r"\bbitrate\s+(\d+)", result.stdout)
            if match:
                bitrate = int(match.group(1))
            match = re.search(r"\bdbitrate\s+(\d+)", result.stdout)
            if match:
                dbitrate = int(match.group(1))
    except Exception:
        pass
    return bitrate, dbitrate


def _canbusload_counts_can_fd() -> bool:
    """Whether the canbusload on PATH counts CAN FD frames.

    Releases before mid-2021 (Ubuntu 22.04 ships 2020.11) accept a data
    bitrate but count Classic CAN frames only; their usage text gives the
    interface as ``<ifname>@<bitrate>``, later ones as ``<ifname>@<bitrate>[,<dbitrate>]``.
    """
    try:
        result = subprocess.run(["canbusload"], check=False, capture_output=True, text=True, timeout=5)
        return "dbitrate" in result.stdout + result.stderr
    except Exception:
        return False


class BusLoadMonitor:
    """Runs canbusload as a subprocess and exposes the latest utilization %.

    `canbusload` ships with Linux `can-utils`. If it is not on PATH (any non-
    Linux OS, or a minimal Linux install) the monitor becomes a permanent
    no-op: utilization stays None, unknown rather than an idle 0 %, and
    `is_alive` reports True so the health watchdog in `_register_loop` does
    not trip a disconnect.
    """

    def __init__(self, iface: str) -> None:
        self._iface = iface
        self._bitrate, self._dbitrate = _get_can_bitrates(iface)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self._disabled_why: Optional[str] = None
        if shutil.which("canbusload") is None:
            self._disabled_why = "'canbusload' not on PATH (install can-utils on Linux for bus-load metrics)"
        elif self._dbitrate and not _canbusload_counts_can_fd():
            # It would read CAN FD traffic as an idle bus: the load is unknown instead.
            self._disabled_why = ("this canbusload counts Classic CAN frames only, and the bus is CAN FD "
                                  "(a newer can-utils counts both)")
        self._disabled = self._disabled_why is not None
        self.utilization: Optional[float] = None if self._disabled else 0.0

    async def start(self) -> None:
        if self._disabled:
            logger.info("BusLoadMonitor disabled: %s", self._disabled_why)
            return
        # canbusload counts CAN FD frames' data phase at ",<dbitrate>".
        rates = f"{self._bitrate},{self._dbitrate}" if self._dbitrate else str(self._bitrate)
        self._proc = await asyncio.create_subprocess_exec(
            "canbusload", f"{self._iface}@{rates}", "-b",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._task = asyncio.create_task(self._read_loop())
        logger.info("BusLoadMonitor started on %s@%s", self._iface, rates)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # canbusload ends by itself when its interface goes (a USB adapter
        # unplugged): terminating it then raises, and the session's teardown
        # must not stop here.
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except ProcessLookupError:
                pass
            except asyncio.TimeoutError:
                self._proc.kill()
        self._proc = None
        self.utilization = None if self._disabled else 0.0
        logger.info("BusLoadMonitor stopped")

    @property
    def is_alive(self) -> bool:
        if self._disabled:
            return True
        return self._proc is not None and self._proc.returncode is None

    async def _read_loop(self) -> None:
        try:
            assert self._proc and self._proc.stdout
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                match = re.search(r"(\d+)%", text)
                if match:
                    self.utilization = float(match.group(1))
        except asyncio.CancelledError:
            raise
