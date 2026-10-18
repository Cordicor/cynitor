#!/usr/bin/env python3
"""Demo Cyphal nodes for trying Cynitor without hardware.

    python3 tools/demo_nodes.py [--iface vcan0] [--fd] [--conflict] [--node-id 50]

On any OS, Cynitor's own simulated bus runs them with nothing to set up: pick
"Simulated bus" in the dashboard, or start it with --can sim:classic. This
runs them on a bus of your own instead: on Linux, next to Cynitor on the same
virtual CAN interface,

    sudo ip link add dev vcan0 type vcan && sudo ip link set vcan0 up

It needs the compiled DSDL, which Cynitor makes on its first start (or:
python3 server/startup_setup.py --recompile).

--conflict starts a second node on the same node-ID after 10 s, which Cynitor
should flag as a node-ID conflict. --fd runs Cyphal/CAN FD; the interface must
be set up for it (e.g. a vcan with mtu 72). --node-id puts the node on another
node-ID: a second demo on 51 publishes the same subjects, so each has two
publishers, as redundant sensors do.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "python_compiled_messages")]
try:
    import uavcan.node  # noqa: F401  (compiled DSDL)
except ImportError:
    sys.exit("Compiled DSDL not found: start Cynitor once, or run python3 server/startup_setup.py --recompile")

import pycyphal.application  # noqa: E402

import demo_devices  # noqa: E402  (server/, which says what the nodes do)


def transport_factory(iface: str, fd: bool) -> demo_devices.MakeTransport:
    """Transports on ``iface`` (a SocketCAN name, or a pycyphal spec), as pycyphal reads its registers."""
    iface = iface if ":" in iface else f"socketcan:{iface}"

    def make_transport(node_id):
        registers = {"UAVCAN__CAN__IFACE": iface, "UAVCAN__CAN__MTU": "64" if fd else "8"}
        if node_id is not None:
            registers["UAVCAN__NODE__ID"] = str(node_id)
        return pycyphal.application.make_transport(
            pycyphal.application.make_registry(None, environment_variables=registers))
    return make_transport


def _socketcan_mtu(iface: str):
    """The MTU of SocketCAN interface ``iface`` (72 for CAN FD, 16 for Classic), or None off Linux or if absent."""
    try:
        return int(Path(f"/sys/class/net/{iface}/mtu").read_text())
    except (OSError, ValueError):
        return None


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, epilog=demo_devices.__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--iface", default="vcan0", help="SocketCAN name, or a pycyphal spec (default vcan0)")
    parser.add_argument("--fd", action="store_true",
                        help="run Cyphal/CAN FD (MTU 64); on a SocketCAN interface set up for CAN FD it does anyway")
    parser.add_argument("--conflict", action="store_true", help="add a second node on the same node-ID")
    parser.add_argument("--node-id", type=int, default=demo_devices.DEFAULT_NODE_ID, choices=range(1, 126),
                        metavar="1..125", help=f"the node-ID to run on (default {demo_devices.DEFAULT_NODE_ID})")
    args = parser.parse_args()
    # Cynitor runs CAN FD on a CAN FD interface (a new vcan is one on current
    # kernels): a Classic demo there would hear none of its requests.
    if not args.fd and ":" not in args.iface and _socketcan_mtu(args.iface) == 72:
        args.fd = True
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("pycyphal").setLevel(logging.WARNING)
    print(f"Demo nodes on {args.iface}{' (CAN FD)' if args.fd else ''}", flush=True)
    await demo_devices.run(transport_factory(args.iface, args.fd), args.node_id, args.conflict)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
