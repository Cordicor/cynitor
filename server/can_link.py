"""SocketCAN interfaces as the kernel shows them: which there are, and how each controller is doing.

Linux only (sysfs and iproute2); elsewhere there are none, and no diagnostics.
Used by the session (main.py) and the CAN API (api_can.py).
"""

import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

IS_LINUX = sys.platform.startswith("linux")

# The controller's state in `ip -details link show`: "can state ERROR-ACTIVE",
# or with the controller's modes between, as "can <FD> state ERROR-WARNING".
_CAN_STATE = re.compile(r"\bcan\s+(?:<[^>]*>\s+)?state\s+(\S+)")


def discover_can_interfaces() -> list[str]:
    interfaces: set[str] = set()

    def maybe_add_interface(name: str) -> None:
        interface_name = name.split("@", 1)[0].strip()
        if not interface_name:
            return

        type_file = Path("/sys/class/net") / interface_name / "type"
        try:
            if type_file.exists() and type_file.read_text(encoding="utf-8").strip() == "280":
                interfaces.add(interface_name)
                return
        except Exception:
            pass

        if re.match(r"^(v?can\d+|slcan\d+)$", interface_name):
            interfaces.add(interface_name)

    try:
        result = subprocess.run(
            ["ip", "-o", "link", "show"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                match = re.match(r"^\d+:\s+([^:]+):", line)
                if match:
                    maybe_add_interface(match.group(1))
    except Exception:
        pass

    try:
        for iface in Path("/sys/class/net").iterdir():
            maybe_add_interface(iface.name)
    except Exception:
        pass

    return sorted(interfaces)


def get_can_link_diagnostics(iface: str) -> dict:
    """Best-effort controller/bus diagnostics for a CAN interface.

    Parses ``ip -details -statistics link show <iface>`` plus sysfs operstate.
    SocketCAN-specific (Linux). All fields are optional — virtual interfaces
    (vcan) expose no CAN controller state, so most values come back ``None``.
    Never raises; returns whatever could be read.
    """
    result: dict = {
        "operstate": None, "state": None, "bitrate": None, "dbitrate": None,
        "berr_tx": None, "berr_rx": None, "restart_ms": None,
        "restarts": None, "bus_errors": None, "arbitration_lost": None,
        "error_warning": None, "error_passive": None, "bus_off": None,
    }
    if not IS_LINUX:
        return result

    try:
        result["operstate"] = (
            (Path("/sys/class/net") / iface / "operstate").read_text(encoding="utf-8").strip()
        )
    except Exception:
        pass

    try:
        proc = subprocess.run(
            ["ip", "-details", "-statistics", "link", "show", iface],
            check=False, capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return result
    if proc.returncode != 0:
        return result
    out = proc.stdout

    def _int(pattern: str, group: int = 1):
        m = re.search(pattern, out)
        return int(m.group(group)) if m else None

    m = _CAN_STATE.search(out)
    if m:
        result["state"] = m.group(1)
    result["bitrate"] = _int(r"\bbitrate\s+(\d+)")
    result["dbitrate"] = _int(r"\bdbitrate\s+(\d+)")
    result["restart_ms"] = _int(r"restart-ms\s+(\d+)")
    berr = re.search(r"berr-counter\s+tx\s+(\d+)\s+rx\s+(\d+)", out)
    if berr:
        result["berr_tx"], result["berr_rx"] = int(berr.group(1)), int(berr.group(2))

    # CAN error-state-change counters appear as a labelled header row followed
    # by the values, only with -statistics and only on real controllers.
    counters = re.search(
        r"re-started\s+bus-errors\s+arbit-lost\s+error-warn\s+error-pass\s+bus-off"
        r"\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)",
        out,
    )
    if counters:
        (result["restarts"], result["bus_errors"], result["arbitration_lost"],
         result["error_warning"], result["error_passive"], result["bus_off"]) = (
            int(counters.group(i)) for i in range(1, 7)
        )
    return result


def rx_overflows(device: str) -> Optional[int]:
    """Frames the controller or its driver lost, the receive buffer being full: ``rx_over_errors``.

    A count of overflows rather than of frames, for most drivers (gs_usb counts
    one per overflow notice). None where there is no such counter, or off Linux.
    """
    if not IS_LINUX:
        return None
    try:
        return int((Path("/sys/class/net") / device / "statistics" / "rx_over_errors").read_text().strip())
    except (OSError, ValueError):
        return None

