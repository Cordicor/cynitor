"""Find the CAN adapters the dashboard can offer in its interface list.

SocketCAN interfaces come from the kernel (see main.discover_can_interfaces).
Everything else is found here, per kind of adapter:

- vendor drivers python-can can enumerate: PEAK, Kvaser, Vector, IXXAT;
- candleLight adapters such as the CANable (gs_usb), by USB scan, each asked
  whether its firmware does CAN FD;
- slcan adapters, by the USB IDs of their serial ports.

The simulated buses are not found but always there (simulated_adapters).

On Linux the kernel drives candleLight and slcan adapters itself (as can0, or
slcan0 via slcand), so they are only scanned for elsewhere; opening them over
USB or the tty would fight the kernel for the device.

Every probe is best effort: a missing driver or library means no entries from
that probe, never an error.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable, Iterable, Iterator, Optional

from can_config import SIM_BUSES, SIM_INTERFACE, fd_data_bitrates, supports_fd
from gs_usb_fd import read_capability

logger = logging.getLogger(__name__)

_VENDOR_LABELS = {
    "pcan": "PEAK",
    "kvaser": "Kvaser",
    "vector": "Vector",
    "ixxat": "IXXAT",
}

# USB vendor/product IDs of serial-port adapters that speak slcan, with their
# label and whether their firmware does CAN FD. Only known ones are listed:
# offering every COM port would include debug probes and other serial devices
# that are not CAN adapters at all.
_SLCAN_USB_IDS = {
    (0x16D0, 0x117E): ("CANable (slcan)", True),  # CANable 2.0 firmware
}


@dataclass(frozen=True)
class Adapter:
    interface: str        # what --can and POST /api/can/connect take
    label: str            # what the dropdown shows
    needs_bitrate: bool   # False only for SocketCAN, where the kernel owns it
    # Whether a session can run CAN FD: for SocketCAN, whether the interface is
    # set up for it; otherwise whether Cynitor can open the adapter as CAN FD.
    supports_fd: bool = False
    # The only CAN FD data bitrates it can run, where it has presets (slcan).
    data_bitrates: Optional[tuple[int, ...]] = None

    def as_dict(self) -> dict:
        return asdict(self)


@contextlib.contextmanager
def _quiet_python_can() -> Iterator[None]:
    """Hold back python-can's import-time warnings about absent vendor libraries.

    "Kvaser canlib is unavailable" and the like are expected on any machine
    without that vendor's driver, and would otherwise land in the dashboard's
    log panel.
    """
    can_logger = logging.getLogger("can")
    previous = can_logger.level
    can_logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        can_logger.setLevel(previous)


def _vendor_adapters() -> list[Adapter]:
    import can

    found = []
    for interface, vendor in _VENDOR_LABELS.items():
        try:
            with _quiet_python_can():
                configs = can.detect_available_configs(interfaces=[interface])
        except Exception as exc:
            logger.debug("Adapter discovery: %s probe failed: %s", interface, exc)
            continue
        for config in configs:
            channel = config.get("channel")
            if channel is None:
                continue
            spec = f"{interface}:{channel}"
            found.append(Adapter(spec, f"{vendor} {channel}", True, supports_fd(spec)))
    return found


def _gs_usb_adapters() -> list[Adapter]:
    try:
        from startup_setup import ensure_libusb_on_path
        ensure_libusb_on_path()
        from gs_usb.gs_usb import GsUsb
        devices = GsUsb.scan()
    except Exception as exc:
        logger.debug("Adapter discovery: gs_usb scan failed: %s", exc)
        return []
    found = []
    for index, device in enumerate(devices):
        serial, fd = _probe_gs_usb(device.gs_usb)
        # Short enough for the sidebar; the last digits tell two adapters apart.
        label = f"CANable {index}" + (f" …{serial[-4:]}" if serial else "")
        found.append(Adapter(f"gs_usb:{index}", label, True, fd))
    return found


def _probe_gs_usb(device) -> tuple[Optional[str], bool]:
    """A gs_usb device's serial number, and whether its firmware does CAN FD.

    Reading either opens the device, which fails while another program holds
    it; the adapter is still worth listing then. It is let go at once, as
    WinUSB lets one handle at a time open it and connecting needs one.
    """
    import usb.util
    try:
        try:
            serial = device.serial_number
        except Exception:
            serial = None
        try:
            fd = read_capability(device).can_fd
        except Exception as exc:
            logger.debug("Adapter discovery: gs_usb features unread: %s", exc)
            fd = False
        return serial, fd
    finally:
        usb.util.dispose_resources(device)


def _slcan_adapters() -> list[Adapter]:
    try:
        from serial.tools import list_ports
        ports = list_ports.comports()
    except Exception as exc:
        logger.debug("Adapter discovery: serial port scan failed: %s", exc)
        return []
    found = []
    for port in sorted(ports, key=lambda p: p.device):
        known = _SLCAN_USB_IDS.get((port.vid, port.pid))
        if known:
            name, fd = known
            spec = f"slcan:{port.device}"
            found.append(Adapter(spec, f"{name} {port.device}", True, fd,
                                 fd_data_bitrates(spec) if fd else None))
    return found


_SIMULATED_LABELS = {
    "classic": "Simulated bus (no hardware)",
    "fd": "Simulated bus, CAN FD (no hardware)",
}


def simulated_adapters() -> list[Adapter]:
    """The simulated buses (can_config.SIM_BUSES): there on every OS, and with rates of their own."""
    return [Adapter(spec, _SIMULATED_LABELS[name], False, supports_fd(spec))
            for name in SIM_BUSES for spec in [f"{SIM_INTERFACE}:{name}"]]


def discover_adapters(socketcan_names: Iterable[str],
                      platform: str = sys.platform) -> list[Adapter]:
    """Every adapter found, SocketCAN interfaces first."""
    adapters = [Adapter(name, f"{name} (SocketCAN)", False, supports_fd(name)) for name in socketcan_names]
    adapters += _vendor_adapters()
    if not platform.startswith("linux"):
        adapters += _gs_usb_adapters()
        adapters += _slcan_adapters()
    return adapters


class AdapterCatalog:
    """The last discovery result, refreshed at most every ``max_age`` seconds.

    The dashboard polls /api/status every few seconds; discovery loads vendor
    libraries and walks USB, so it is not repeated on every poll.
    """

    def __init__(self, discover: Callable[[], list[Adapter]], max_age: float = 10.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._discover = discover
        self._max_age = max_age
        self._clock = clock
        self._lock = threading.Lock()
        self._adapters: list[Adapter] = []
        self._taken_at: Optional[float] = None

    def get(self, refresh: bool = False, rescan: bool = True) -> list[Adapter]:
        """The adapter list; rescanned if stale or ``refresh``, unless ``rescan`` is False.

        Pass ``rescan=False`` while connected: probing a USB adapter that the
        session holds cannot tell anything new and only risks disturbing it.
        """
        with self._lock:
            stale = self._taken_at is None or self._clock() - self._taken_at >= self._max_age
            if rescan and (refresh or stale):
                try:
                    self._adapters = self._discover()
                except Exception as exc:
                    logger.warning("Adapter discovery failed: %s", exc)
                self._taken_at = self._clock()
            return list(self._adapters)
