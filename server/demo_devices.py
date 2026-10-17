"""Demo Cyphal devices, for trying Cynitor without hardware.

The simulated bus runs them (see simulation.py), and so does
tools/demo_nodes.py, on a virtual CAN interface or an adapter.

Node 50, "demo.sensor":
  - publishes uavcan.si.sample.temperature.Scalar.1.0 on subject 1620 at 10 Hz,
    named in its registers, so Cynitor decodes and can plot it;
  - publishes uavcan.si.unit.velocity.Vector3.1.0 on subject 1700 at 5 Hz with
    no register naming it, so Cynitor cannot decode it until you set its type
    (Subjects view: click it, and pick from the types its messages fit);
  - publishes uavcan.primitive.String.1.0 on subject 1800, its state as text
    every 2 s, named in its registers: add it to the log panel with its +;
  - publishes a uavcan.diagnostic.Record every 3 s, going through every
    severity from DEBUG to CRITICAL (the log panel);
  - serves uavcan.node.ExecuteCommand: Restart really restarts it (its uptime
    starts over), Factory reset only answers success, and Update firmware
    acts like a bootloader: it restarts in SOFTWARE_UPDATE mode, reads the
    file from Cynitor with uavcan.file.Read (slowly, so the progress shows),
    and comes back with its software version's minor number one higher.

With ``conflict``, a second node starts on the same node-ID after 10 s, which
Cynitor should flag as a node-ID conflict.

Every device takes ``make_transport(node_id)``: a new pycyphal transport on the
bus it is to run on, for that node-ID (None: anonymous). They need the compiled
DSDL, imported when this module is.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from typing import Callable, Optional

import pycyphal.application
import pycyphal.transport
import uavcan.diagnostic
import uavcan.file
import uavcan.node
import uavcan.primitive
import uavcan.si.sample.temperature
import uavcan.si.unit.velocity

logger = logging.getLogger(__name__)

MakeTransport = Callable[[Optional[int]], pycyphal.transport.Transport]

DEFAULT_NODE_ID = 50
TEMPERATURE_SUBJECT_ID = 1620
UNNAMED_SUBJECT_ID = 1700
STATUS_SUBJECT_ID = 1800
STATES = ["idle", "heating", "holding", "cooling"]
Severity = uavcan.diagnostic.Severity_1_0
# (severity, message) in turn, one every 3 s: every level the log panel shows.
DIAGNOSTICS = [
    (Severity.INFO, "temperature {celsius:.1f} C"),
    (Severity.DEBUG, "adc raw {adc}"),
    (Severity.NOTICE, "calibration table loaded"),
    (Severity.INFO, "temperature {celsius:.1f} C"),
    (Severity.WARNING, "supply voltage low: 4.6 V"),
    (Severity.INFO, "temperature {celsius:.1f} C"),
    (Severity.ERROR, "I2C timeout on bus 1, retrying"),
    (Severity.INFO, "I2C bus 1 recovered"),
    (Severity.CRITICAL, "heater overcurrent, heater off for 5 s"),
]
Command = uavcan.node.ExecuteCommand_1_3


def make_node(name: str, make_transport: MakeTransport, node_id: int, minor: int = 0) -> pycyphal.application.Node:
    """A started node named ``name``, its publishers' subject-IDs set in its registers."""
    registers = {
        "UAVCAN__PUB__TEMPERATURE__ID": str(TEMPERATURE_SUBJECT_ID),
        "UAVCAN__PUB__STATUS__ID": str(STATUS_SUBJECT_ID),
    }
    # The same unique-ID every start, as a real device has: a fresh one each time
    # made every connect leave the previous one behind as an offline device.
    unique_id = hashlib.md5(f"cynitor demo {name} {node_id}".encode()).digest()
    info = uavcan.node.GetInfo_1_0.Response(name=name, software_version=uavcan.node.Version_1_0(major=1, minor=minor),
                                            unique_id=unique_id)
    node = pycyphal.application.make_node(
        info, pycyphal.application.make_registry(None, environment_variables=registers),
        transport=make_transport(node_id),
    )
    node.start()
    return node


async def run_bootloader(make_transport: MakeTransport, node_id: int, minor: int,
                         server_node_id: int, path: str) -> bool:
    """Read the firmware file the way a bootloader does; whether it arrived whole."""
    node = make_node("demo.sensor", make_transport, node_id, minor)
    client = None
    try:
        node.heartbeat_publisher.mode = uavcan.node.Mode_1_0.SOFTWARE_UPDATE
        client = node.make_client(uavcan.file.Read_1_1, server_node_id)
        logger.info("demo.sensor: bootloader reading %s from node %d", path, server_node_id)
        size = 0
        while True:
            result = await client.call(uavcan.file.Read_1_1.Request(offset=size, path=uavcan.file.Path_2_0(path)))
            if result is None or result[0].error.value != 0:
                logger.warning("demo.sensor: bootloader read failed at byte %d: %s", size, result)
                return False
            chunk = len(result[0].data.value)
            size += chunk
            if chunk < 256:
                logger.info("demo.sensor: bootloader read %d bytes; starting the new firmware", size)
                return True
            await asyncio.sleep(0.02)  # slow enough to watch
    finally:
        if client is not None:
            client.close()
        node.close()


async def run_sensor(make_transport: MakeTransport, node_id: int = DEFAULT_NODE_ID) -> None:
    """Node ``node_id``, "demo.sensor", until cancelled."""
    minor = 0
    update = None  # (server node-ID, file) once an update is asked for
    while True:  # one pass per (re)start
        if update is not None:
            if await run_bootloader(make_transport, node_id, minor, *update):
                minor += 1
            update = None
        node = make_node("demo.sensor", make_transport, node_id, minor)
        try:
            update = await _sense(node, node_id)
        finally:
            node.close()  # and straight back: well within Cynitor's 3 s offline threshold


async def _sense(node: pycyphal.application.Node, node_id: int) -> Optional[tuple[int, str]]:
    """Publish and serve until asked to restart; the update to install first, if one was asked for."""
    temperature = node.make_publisher(uavcan.si.sample.temperature.Scalar_1_0, "temperature")
    diagnostics = node.make_publisher(uavcan.diagnostic.Record_1_1)
    status = node.make_publisher(uavcan.primitive.String_1_0, "status")
    # Straight through the presentation layer: no register names its type.
    velocity = node.presentation.make_publisher(uavcan.si.unit.velocity.Vector3_1_0, UNNAMED_SUBJECT_ID)
    restart = asyncio.Event()
    update = None

    async def on_command(request: Command.Request, meta) -> Command.Response:
        nonlocal update
        if request.command == Command.Request.COMMAND_RESTART:
            asyncio.get_running_loop().call_later(0.2, restart.set)  # answer first, then restart
            return Command.Response(status=Command.Response.STATUS_SUCCESS)
        if request.command == Command.Request.COMMAND_FACTORY_RESET:
            return Command.Response(status=Command.Response.STATUS_SUCCESS)
        if request.command == Command.Request.COMMAND_BEGIN_SOFTWARE_UPDATE:
            update = (meta.client_node_id, request.parameter.tobytes().decode())
            asyncio.get_running_loop().call_later(0.2, restart.set)
            return Command.Response(status=Command.Response.STATUS_SUCCESS)
        return Command.Response(status=Command.Response.STATUS_BAD_COMMAND)

    node.get_server(Command).serve_in_background(on_command)
    logger.info("demo.sensor: node-ID %d", node_id)
    tick = 0
    while not restart.is_set():
        kelvin = 293.15 + 5 * math.sin(time.monotonic() / 5)
        await temperature.publish(uavcan.si.sample.temperature.Scalar_1_0(kelvin=kelvin))
        if tick % 2 == 0:
            t = time.monotonic()
            await velocity.publish(uavcan.si.unit.velocity.Vector3_1_0(
                meter_per_second=[math.cos(t), math.sin(t), 0.5]))
        if tick % 20 == 0:
            state = STATES[tick // 20 % len(STATES)]
            await status.publish(uavcan.primitive.String_1_0(f"state: {state}, {kelvin - 273.15:.1f} C"))
        if tick % 30 == 0:
            severity, text = DIAGNOSTICS[tick // 30 % len(DIAGNOSTICS)]
            await diagnostics.publish(uavcan.diagnostic.Record_1_1(
                severity=Severity(severity),
                text=text.format(celsius=kelvin - 273.15, adc=int(kelvin * 10) % 4096),
            ))
        tick += 1
        await asyncio.sleep(0.1)
    logger.info("demo.sensor: restarting on request%s", " into the bootloader" if update else "")
    return update


async def run_twin(make_transport: MakeTransport, node_id: int = DEFAULT_NODE_ID, delay: float = 10.0) -> None:
    """A second node on ``node_id``, from ``delay`` seconds on, until cancelled."""
    await asyncio.sleep(delay)  # a later start, so its uptime differs
    node = make_node("demo.twin", make_transport, node_id)
    try:
        logger.info("demo.twin: also on node-ID %d", node_id)
        await asyncio.Event().wait()
    finally:
        node.close()


async def run(make_transport: MakeTransport, node_id: int = DEFAULT_NODE_ID, conflict: bool = False) -> None:
    """The demo devices, until cancelled."""
    devices = [run_sensor(make_transport, node_id)]
    if conflict:
        devices.append(run_twin(make_transport, node_id))
    await asyncio.gather(*devices)
