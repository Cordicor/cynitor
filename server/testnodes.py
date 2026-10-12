"""Test nodes: Cyphal nodes Cynitor runs on the bus, made in the dashboard for testing.

A test node is a pycyphal node with the name, node-ID, health and mode it is
given, publishing subjects of any compiled message type at a set rate, each
field a constant or a waveform. Its registers name its publishers' types, as
firmware's do, so Cynitor and other tools decode them at once. Definitions are
kept in the data folder (test_nodes.json). None starts by itself, and all stop
when CAN disconnects.

On the simulated bus that is all. On a real bus a test node can move real
things (an actuator taking its setpoints from a test publisher), so there
starting one must be confirmed, its node-ID must be free, and publish rates
are capped, per publisher and in total.

A publisher's ``value`` is its message in pycyphal's builtin form: a dict for
a composite, a list for an array, a string for a byte array. Any number in it
may instead be a waveform, {"$wave": "sine" | "ramp" | "square" | "noise" |
"count", "min": a, "max": b, "period_s": p}: sine, ramp and square run from a
to b and back over p seconds, noise is random in [a, b], and count adds one
per message, wrapping from b to a. value_schema describes a type for the
dashboard's editor; check_value checks a value against it, and evaluate gives
the builtin form at a moment, each number rounded and clamped to its field.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

MAX_TEST_NODES = 16
MAX_PUBLISHERS = 8
MIN_RATE_HZ = 0.01
MAX_RATE_HZ = 100.0
# Of every running test node together: a 1 Mbit/s bus carries some 8000
# frames a second, so this leaves most of it to the bus's own nodes.
MAX_TOTAL_RATE_HZ = 1000.0
# Node-IDs 0 and 126-127 are left alone: Cyphal reserves the top two for
# diagnostic tools, and 0 is easily taken for "none".
MIN_NODE_ID, MAX_NODE_ID = 1, 125
# Where Cynitor looks first for a node-ID for a test node that sets none: from
# here up, then below.
AUTO_NODE_ID_FROM = 100
MAX_SUBJECT_ID = 8191
MAX_NAME_BYTES = 50  # uavcan.node.GetInfo's name
MAX_PERIOD_S = 86_400.0

HEALTH = {"nominal": 0, "advisory": 1, "caution": 2, "warning": 3}
MODES = {"operational": 0, "initialization": 1, "maintenance": 2, "software_update": 3}
WAVES = ("sine", "ramp", "square", "noise", "count")
_PERIODIC_WAVES = ("sine", "ramp", "square")
_WAVE_KEY = "$wave"
# Single underscores only: its register, uavcan.pub.<port>.id, is set through
# UAVCAN__PUB__<PORT>__ID, where a double one would read as a dot.
_PORT_NAME = re.compile(r"^[a-z](?:_?[a-z0-9])*$")
MAX_PORT_NAME = 32
# How long a stopped test node's node-ID is still taken for its own: the
# scanner keeps a node for 3 s after its last heartbeat.
_STOPPED_GRACE_S = 5.0

# The fixed subjects every test node publishes on itself.
_OWN_SUBJECTS = {7509: "heartbeat", 7510: "port list"}

_NODE_KEYS = {"name", "node_id", "health", "mode", "vendor_status", "software_version", "publishers"}
_PUBLISHER_KEYS = {"subject_id", "type", "rate_hz", "port", "value"}


class ConfirmationRequired(RuntimeError):
    """Starting a test node on a real bus needs the caller's confirmation."""


class UnknownTestNode(LookupError):
    """There is no test node with the id given."""


# ── Value schemas and waveforms ──

def value_schema(data_type) -> dict:
    """A JSON description of DSDL type ``data_type`` (a pydsdl type), for building and checking values."""
    import pydsdl
    if isinstance(data_type, pydsdl.DelimitedType):
        data_type = data_type.inner_type
    if isinstance(data_type, pydsdl.BooleanType):
        # Named so whatever the pydsdl: older ones call it "saturated bool".
        return {"kind": "bool", "type": "bool"}
    if isinstance(data_type, (pydsdl.IntegerType, pydsdl.FloatType)):
        limits = data_type.inclusive_value_range
        number = int if isinstance(data_type, pydsdl.IntegerType) else float
        return {"kind": number.__name__, "type": str(data_type),
                "min": number(limits.min), "max": number(limits.max)}
    if isinstance(data_type, pydsdl.ArrayType):
        element = data_type.element_type
        schema = {"kind": "array", "type": str(data_type), "capacity": data_type.capacity,
                  "fixed": isinstance(data_type, pydsdl.FixedLengthArrayType), "element": value_schema(element)}
        if isinstance(element, pydsdl.UnsignedIntegerType) and element.bit_length == 8:
            schema["text"] = True  # takes a string, as pycyphal does for byte arrays
        return schema
    if isinstance(data_type, pydsdl.CompositeType):
        union = isinstance(data_type, pydsdl.UnionType)
        return {"kind": "union" if union else "struct", "type": str(data_type),
                "variants" if union else "fields": [{"name": f.name, "schema": value_schema(f.data_type)}
                                                    for f in data_type.fields_except_padding],
                "constants": [{"name": c.name, "value": _native(c.value.native_value)}
                              for c in data_type.constants]}
    raise ValueError(f"{data_type} cannot be given a value")


def _native(value):
    """A constant's value as JSON takes it (pydsdl gives numbers as fractions)."""
    if isinstance(value, (bool, str)):
        return value
    return int(value) if value.denominator == 1 else float(value)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_wave(value) -> bool:
    return isinstance(value, dict) and _WAVE_KEY in value


def check_value(schema: dict, value: Any, path: str = "value") -> None:
    """ValueError, saying where, if ``value`` is not one for ``schema``; None means the default."""
    if value is None:
        return
    kind = schema["kind"]
    if kind in ("int", "float"):
        if _is_wave(value):
            _check_wave(value, path)
        elif not _is_number(value):
            raise ValueError(f"{path}: a number or a waveform for {schema['type']}, not {value!r}")
    elif kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{path}: true or false for {schema['type']}, not {value!r}")
    elif kind == "array":
        if isinstance(value, str) and schema.get("text"):
            if len(value.encode()) > schema["capacity"]:
                raise ValueError(f"{path}: {len(value.encode())} bytes do not fit {schema['type']}")
            return
        if not isinstance(value, list):
            raise ValueError(f"{path}: a list for {schema['type']}, not {value!r}")
        if len(value) > schema["capacity"]:
            raise ValueError(f"{path}: {len(value)} elements do not fit {schema['type']}")
        for index, element in enumerate(value):
            check_value(schema["element"], element, f"{path}[{index}]")
    elif kind == "struct":
        if not isinstance(value, dict):
            raise ValueError(f"{path}: an object for {schema['type']}, not {value!r}")
        fields = {f["name"]: f["schema"] for f in schema["fields"]}
        for name, field_value in value.items():
            if name not in fields:
                raise ValueError(f"{path}: {schema['type']} has no field {name!r}")
            check_value(fields[name], field_value, f"{path}.{name}")
    elif kind == "union":
        if not isinstance(value, dict) or len(value) > 1:
            raise ValueError(f"{path}: an object with one of {schema['type']}'s variants, not {value!r}")
        variants = {v["name"]: v["schema"] for v in schema["variants"]}
        for name, variant_value in value.items():
            if name not in variants:
                raise ValueError(f"{path}: {schema['type']} has no variant {name!r}")
            check_value(variants[name], variant_value, f"{path}.{name}")
    else:
        raise ValueError(f"{path}: unknown kind {kind!r}")


def _check_wave(wave: dict, path: str) -> None:
    unknown = set(wave) - {_WAVE_KEY, "min", "max", "period_s"}
    if unknown:
        raise ValueError(f"{path}: a waveform has no {', '.join(sorted(unknown))}")
    kind = wave[_WAVE_KEY]
    if kind not in WAVES:
        raise ValueError(f"{path}: the waveform is one of {', '.join(WAVES)}, not {kind!r}")
    low, high = wave.get("min"), wave.get("max")
    if not (_is_number(low) and _is_number(high) and low <= high):
        raise ValueError(f"{path}: a waveform needs numbers min <= max, not {low!r} and {high!r}")
    if kind in _PERIODIC_WAVES:
        period = wave.get("period_s")
        if not (_is_number(period) and 0 < period <= MAX_PERIOD_S):
            raise ValueError(f"{path}: a {kind} wave needs period_s, in seconds up to {MAX_PERIOD_S:g}")


def wave_value(wave: dict, t: float, count: int, rng: random.Random) -> float:
    """Waveform ``wave`` at ``t`` seconds and the ``count``th message."""
    kind, low, high = wave[_WAVE_KEY], wave["min"], wave["max"]
    span = high - low
    if kind == "noise":
        return rng.uniform(low, high)
    if kind == "count":
        return low + count % (math.floor(span) + 1)
    phase = (t / wave["period_s"]) % 1.0
    if kind == "sine":  # from min up to max and back, as a cosine starting at its trough
        return low + span * (1 - math.cos(2 * math.pi * phase)) / 2
    if kind == "ramp":
        return low + span * phase
    return low if phase < 0.5 else high  # square


def evaluate(schema: dict, value: Any, t: float, count: int = 0, rng: Optional[random.Random] = None) -> Any:
    """``value`` (checked by check_value) in builtin form at ``t`` seconds and the ``count``th message."""
    rng = rng or random.Random()
    kind = schema["kind"]
    if kind in ("int", "float"):
        if value is None:
            number = 0
        elif _is_wave(value):
            number = wave_value(value, t, count, rng)
        else:
            number = value
        number = min(max(number, schema["min"]), schema["max"])
        return round(number) if kind == "int" else float(number)
    if kind == "bool":
        return bool(value)
    if kind == "array":
        if isinstance(value, str):
            value = list(value.encode())
        elements = [evaluate(schema["element"], v, t, count, rng) for v in value or []]
        if schema["fixed"]:  # every element is there, the missing ones at their default
            elements += [evaluate(schema["element"], None, t, count, rng)
                         for _ in range(schema["capacity"] - len(elements))]
        return elements
    if kind == "struct":
        fields = {f["name"]: f["schema"] for f in schema["fields"]}
        return {name: evaluate(fields[name], v, t, count, rng) for name, v in (value or {}).items()}
    if kind == "union":
        variants = {v["name"]: v["schema"] for v in schema["variants"]}
        return {name: evaluate(variants[name], v, t, count, rng) for name, v in (value or {}).items()}
    raise ValueError(f"unknown kind {kind!r}")


# ── Definitions ──

def normalize_definition(raw: Any, schema_of: Callable[[str], dict]) -> dict:
    """A test node's definition, checked and with its defaults; ValueError saying what is wrong.

    ``schema_of`` gives a message type's value_schema, ValueError if there is no such type.
    """
    if not isinstance(raw, dict):
        raise ValueError("A test node is a JSON object")
    unknown = set(raw) - _NODE_KEYS
    if unknown:
        raise ValueError(f"A test node has no {', '.join(sorted(unknown))}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip().encode()) > MAX_NAME_BYTES:
        raise ValueError(f"name: 1 to {MAX_NAME_BYTES} bytes of text")
    # None leaves it to Cynitor, which gives the node a free one when it starts.
    node_id = None if raw.get("node_id") is None else _integer(raw["node_id"], "node_id", MIN_NODE_ID, MAX_NODE_ID)
    health = raw.get("health", "nominal")
    if health not in HEALTH:
        raise ValueError(f"health: one of {', '.join(HEALTH)}")
    mode = raw.get("mode", "operational")
    if mode not in MODES:
        raise ValueError(f"mode: one of {', '.join(MODES)}")
    vendor_status = _integer(raw.get("vendor_status", 0), "vendor_status", 0, 255)
    version = raw.get("software_version", {"major": 1, "minor": 0})
    if not isinstance(version, dict) or set(version) - {"major", "minor"}:
        raise ValueError("software_version: an object with major and minor")
    software_version = {part: _integer(version.get(part, 0), f"software_version.{part}", 0, 255)
                        for part in ("major", "minor")}
    publishers = raw.get("publishers", [])
    if not isinstance(publishers, list) or len(publishers) > MAX_PUBLISHERS:
        raise ValueError(f"publishers: a list of up to {MAX_PUBLISHERS}")
    normalized = [_normalize_publisher(p, f"publishers[{i}]", schema_of) for i, p in enumerate(publishers)]
    _name_ports(normalized)
    subjects = [p["subject_id"] for p in normalized]
    if len(set(subjects)) != len(subjects):
        raise ValueError("publishers: one publisher per subject-ID")
    return {"name": name.strip(), "node_id": node_id, "health": health, "mode": mode,
            "vendor_status": vendor_status, "software_version": software_version, "publishers": normalized}


def _integer(value, path: str, low: int, high: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"{path}: a whole number from {low} to {high}, not {value!r}")
    return value


def _normalize_publisher(raw: Any, path: str, schema_of: Callable[[str], dict]) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: an object")
    unknown = set(raw) - _PUBLISHER_KEYS
    if unknown:
        raise ValueError(f"{path}: a publisher has no {', '.join(sorted(unknown))}")
    subject_id = _integer(raw.get("subject_id"), f"{path}.subject_id", 0, MAX_SUBJECT_ID)
    if subject_id in _OWN_SUBJECTS:
        raise ValueError(f"{path}.subject_id: {subject_id} carries the node's own {_OWN_SUBJECTS[subject_id]}")
    type_name = raw.get("type")
    if not isinstance(type_name, str) or not type_name:
        raise ValueError(f"{path}.type: a message type's full name, such as uavcan.primitive.scalar.Real32.1.0")
    try:
        schema = schema_of(type_name)
    except ValueError as exc:
        raise ValueError(f"{path}.type: {exc}") from None
    rate = raw.get("rate_hz", 1.0)
    if not _is_number(rate) or not MIN_RATE_HZ <= rate <= MAX_RATE_HZ:
        raise ValueError(f"{path}.rate_hz: from {MIN_RATE_HZ:g} to {MAX_RATE_HZ:g} messages a second")
    port = raw.get("port")
    if port is not None and not (isinstance(port, str) and len(port) <= MAX_PORT_NAME and _PORT_NAME.match(port)):
        raise ValueError(f"{path}.port: up to {MAX_PORT_NAME} lowercase letters and digits, starting with a "
                         "letter, single underscores between them")
    value = raw.get("value", {})
    check_value(schema, value, f"{path}.value")
    return {"subject_id": subject_id, "type": type_name, "rate_hz": float(rate), "port": port, "value": value}


def _name_ports(publishers: list[dict]) -> None:
    """Give each publisher without a port name one from its type, its namespace's last part and its
    name (voltage_scalar for uavcan.si.unit.voltage.Scalar.1.0): its registers are uavcan.pub.<port>.*."""
    taken = {p["port"] for p in publishers if p["port"]}
    if len(taken) != sum(1 for p in publishers if p["port"]):
        raise ValueError("publishers: each port name once")
    for publisher in publishers:
        if publisher["port"]:
            continue
        base = re.sub(r"[^a-z0-9]+", "_", "_".join(publisher["type"].split(".")[-4:-2]).lower()).strip("_")
        base = base if base[:1].isalpha() else f"p{base}"
        port, suffix = base[:MAX_PORT_NAME].rstrip("_"), 2
        while port in taken:
            port, suffix = f"{base[:MAX_PORT_NAME - 4].rstrip('_')}_{suffix}", suffix + 1
        publisher["port"] = port
        taken.add(port)


def _status_changed_only(old: dict, new: dict) -> bool:
    """Whether the definitions differ in nothing a running node cannot take as it runs."""
    live = ("health", "mode", "vendor_status")
    return {k: v for k, v in old.items() if k not in live} == {k: v for k, v in new.items() if k not in live}


# ── Running ──

@dataclass
class TestNodeBus:
    """Where test nodes run while CAN is connected."""
    __test__ = False  # not a test, for pytest
    make_transport: Callable[[int], Any]     # a pycyphal transport for a node-ID
    simulated: bool                          # the simulated bus, where nothing real can move
    node_ids_in_use: Callable[[], set[int]]  # Cynitor's own and those of the nodes on the bus


class _Running:
    """A started test node: its pycyphal node and a publishing task per publisher."""

    def __init__(self, definition: dict, make_transport: Callable[[int], Any]) -> None:
        import pycyphal.application
        import uavcan.node
        from pycyphal.dsdl import get_model
        from scanner_node import ScannerNode

        self.definition = definition
        self.error: Optional[str] = None
        # Before the node: a type gone, or changed so that a value no longer fits it, fails the start.
        classes = []
        for publisher in definition["publishers"]:
            data_type = ScannerNode._message_class(ScannerNode._dsdl_type_to_module_name(publisher["type"]))
            schema = value_schema(get_model(data_type))
            check_value(schema, publisher["value"], f"{publisher['port']}.value")
            classes.append((publisher, data_type, schema))
        registers = {f"UAVCAN__PUB__{p['port'].upper()}__ID": str(p["subject_id"]) for p in definition["publishers"]}
        version = definition["software_version"]
        info = uavcan.node.GetInfo_1_0.Response(
            software_version=uavcan.node.Version_1_0(major=version["major"], minor=version["minor"]),
            name=definition["name"], unique_id=bytes.fromhex(definition["unique_id"]),
        )
        transport = make_transport(definition["node_id"])
        try:
            self.node = pycyphal.application.make_node(
                info, pycyphal.application.make_registry(None, environment_variables=registers), transport=transport)
        except BaseException:
            transport.close()
            raise
        try:
            self.set_status(definition)
            # Named, so that its registers say each publisher's subject-ID and type.
            self._publishers = [(p, t, schema, self.node.make_publisher(t, p["port"])) for p, t, schema in classes]
        except BaseException:
            self.node.close()
            raise
        self.node.start()
        self.started_unix = time.time()
        self._started = time.monotonic()
        self._rng = random.Random()
        self._tasks = [asyncio.create_task(self._publish(*publisher), name=f"test-node-{definition['node_id']}")
                       for publisher in self._publishers]

    def set_status(self, definition: dict) -> None:
        from pycyphal.application.heartbeat_publisher import Health, Mode
        heartbeat = self.node.heartbeat_publisher
        heartbeat.health = Health(HEALTH[definition["health"]])
        heartbeat.mode = Mode(MODES[definition["mode"]])
        heartbeat.vendor_specific_status_code = definition["vendor_status"]
        self.definition = definition

    async def _publish(self, publisher: dict, data_type, schema: dict, port) -> None:
        from pycyphal.dsdl import update_from_builtin
        loop = asyncio.get_running_loop()
        period = 1.0 / publisher["rate_hz"]
        count, due = 0, loop.time()
        try:
            while True:
                builtin = evaluate(schema, publisher["value"], time.monotonic() - self._started, count, self._rng)
                await port.publish(update_from_builtin(data_type(), builtin))
                count += 1
                due += period
                if due < loop.time() - period:  # fell behind: carry on from now rather than burst
                    due = loop.time()
                await asyncio.sleep(max(0.0, due - loop.time()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = f"Publisher {publisher['port']} on subject {publisher['subject_id']} stopped: {exc}"
            logger.warning("Test node %d: %s", self.definition["node_id"], self.error)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.node.close()

    @property
    def rate_hz(self) -> float:
        return sum(p["rate_hz"] for p in self.definition["publishers"])


class TestNodeManager:
    """The test nodes' definitions, kept in ``path``, and those running on the bus."""
    __test__ = False  # not a test, for pytest

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._nodes: dict[str, dict] = self._load()
        self._running: dict[str, _Running] = {}
        self._stopped_at: dict[int, float] = {}  # node-ID -> when a test node on it stopped
        self._bus: Optional[TestNodeBus] = None
        # Held while nodes start and stop, so that two requests cannot start one node twice.
        self._lock = asyncio.Lock()

    # Definitions

    def _load(self) -> dict[str, dict]:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("Could not read the test nodes from %s: %s", self.path, exc)
            return {}
        nodes = {}
        for node in saved.get("nodes", []) if isinstance(saved, dict) else []:
            if isinstance(node, dict) and isinstance(node.get("id"), str) and node.keys() >= _NODE_KEYS:
                nodes[node["id"]] = node
        return nodes

    def _save(self, nodes: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_name(f".{self.path.name}.part")
        partial.write_text(json.dumps({"version": 1, "nodes": list(nodes.values())}, indent=1), encoding="utf-8")
        os.replace(partial, self.path)

    def list(self) -> list[dict]:
        return [self.view(test_id) for test_id in self._nodes]

    def _definition(self, test_id: str) -> dict:
        try:
            return self._nodes[test_id]
        except KeyError:
            raise UnknownTestNode(f"There is no test node {test_id}") from None

    def view(self, test_id: str) -> dict:
        """A test node as the API gives it: its definition, and whether it runs. UnknownTestNode if there is none."""
        definition = self._definition(test_id)
        running = self._running.get(test_id)
        # assigned_node_id: the node-ID Cynitor gave one that sets none, when it last started.
        return {"assigned_node_id": None, **definition, "running": running is not None,
                "error": running.error if running else None,
                "started_unix": running.started_unix if running else None}

    def add(self, definition: dict) -> dict:
        """Keep a new test node (normalize_definition's); RuntimeError at the limit."""
        if len(self._nodes) >= MAX_TEST_NODES:
            raise RuntimeError(f"There can be up to {MAX_TEST_NODES} test nodes; delete one first")
        test_id = secrets.token_hex(4)
        while test_id in self._nodes:
            test_id = secrets.token_hex(4)
        node = {"id": test_id, "unique_id": secrets.token_hex(16), **definition}
        self._save({**self._nodes, test_id: node})
        self._nodes[test_id] = node
        return self.view(test_id)

    async def replace(self, test_id: str, definition: dict) -> dict:
        """Change a test node's definition; a running one takes it as it runs, or restarts with it."""
        async with self._lock:
            old = self._definition(test_id)
            node = {"id": test_id, "unique_id": old["unique_id"], **definition}
            if node["node_id"] is None and old.get("assigned_node_id") is not None:
                node["assigned_node_id"] = old["assigned_node_id"]  # still its own, for its next start
            running = self._running.get(test_id)
            live = running is not None and _status_changed_only(old, node)
            if running is not None and not live:
                node_id = self._node_id_for(test_id, node)
                self._check_rate(test_id, node)
            self._save({**self._nodes, test_id: node})
            self._nodes[test_id] = node
            if live:
                running.set_status({**node, "node_id": running.definition["node_id"]})
            elif running is not None:  # confirmed when it started, so no new confirmation
                await self._close(test_id)
                self._run(node, node_id)
                logger.info("Test node %d restarted with its new definition", node_id)
            return self.view(test_id)

    async def delete(self, test_id: str) -> None:
        async with self._lock:
            self._definition(test_id)
            if test_id in self._running:
                await self._close(test_id)
            remaining = {k: v for k, v in self._nodes.items() if k != test_id}
            self._save(remaining)
            self._nodes = remaining

    # Running

    def attach(self, bus: TestNodeBus) -> None:
        """CAN is connected: test nodes may run on ``bus``."""
        self._bus = bus

    async def detach(self) -> None:
        """CAN is disconnecting: stop every test node."""
        async with self._lock:
            self._bus = None
            for test_id in list(self._running):
                try:
                    await self._close(test_id)
                except Exception as exc:  # the others still stop, and the session still tears down
                    logger.warning("Test node %s did not stop cleanly: %s", test_id, exc)

    @property
    def bus(self) -> Optional[TestNodeBus]:
        return self._bus

    def running_node_ids(self) -> set[int]:
        return {running.definition["node_id"] for running in self._running.values()}

    def identify(self, node_id: Optional[int], unique_id_hex: Optional[str]) -> Optional[str]:
        """The id of the test node that the bus's node ``node_id``, with ``unique_id_hex``, is; None if none is.

        By unique-ID, so that one stopped is still marked; and by node-ID
        while one runs, before the scanner has asked for its unique-ID.
        """
        if unique_id_hex:
            for test_id, node in self._nodes.items():
                if node["unique_id"] == unique_id_hex:
                    return test_id
        for test_id, running in self._running.items():
            if node_id is not None and running.definition["node_id"] == node_id:
                return test_id
        return None

    async def start(self, test_id: str, confirm_real_bus: bool = False) -> dict:
        """Start a test node. UnknownTestNode if there is none; RuntimeError while CAN is not connected,
        or if its node-ID is taken; ConfirmationRequired on a real bus without ``confirm_real_bus``;
        ValueError if its types have changed so that its values no longer fit."""
        async with self._lock:
            definition = self._definition(test_id)
            if test_id in self._running:
                return self.view(test_id)
            if self._bus is None:
                raise RuntimeError("Connect CAN first: test nodes run on the bus Cynitor is connected to")
            if not self._bus.simulated and not confirm_real_bus:
                raise ConfirmationRequired(
                    "This is a real bus: a test node's messages reach every node on it, and may move real "
                    "things. Confirm to start it")
            node_id = self._node_id_for(test_id, definition)
            self._check_rate(test_id, definition)
            self._run(definition, node_id)
            logger.info("Test node %d (%s) started%s", node_id, definition["name"],
                        "" if self._bus.simulated else " on a real bus")
            return self.view(test_id)

    async def stop(self, test_id: str) -> dict:
        """Stop a test node; UnknownTestNode if there is none."""
        async with self._lock:
            self._definition(test_id)
            running = self._running.get(test_id)
            if running is not None:
                await self._close(test_id)
                logger.info("Test node %d stopped", running.definition["node_id"])
            return self.view(test_id)

    def _run(self, definition: dict, node_id: int) -> None:
        """Start the kept ``definition`` on ``node_id``: its own, or the one Cynitor gives it."""
        try:
            self._running[definition["id"]] = _Running({**definition, "node_id": node_id}, self._bus.make_transport)
        except ValueError:
            raise
        except Exception as exc:  # the bus refused it, say: as much a conflict with the bus as a taken node-ID
            raise RuntimeError(f"Test node {definition['name']} did not start: {exc}") from exc
        if definition["node_id"] is None and definition.get("assigned_node_id") != node_id:
            definition["assigned_node_id"] = node_id  # kept, so that it is the same node at its next start
            self._save(self._nodes)

    async def _close(self, test_id: str) -> None:
        running = self._running.pop(test_id)
        self._stopped_at[running.definition["node_id"]] = time.monotonic()
        await running.close()

    def _node_id_for(self, test_id: str, definition: dict) -> int:
        """The node-ID a test node starts on: its own, which must be free, or a free one if it sets none.

        One that sets none takes the one it was given last time while that is
        free, so that it is the same node from run to run; else the first free
        from AUTO_NODE_ID_FROM up, then below. Never one that another test node
        is set to or was given, which that node would then find taken.
        RuntimeError if its own is taken, or none is free.
        """
        if definition["node_id"] is not None:
            self._check_node_id(test_id, definition["node_id"])
            return definition["node_id"]
        others = {node["node_id"] or node.get("assigned_node_id")
                  for other_id, node in self._nodes.items() if other_id != test_id}
        candidates = [definition.get("assigned_node_id"), *range(AUTO_NODE_ID_FROM, MAX_NODE_ID + 1),
                      *range(MIN_NODE_ID, AUTO_NODE_ID_FROM)]
        for node_id in candidates:
            if node_id is not None and node_id not in others and self._why_taken(test_id, node_id) is None:
                return node_id
        raise RuntimeError("No node-ID is free on the bus for this test node")

    def _why_taken(self, test_id: str, node_id: int) -> Optional[str]:
        """Why test node ``test_id`` cannot start on ``node_id``, or None if it can."""
        for other_id, running in self._running.items():
            if other_id != test_id and running.definition["node_id"] == node_id:
                return f"Test node {running.definition['name']} already runs on node-ID {node_id}"
        now = time.monotonic()
        ours = {r.definition["node_id"] for r in self._running.values()}
        ours |= {nid for nid, at in self._stopped_at.items() if now - at < _STOPPED_GRACE_S}
        if node_id in self._bus.node_ids_in_use() and node_id not in ours:
            return (f"Node-ID {node_id} is in use on the bus; give the test node another, "
                    "or none, for Cynitor to give it a free one")
        return None

    def _check_node_id(self, test_id: str, node_id: int) -> None:
        reason = self._why_taken(test_id, node_id)
        if reason:
            raise RuntimeError(reason)

    def _check_rate(self, test_id: str, definition: dict) -> None:
        others = sum(r.rate_hz for other_id, r in self._running.items() if other_id != test_id)
        own = sum(p["rate_hz"] for p in definition["publishers"])
        if others + own > MAX_TOTAL_RATE_HZ:
            raise RuntimeError(
                f"Test nodes may publish {MAX_TOTAL_RATE_HZ:g} messages a second in all; with this one "
                f"they would publish {others + own:g}")
