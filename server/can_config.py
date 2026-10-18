"""CAN interface-spec and bitrate rules shared by everything that opens the bus.

On Linux, SocketCAN owns the bitrate (set with ``ip link``) and ignores what an
application asks for. Every other python-can interface -- PCAN, gs_usb, slcan,
Kvaser, ... -- runs at whatever bitrate the process that opens it chooses. The
scanner and the test nodes each open the interface, so they must agree on one
value, and it must be the bus's: a node that joins at the wrong speed floods
the bus with error frames. Cynitor therefore never guesses one; it has to be
given for every interface except SocketCAN.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Union

MAX_CLASSIC_CAN_BITRATE = 1_000_000
# Sanity bound for the CAN FD data phase; adapters top out between 8 and 12 Mbit/s.
MAX_FD_DATA_BITRATE = 12_000_000

BITRATE_ENV = "UAVCAN__CAN__BITRATE"
MTU_ENV = "UAVCAN__CAN__MTU"
CLASSIC_MTU = 8   # Cyphal/CAN payload per frame: Classic CAN
FD_MTU = 64       # ... and CAN FD

# python-can interfaces the hub can open as CAN FD, given a data bitrate.
# gs_usb is opened by Cynitor's own driver then (gs_usb_fd), as python-can's
# has no CAN FD mode; whether a candleLight adapter can run CAN FD is up to
# its firmware, which says so when it is opened. Others (canalystii, usb2can,
# ...) cannot; on Linux, adapters with a kernel driver do CAN FD as SocketCAN.
FD_INTERFACES = frozenset({"pcan", "kvaser", "vector", "ixxat", "gs_usb", "slcan", "virtual", "rawlog"})

# Interfaces that run the CAN FD data phase at some bitrates only. slcan's are
# the presets of the CANable 2.0 firmware (commands Y2 and Y5), the slcan
# firmware that does CAN FD, and all that python-can's slcan can set.
FD_DATA_BITRATES: dict[str, tuple[int, ...]] = {"slcan": (2_000_000, 5_000_000)}

# Simulated buses, an in-process bus with demo devices on it for trying Cynitor
# without hardware, on any OS (see simulation.py). Their rates are their own,
# as SocketCAN's are the kernel's: given ones are ignored. Only the bus-load
# estimate depends on them.
SIM_INTERFACE = "sim"
SIM_BUSES: dict[str, tuple[int, Optional[int]]] = {
    "classic": (1_000_000, None),
    "fd": (1_000_000, 4_000_000),
}

# Sample points of the CAN FD bit timing Cynitor works out itself for PCAN, in
# percent: what pycyphal uses there. candleLight adapters take the Linux
# kernel's (gs_usb_fd.sample_point).
FD_NOMINAL_SAMPLE_POINT = 80.0
FD_DATA_SAMPLE_POINT = 80.0

# A SocketCAN interface carries CAN FD frames once its MTU is that of
# struct canfd_frame (`ip link set can0 ... fd on`, or `mtu 72` for vcan).
_CANFD_MTU = 72
_SYSFS_NET = Path("/sys/class/net")

# PythonCANMedia insists on a bitrate even for SocketCAN, which ignores it.
_SOCKETCAN_IGNORED_BITRATE = 500_000

# Earlier docs spelled python-can specs as "pythoncan:pcan:PCAN_USBBUS1".
# pycyphal splits on the first colon, so it looked for an interface named
# "pythoncan" and failed. The prefix is accepted and dropped so those
# instructions keep working.
_LEGACY_PREFIX = "pythoncan:"


def normalize_can_iface(iface: str) -> str:
    """Turn user input into a pycyphal ``<interface>:<channel>`` spec.

    A bare name such as ``vcan0`` means SocketCAN, which is what the CLI was
    built around. Anything with a colon is already a spec (``gs_usb:0``,
    ``pcan:PCAN_USBBUS1``, ``slcan:COM5@115200``) and passes through.
    """
    value = iface.strip()
    if value.lower().startswith(_LEGACY_PREFIX):
        value = value[len(_LEGACY_PREFIX):]
    if ":" in value:
        return value
    return f"socketcan:{value}"


def is_explicit_spec(iface: str) -> bool:
    """Whether ``iface`` names its python-can interface rather than a bare SocketCAN name."""
    return ":" in iface


def is_socketcan(iface: str) -> bool:
    """Whether ``iface`` opens SocketCAN, where the kernel owns the bitrate."""
    return normalize_can_iface(iface).startswith("socketcan:")


def socketcan_device(iface: Optional[str]) -> str:
    """The kernel device name behind a SocketCAN ``iface`` (``socketcan:can0`` -> ``can0``)."""
    if iface is None:
        raise ValueError("No CAN interface")
    spec = normalize_can_iface(iface)
    if not spec.startswith("socketcan:"):
        raise ValueError(f"{iface!r} is not a SocketCAN interface")
    return spec[len("socketcan:"):]


def _interface_name(iface: str) -> str:
    """The python-can interface that opens ``iface`` (``pcan:PCAN_USBBUS1`` -> ``pcan``)."""
    return normalize_can_iface(iface).split(":", 1)[0]


# The python-can interfaces a request to the API may open: the adapters the
# README names, the simulated bus and python-can's in-process virtual one.
# Others (python-can has network and serial back-ends too) only with the
# server's --allow-any-interface; --can on the command line opens any.
API_INTERFACES = frozenset({"socketcan", "pcan", "kvaser", "vector", "ixxat", "gs_usb", "slcan", SIM_INTERFACE,
                            "virtual"})
# An slcan adapter's serial port, and its baud rate: not any terminal device.
_SLCAN_PORT = re.compile(r"(COM\d+|/dev/tty(ACM|USB)\d+|/dev/serial/by-id/[\w.:+-]+|/dev/(tty|cu)\.usb[\w.-]+)(@\d+)?")


def api_interface_error(iface: str) -> Optional[str]:
    """Why a request may not open ``iface``, or None if it may."""
    name = _interface_name(iface)
    if name not in API_INTERFACES:
        return (f"Interface type {name!r} is not one Cynitor opens on request; start the server with "
                "--allow-any-interface to allow any python-can interface")
    channel = normalize_can_iface(iface).split(":", 1)[1]
    if name == "slcan" and not _SLCAN_PORT.fullmatch(channel):
        return f"{channel!r} is not a serial port name such as COM5, /dev/ttyACM0 or /dev/serial/by-id/..."
    return None


def is_simulated(iface: str) -> bool:
    """Whether ``iface`` names a simulated bus (``sim:classic``, ``sim:fd``)."""
    return _interface_name(iface) == SIM_INTERFACE


def simulated_rates(iface: str) -> tuple[int, Optional[int]]:
    """(bitrate, CAN FD data bitrate or None) of simulated bus ``iface``; ValueError for no such bus."""
    name = normalize_can_iface(iface).split(":", 1)[1]
    try:
        return SIM_BUSES[name]
    except KeyError:
        known = " or ".join(f"{SIM_INTERFACE}:{bus}" for bus in SIM_BUSES)
        raise ValueError(f"There is no simulated bus {name!r}; there are {known}") from None


def validate_bitrate(bitrate: object) -> int:
    """Return ``bitrate`` as an int, or raise ValueError if it is not a usable classic-CAN bitrate."""
    # bool is an int subclass; True would otherwise pass as 1 bit/s.
    if isinstance(bitrate, bool) or not isinstance(bitrate, int):
        raise ValueError(f"CAN bitrate must be an integer, got {bitrate!r}")
    if not 0 < bitrate <= MAX_CLASSIC_CAN_BITRATE:
        raise ValueError(
            f"CAN bitrate must be between 1 and {MAX_CLASSIC_CAN_BITRATE} bit/s, got {bitrate}"
        )
    return bitrate


def validate_data_bitrate(data_bitrate: object) -> int:
    """Return ``data_bitrate`` as an int, or raise ValueError if it is not a usable CAN FD data rate."""
    if isinstance(data_bitrate, bool) or not isinstance(data_bitrate, int):
        raise ValueError(f"CAN FD data bitrate must be an integer, got {data_bitrate!r}")
    if not 0 < data_bitrate <= MAX_FD_DATA_BITRATE:
        raise ValueError(
            f"CAN FD data bitrate must be between 1 and {MAX_FD_DATA_BITRATE} bit/s, got {data_bitrate}"
        )
    return data_bitrate


def resolve_data_bitrate(iface: str, data_bitrate: Optional[object]) -> Optional[int]:
    """The CAN FD data bitrate to open ``iface`` with, or None for Classic CAN.

    SocketCAN always gets None: the kernel owns its rates, and whether it runs
    CAN FD follows the interface's own setting (see socketcan_supports_fd).
    A simulated bus has its own, whatever is given. Raises ValueError for an
    adapter that cannot run CAN FD, or not at that data bitrate.
    """
    if is_simulated(iface):
        if data_bitrate is not None:
            validate_data_bitrate(data_bitrate)
        return simulated_rates(iface)[1]
    if data_bitrate is None or is_socketcan(iface):
        return None
    interface = _interface_name(iface)
    if interface not in FD_INTERFACES:
        raise ValueError(
            f"{interface} adapters cannot run CAN FD through Cynitor; leave the data bitrate "
            f"out for Classic CAN (on Linux, use the adapter as SocketCAN for CAN FD)"
        )
    data_bitrate = validate_data_bitrate(data_bitrate)
    offered = fd_data_bitrates(iface)
    if offered is not None and data_bitrate not in offered:
        raise ValueError(
            f"{interface} adapters run the CAN FD data phase at "
            f"{' or '.join(str(rate) for rate in offered)} bit/s only, not {data_bitrate}"
        )
    return data_bitrate


def fd_data_bitrates(iface: str) -> Optional[tuple[int, ...]]:
    """The only CAN FD data bitrates ``iface`` can run, or None if it is not so limited."""
    return FD_DATA_BITRATES.get(_interface_name(iface))


def socketcan_supports_fd(device: str, sysfs: Path = _SYSFS_NET) -> bool:
    """Whether SocketCAN ``device`` is set up for CAN FD frames."""
    try:
        return int((sysfs / device / "mtu").read_text(encoding="utf-8")) == _CANFD_MTU
    except (OSError, ValueError):
        return False


def supports_fd(iface: str) -> bool:
    """Whether a session on ``iface`` can run CAN FD.

    For SocketCAN, whether the interface is set up for it; for anything else,
    whether Cynitor can open the adapter as CAN FD when given a data bitrate.
    A candleLight adapter also needs firmware that does CAN FD, which only the
    device can tell (can_discovery asks it).
    """
    if is_socketcan(iface):
        return socketcan_supports_fd(socketcan_device(iface))
    if is_simulated(iface):
        return simulated_rates(iface)[1] is not None
    return _interface_name(iface) in FD_INTERFACES


def resolve_bitrate(iface: str, bitrate: Optional[object]) -> Optional[int]:
    """The bitrate to open ``iface`` at: validated, and required unless it is SocketCAN.

    Returns None only for SocketCAN given no bitrate. A simulated bus has its
    own, whatever is given. Raises ValueError when any other interface is
    given none, since there is no safe default.
    """
    if is_simulated(iface):
        if bitrate is not None:
            validate_bitrate(bitrate)
        return simulated_rates(iface)[0]
    if bitrate is None:
        if is_socketcan(iface):
            return None
        raise ValueError(
            f"A bitrate is required for {normalize_can_iface(iface)}: unlike SocketCAN, "
            f"the adapter runs at whatever speed Cynitor opens it with, so it must "
            f"match the bus (for example 500000)"
        )
    return validate_bitrate(bitrate)


def bitrate_env_value(bitrate: int, data_bitrate: Optional[int] = None) -> str:
    """Format the bitrates for the ``uavcan.can.bitrate`` register.

    The register holds ``[arbitration, data]``. A single number is read as
    ``[n, 0]``, which pycyphal takes for CAN FD with a zero-rate data phase,
    so classic CAN must repeat the value.
    """
    return f"{bitrate} {data_bitrate or bitrate}"


def bitrates_from_env() -> Optional[tuple[int, int]]:
    """The (arbitration, data) bitrates that ``prepare_runtime`` published, if any."""
    parts = os.environ.get(BITRATE_ENV, "").split()
    try:
        return (int(parts[0]), int(parts[-1])) if parts else None
    except ValueError:
        return None


def bitrate_from_env() -> Optional[int]:
    """The arbitration bitrate that ``prepare_runtime`` published, if any."""
    rates = bitrates_from_env()
    return rates[0] if rates else None


def media_bitrate(iface: str) -> Union[int, tuple[int, int]]:
    """The bitrate to hand PythonCANMedia for ``iface``, as published by ``prepare_runtime``.

    One number for Classic CAN; (arbitration, data) when the data phase differs.
    """
    rates = bitrates_from_env()
    if rates is not None:
        return rates[0] if rates[0] == rates[1] else rates
    if is_socketcan(iface):
        return _SOCKETCAN_IGNORED_BITRATE
    raise RuntimeError(
        f"No bitrate published for {iface}; prepare_runtime must run before the bus is opened"
    )


def media_mtu() -> int:
    """The MTU that ``prepare_runtime`` published: FD_MTU for CAN FD, else CLASSIC_MTU."""
    try:
        return int(os.environ.get(MTU_ENV, CLASSIC_MTU))
    except ValueError:
        return CLASSIC_MTU
