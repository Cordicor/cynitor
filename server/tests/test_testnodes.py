"""Tests for test nodes (testnodes.py): value schemas, waveforms, definitions, the manager and the API.

A running test node is a pycyphal node, which needs the compiled DSDL: those
run in the integration test (tests/integration/hub_session.py). Here a
stand-in takes their place.
"""

import json
import random
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

import testnodes
from dsdl_manager import DsdlManager
from testnodes import (MAX_PUBLISHERS, MAX_TEST_NODES, ConfirmationRequired, TestNodeBus, TestNodeManager,
                       UnknownTestNode, check_value, evaluate, normalize_definition, value_schema, wave_value)
from websocket_server import WebSocketServer

REAL32 = "uavcan.primitive.scalar.Real32.1.0"

# Sources for the types the tests read, as a compile reads them: public ones
# under public_regulated_data_types, custom ones under custom.
_TYPES = {
    "uavcan/primitive/Empty.1.0.dsdl": "@sealed\n",
    "uavcan/primitive/scalar/Real32.1.0.dsdl": "float32 value\n@sealed\n",
    "demo/Choice.1.0.dsdl": "@union\nuavcan.primitive.Empty.1.0 none\nuint8 small\nfloat32 large\n@sealed\n",
    "demo/Reading.1.0.dsdl": """\
uint8 KIND_RAW = 3
int16 level
float16 gain
bool valid
void4
uint8[<=8] label
float32[3] vector
Choice.1.0 choice
@extent 64 * 8
""",
    "demo/Ping.1.0.dsdl": "uint8 x\n@sealed\n---\n@sealed\n",
    "demo/Uncompiled.1.0.dsdl": "uint8 x\n@sealed\n",
}
_COMPILED = ["uavcan/primitive/scalar/Real32_1_0.py", "demo/Choice_1_0.py", "demo/Reading_1_0.py", "demo/Ping_1_0.py"]


@pytest.fixture
def dsdl(tmp_path: Path) -> DsdlManager:
    root = tmp_path / "project"
    for rel, text in _TYPES.items():
        base = "public_regulated_data_types" if rel.startswith("uavcan/") else "custom"
        path = root / "dsdl_messages" / base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for rel in _COMPILED:  # what is_compiled looks for
        path = root / "python_compiled_messages" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return DsdlManager(root)


@pytest.fixture
def reading(dsdl: DsdlManager) -> dict:
    return value_schema(dsdl.read_model("demo.Reading.1.0"))


def _fields(schema: dict) -> dict:
    return {f["name"]: f["schema"] for f in schema["fields"]}


class TestValueSchema:
    def test_a_struct(self, reading):
        # Not sealed, so delimited: the schema is of what it delimits.
        assert (reading["kind"], reading["type"]) == ("struct", "demo.Reading.1.0")
        fields = _fields(reading)
        assert list(fields) == ["level", "gain", "valid", "label", "vector", "choice"]  # no padding
        assert fields["level"] == {"kind": "int", "type": "saturated int16", "min": -32768, "max": 32767}
        assert fields["gain"] == {"kind": "float", "type": "saturated float16", "min": -65504.0, "max": 65504.0}
        assert fields["valid"] == {"kind": "bool", "type": "bool"}
        assert reading["constants"] == [{"name": "KIND_RAW", "value": 3}]

    def test_arrays(self, reading):
        fields = _fields(reading)
        label, vector = fields["label"], fields["vector"]
        assert (label["capacity"], label["fixed"], label["text"]) == (8, False, True)  # bytes, so text will do
        assert (vector["capacity"], vector["fixed"], vector["element"]["kind"]) == (3, True, "float")
        assert "text" not in vector

    def test_a_union(self, reading):
        choice = _fields(reading)["choice"]
        assert (choice["kind"], choice["type"]) == ("union", "demo.Choice.1.0")
        assert [v["name"] for v in choice["variants"]] == ["none", "small", "large"]

    def test_is_json(self, reading):
        assert json.loads(json.dumps(reading)) == reading


class TestCheckValue:
    def test_good_values(self, reading):
        check_value(reading, {
            "level": -5, "gain": {"$wave": "sine", "min": 0, "max": 2, "period_s": 10}, "valid": True,
            "label": "probe", "vector": [1, {"$wave": "noise", "min": -1, "max": 1}], "choice": {"large": 2.5},
        })
        check_value(reading, {})    # every field at its default
        check_value(reading, None)

    @pytest.mark.parametrize("value, message", [
        ({"level": "5"}, "value.level: a number or a waveform for saturated int16, not '5'"),
        ({"level": True}, "value.level: a number or a waveform"),
        ({"level": float("nan")}, "value.level: a number or a waveform"),
        ({"valid": 1}, "value.valid: true or false for bool, not 1"),
        ({"vector": [1, 2, 3, 4]}, "value.vector: 4 elements do not fit saturated float32[3]"),
        ({"vector": [1, "x"]}, "value.vector[1]: a number or a waveform"),
        ({"label": "much too long"}, "value.label: 13 bytes do not fit"),
        ({"nope": 1}, "value: demo.Reading.1.0 has no field 'nope'"),
        ({"choice": {"small": 1, "large": 2}}, "value.choice: an object with one of demo.Choice.1.0's variants"),
        ({"choice": {"huge": 1}}, "value.choice: demo.Choice.1.0 has no variant 'huge'"),
        ([1], "value: an object for demo.Reading.1.0, not [1]"),
    ])
    def test_bad_values(self, reading, value, message):
        with pytest.raises(ValueError, match=re.escape(message)):
            check_value(reading, value)

    @pytest.mark.parametrize("wave, message", [
        ({"$wave": "zigzag", "min": 0, "max": 1}, "the waveform is one of sine, ramp, square, noise, count"),
        ({"$wave": "noise", "min": 2, "max": 1}, "a waveform needs numbers min <= max"),
        ({"$wave": "noise", "min": 0}, "a waveform needs numbers min <= max"),
        ({"$wave": "sine", "min": 0, "max": 1}, "a sine wave needs period_s"),
        ({"$wave": "square", "min": 0, "max": 1, "period_s": 0}, "a square wave needs period_s"),
        ({"$wave": "ramp", "min": 0, "max": 1, "period_s": 1, "phase": 2}, "a waveform has no phase"),
    ])
    def test_bad_waves(self, reading, wave, message):
        with pytest.raises(ValueError, match=re.escape(f"value.level: {message}")):
            check_value(reading, {"level": wave})


def _wave(kind, low=0.0, high=10.0, period=4.0) -> dict:
    return {"$wave": kind, "min": low, "max": high, "period_s": period}


class TestWaves:
    @pytest.mark.parametrize("t, expected", [(0, 0), (1, 5), (2, 10), (3, 5), (4, 0), (6, 10)])
    def test_sine_runs_from_min_to_max_and_back(self, t, expected):
        assert wave_value(_wave("sine"), t, 0, random.Random()) == pytest.approx(expected)

    @pytest.mark.parametrize("t, expected", [(0, 0), (1, 2.5), (3, 7.5), (4, 0), (5, 2.5)])
    def test_ramp_rises_and_starts_over(self, t, expected):
        assert wave_value(_wave("ramp"), t, 0, random.Random()) == pytest.approx(expected)

    @pytest.mark.parametrize("t, expected", [(0, 0), (1.9, 0), (2, 10), (3.9, 10), (4, 0)])
    def test_square_is_min_then_max(self, t, expected):
        assert wave_value(_wave("square"), t, 0, random.Random()) == expected

    def test_count_adds_one_a_message_and_wraps(self):
        count = {"$wave": "count", "min": 1, "max": 3}
        assert [wave_value(count, 0, n, random.Random()) for n in range(5)] == [1, 2, 3, 1, 2]

    def test_noise_stays_within_its_range(self):
        rng = random.Random(7)
        values = [wave_value(_wave("noise", -1, 1), 0, 0, rng) for _ in range(1000)]
        assert all(-1 <= v <= 1 for v in values) and len(set(values)) > 900


INT8 = {"kind": "int", "type": "saturated int8", "min": -128, "max": 127}
UINT8 = {"kind": "int", "type": "saturated uint8", "min": 0, "max": 255}
FLOAT16 = {"kind": "float", "type": "saturated float16", "min": -65504.0, "max": 65504.0}
BOOL = {"kind": "bool", "type": "bool"}


class TestEvaluate:
    def test_numbers_are_rounded_and_clamped_to_their_field(self):
        assert evaluate(INT8, 2.6, 0) == 3
        assert evaluate(INT8, 1000, 0) == 127
        assert evaluate(INT8, _wave("ramp", -1000, 1000), 0) == -128
        assert evaluate(FLOAT16, 1e9, 0) == 65504.0
        assert isinstance(evaluate(FLOAT16, 3, 0), float)

    def test_defaults(self):
        assert evaluate(INT8, None, 0) == 0 and evaluate(BOOL, None, 0) is False

    def test_text_is_bytes(self):
        text = {"kind": "array", "type": "saturated uint8[<=8]", "capacity": 8, "fixed": False,
                "element": UINT8, "text": True}
        assert evaluate(text, "hi", 0) == [104, 105]

    def test_a_fixed_array_is_filled_with_defaults(self):
        vector = {"kind": "array", "type": "saturated float16[3]", "capacity": 3, "fixed": True, "element": FLOAT16}
        assert evaluate(vector, [1.5], 0) == [1.5, 0.0, 0.0]

    def test_a_message(self, reading):
        value = {"level": _wave("sine", 0, 100), "valid": True, "label": "ab",
                 "vector": [{"$wave": "count", "min": 0, "max": 9}], "choice": {"small": 300}}
        assert evaluate(reading, value, t=2.0, count=12) == {
            "level": 100, "valid": True, "label": [97, 98], "vector": [2.0, 0.0, 0.0], "choice": {"small": 255},
        }  # gain is not given: the message keeps its default

    def test_a_union_without_a_variant_keeps_its_default(self, reading):
        assert evaluate(_fields(reading)["choice"], {}, 0) == {}


_SCHEMAS = {REAL32: {"kind": "struct", "type": REAL32, "constants": [], "fields": [
    {"name": "value", "schema": {"kind": "float", "type": "saturated float32", "min": -3.4e38, "max": 3.4e38}}]}}


def _schema_of(full_name: str) -> dict:
    try:
        return _SCHEMAS[full_name]
    except KeyError:
        raise ValueError(f"There is no DSDL type {full_name}") from None


def _publisher(subject_id=100, **more) -> dict:
    return {"subject_id": subject_id, "type": REAL32, **more}


class TestNormalizeDefinition:
    def test_defaults(self):
        assert normalize_definition({"name": " probe ", "node_id": 60}, _schema_of) == {
            "name": "probe", "node_id": 60, "health": "nominal", "mode": "operational", "vendor_status": 0,
            "software_version": {"major": 1, "minor": 0}, "publishers": [],
        }

    def test_no_node_id_leaves_it_to_cynitor(self):
        for raw in ({"name": "probe"}, {"name": "probe", "node_id": None}):
            assert normalize_definition(raw, _schema_of)["node_id"] is None

    def test_a_publisher(self):
        value = {"value": _wave("sine", 280, 300, 60)}
        node = normalize_definition({"name": "probe", "node_id": 60, "health": "caution",
                                     "publishers": [_publisher(rate_hz=5, value=value)]}, _schema_of)
        assert node["health"] == "caution"
        assert node["publishers"] == [{"subject_id": 100, "type": REAL32, "rate_hz": 5.0, "port": "scalar_real32",
                                       "value": value}]

    def test_ports_are_named_after_their_types(self):
        publishers = [_publisher(100), _publisher(101), _publisher(102), _publisher(103, port="scalar_real32_3")]
        node = normalize_definition({"name": "probe", "node_id": 60, "publishers": publishers}, _schema_of)
        assert [p["port"] for p in node["publishers"]] == [
            "scalar_real32", "scalar_real32_2", "scalar_real32_4", "scalar_real32_3"]

    def test_a_long_port_name_is_cut_short(self):
        _SCHEMAS["very_long_namespace.of_types.ExtraordinarilyLongName.1.0"] = _SCHEMAS[REAL32]
        try:
            node = normalize_definition({"name": "n", "node_id": 60, "publishers": [
                {"subject_id": 100, "type": "very_long_namespace.of_types.ExtraordinarilyLongName.1.0"},
                {"subject_id": 101, "type": "very_long_namespace.of_types.ExtraordinarilyLongName.1.0"}]},
                _schema_of)
        finally:
            del _SCHEMAS["very_long_namespace.of_types.ExtraordinarilyLongName.1.0"]
        ports = [p["port"] for p in node["publishers"]]
        assert ports == ["of_types_extraordinarilylongname", "of_types_extraordinarilylong_2"]
        assert all(len(port) <= 32 for port in ports)

    @pytest.mark.parametrize("raw, message", [
        ([], "A test node is a JSON object"),
        ({"name": "n", "node_id": 60, "colour": "red"}, "A test node has no colour"),
        ({"name": " ", "node_id": 60}, "name: 1 to 50 bytes of text"),
        ({"name": "x" * 51, "node_id": 60}, "name: 1 to 50 bytes of text"),
        ({"name": "n", "node_id": 0}, "node_id: a whole number from 1 to 125, not 0"),
        ({"name": "n", "node_id": 126}, "node_id: a whole number from 1 to 125"),
        ({"name": "n", "node_id": 60.0}, "node_id: a whole number from 1 to 125"),
        ({"name": "n", "node_id": True}, "node_id: a whole number from 1 to 125"),
        ({"name": "n", "node_id": 60, "health": "great"}, "health: one of nominal, advisory, caution, warning"),
        ({"name": "n", "node_id": 60, "mode": "asleep"}, "mode: one of operational, initialization"),
        ({"name": "n", "node_id": 60, "vendor_status": 256}, "vendor_status: a whole number from 0 to 255"),
        ({"name": "n", "node_id": 60, "software_version": {"major": 1, "patch": 2}},
         "software_version: an object with major and minor"),
        ({"name": "n", "node_id": 60, "publishers": {}}, f"publishers: a list of up to {MAX_PUBLISHERS}"),
        ({"name": "n", "node_id": 60, "publishers": [_publisher(i) for i in range(MAX_PUBLISHERS + 1)]},
         f"publishers: a list of up to {MAX_PUBLISHERS}"),
    ])
    def test_bad_nodes(self, raw, message):
        with pytest.raises(ValueError, match=re.escape(message)):
            normalize_definition(raw, _schema_of)

    @pytest.mark.parametrize("publisher, message", [
        ({"subject_id": 8192, "type": REAL32}, "subject_id: a whole number from 0 to 8191"),
        ({"subject_id": 7509, "type": REAL32}, "subject_id: 7509 carries the node's own heartbeat"),
        ({"subject_id": 100}, "type: a message type's full name"),
        ({"subject_id": 100, "type": "demo.Nope.1.0"}, "type: There is no DSDL type demo.Nope.1.0"),
        (_publisher(rate_hz=0), "rate_hz: from 0.01 to 100 messages a second"),
        (_publisher(rate_hz=101), "rate_hz: from 0.01 to 100 messages a second"),
        (_publisher(port="Temp"), "port: up to 32 lowercase letters and digits"),
        (_publisher(port="a__b"), "port: up to 32 lowercase letters and digits"),  # reads as a dot
        (_publisher(port="a_"), "port: up to 32 lowercase letters and digits"),
        (_publisher(port="a" * 33), "port: up to 32 lowercase letters and digits"),
        (_publisher(value={"value": "hot"}), "value.value: a number or a waveform"),
        (_publisher(mystery=1), ": a publisher has no mystery"),
    ])
    def test_bad_publishers(self, publisher, message):
        with pytest.raises(ValueError, match=re.escape(f"publishers[0]{'' if message[0] == ':' else '.'}{message}")):
            normalize_definition({"name": "n", "node_id": 60, "publishers": [publisher]}, _schema_of)

    def test_one_publisher_a_subject(self):
        with pytest.raises(ValueError, match="one publisher per subject-ID"):
            normalize_definition({"name": "n", "node_id": 60, "publishers": [_publisher(), _publisher()]},
                                 _schema_of)

    def test_each_port_name_once(self):
        publishers = [_publisher(100, port="a"), _publisher(101, port="a")]
        with pytest.raises(ValueError, match="each port name once"):
            normalize_definition({"name": "n", "node_id": 60, "publishers": publishers}, _schema_of)


class _StandIn:
    """Stands in for a running test node: no pycyphal, no bus."""

    def __init__(self, definition: dict, make_transport) -> None:
        if definition["name"] == "broken":
            raise OSError("the bus is gone")
        if definition["name"] == "outdated":
            raise ValueError("value.value: a number or a waveform")
        self.definition = definition
        self.transport = make_transport(definition["node_id"])
        self.error = None
        self.started_unix = 1_700_000_000.0
        self.closed = False

    def set_status(self, definition: dict) -> None:
        self.definition = definition

    async def close(self) -> None:
        self.closed = True

    @property
    def rate_hz(self) -> float:
        return sum(p["rate_hz"] for p in self.definition["publishers"])


@pytest.fixture
def started(monkeypatch) -> list:
    """Every stand-in started, in order."""
    made = []

    class StandIn(_StandIn):
        def __init__(self, definition, make_transport):
            super().__init__(definition, make_transport)
            made.append(self)

    monkeypatch.setattr(testnodes, "_Running", StandIn)
    return made


@pytest.fixture
def manager(tmp_path: Path, started) -> TestNodeManager:
    return TestNodeManager(tmp_path / "test_nodes.json")


def _bus(simulated: bool = True, in_use=()) -> TestNodeBus:
    return TestNodeBus(make_transport=lambda node_id: ("transport", node_id), simulated=simulated,
                       node_ids_in_use=lambda: set(in_use))


def _node(name: str = "probe", node_id: int = 60, **more) -> dict:
    return {"name": name, "node_id": node_id, "health": "nominal", "mode": "operational", "vendor_status": 0,
            "software_version": {"major": 1, "minor": 0}, "publishers": [], **more}


def _running_publisher(subject_id: int = 100, rate_hz: float = 10.0) -> dict:
    return {"subject_id": subject_id, "type": REAL32, "rate_hz": rate_hz, "port": f"p{subject_id}", "value": {}}


class TestDefinitions:
    def test_kept_in_the_data_folder(self, manager):
        added = manager.add(_node())
        assert (added["running"], added["error"], len(added["unique_id"])) == (False, None, 32)
        assert TestNodeManager(manager.path).list() == [added]
        assert json.loads(manager.path.read_text())["version"] == 1

    def test_each_has_an_id_and_a_unique_id_of_its_own(self, manager):
        a, b = manager.add(_node()), manager.add(_node())
        assert a["id"] != b["id"] and a["unique_id"] != b["unique_id"]

    def test_up_to_a_limit(self, manager):
        for _ in range(MAX_TEST_NODES):
            manager.add(_node())
        with pytest.raises(RuntimeError, match=f"up to {MAX_TEST_NODES} test nodes"):
            manager.add(_node())

    async def test_a_change_keeps_its_ids(self, manager):
        added = manager.add(_node())
        changed = await manager.replace(added["id"], _node("renamed"))
        assert (changed["id"], changed["unique_id"], changed["name"]) == (added["id"], added["unique_id"], "renamed")
        assert TestNodeManager(manager.path).list() == [changed]

    async def test_delete(self, manager):
        test_id = manager.add(_node())["id"]
        await manager.delete(test_id)
        assert manager.list() == [] and TestNodeManager(manager.path).list() == []

    async def test_unknown(self, manager):
        with pytest.raises(UnknownTestNode):
            manager.view("nope")
        for action in (lambda: manager.start("nope"), lambda: manager.stop("nope"),
                       lambda: manager.delete("nope"), lambda: manager.replace("nope", _node())):
            with pytest.raises(UnknownTestNode):
                await action()

    def test_an_unreadable_file_leaves_none(self, tmp_path, caplog):
        path = tmp_path / "test_nodes.json"
        path.write_text("{")
        assert TestNodeManager(path).list() == [] and "Could not read the test nodes" in caplog.text

    def test_incomplete_entries_are_left_out(self, tmp_path):
        path = tmp_path / "test_nodes.json"
        whole = {"id": "a1", "unique_id": "00" * 16, **_node()}
        path.write_text(json.dumps({"version": 1, "nodes": [whole, {"id": "b2", "name": "half"}, "junk"]}))
        assert [n["id"] for n in TestNodeManager(path).list()] == ["a1"]


class TestRunning:
    async def test_not_before_can_connects(self, manager):
        test_id = manager.add(_node())["id"]
        with pytest.raises(RuntimeError, match="Connect CAN first"):
            await manager.start(test_id)

    async def test_on_the_simulated_bus_without_confirmation(self, manager, started):
        test_id = manager.add(_node())["id"]
        manager.attach(_bus(simulated=True))
        view = await manager.start(test_id)
        assert view["running"] and view["started_unix"] == started[0].started_unix
        assert started[0].transport == ("transport", 60) and manager.running_node_ids() == {60}

    async def test_on_a_real_bus_once_confirmed(self, manager, started):
        test_id = manager.add(_node())["id"]
        manager.attach(_bus(simulated=False))
        with pytest.raises(ConfirmationRequired, match="This is a real bus"):
            await manager.start(test_id)
        assert started == []
        assert (await manager.start(test_id, confirm_real_bus=True))["running"]

    async def test_starting_twice_runs_one(self, manager, started):
        test_id = manager.add(_node())["id"]
        manager.attach(_bus())
        await manager.start(test_id)
        await manager.start(test_id)
        assert len(started) == 1

    async def test_not_on_a_node_id_in_use(self, manager, started):
        test_id = manager.add(_node(node_id=42))["id"]
        manager.attach(_bus(in_use={42}))
        with pytest.raises(RuntimeError, match="Node-ID 42 is in use on the bus"):
            await manager.start(test_id)
        assert started == []

    async def test_restarted_at_once_its_own_node_id_is_not_taken(self, manager, started):
        in_use = set()
        manager.attach(TestNodeBus(lambda node_id: None, True, lambda: in_use))
        test_id = manager.add(_node())["id"]
        await manager.start(test_id)
        in_use.add(60)  # the scanner hears it, and still does for seconds after it stops
        await manager.stop(test_id)
        assert (await manager.start(test_id))["running"]

    async def test_once_a_stopped_node_is_long_gone_its_node_id_counts_again(self, manager, monkeypatch):
        monkeypatch.setattr(testnodes, "_STOPPED_GRACE_S", 0.0)
        in_use = set()
        manager.attach(TestNodeBus(lambda node_id: None, True, lambda: in_use))
        test_id = manager.add(_node())["id"]
        await manager.start(test_id)
        await manager.stop(test_id)
        in_use.add(60)  # another node has it now
        with pytest.raises(RuntimeError, match="Node-ID 60 is in use"):
            await manager.start(test_id)

    async def test_two_test_nodes_cannot_share_a_node_id(self, manager):
        a, b = manager.add(_node("a")), manager.add(_node("b"))
        manager.attach(_bus())
        await manager.start(a["id"])
        with pytest.raises(RuntimeError, match="Test node a already runs on node-ID 60"):
            await manager.start(b["id"])

    async def test_their_rates_are_capped_in_all(self, manager):
        loud = [_running_publisher(100 + i, rate_hz=100.0) for i in range(MAX_PUBLISHERS)]
        a, b = manager.add(_node("a", 60, publishers=loud)), manager.add(_node("b", 61, publishers=loud))
        manager.attach(_bus())
        await manager.start(a["id"])
        with pytest.raises(RuntimeError, match="1000 messages a second in all; with this one they would publish 1600"):
            await manager.start(b["id"])

    async def test_disconnecting_stops_every_one(self, manager, started):
        a, b = manager.add(_node("a", 60)), manager.add(_node("b", 61))
        manager.attach(_bus())
        await manager.start(a["id"])
        await manager.start(b["id"])
        await manager.detach()
        assert all(s.closed for s in started) and manager.running_node_ids() == set() and manager.bus is None
        assert not any(n["running"] for n in manager.list())
        with pytest.raises(RuntimeError, match="Connect CAN first"):
            await manager.start(a["id"])

    async def test_one_the_bus_refuses(self, manager):
        test_id = manager.add(_node("broken"))["id"]
        manager.attach(_bus())
        with pytest.raises(RuntimeError, match="Test node broken did not start: the bus is gone"):
            await manager.start(test_id)
        assert not manager.view(test_id)["running"]

    async def test_one_whose_type_changed_under_it(self, manager):
        test_id = manager.add(_node("outdated"))["id"]
        manager.attach(_bus())
        with pytest.raises(ValueError, match="a number or a waveform"):
            await manager.start(test_id)


class TestNodeIdLeftToCynitor:
    """A test node that sets no node-ID is given one when it starts."""

    async def test_it_is_given_a_free_one(self, manager, started):
        test_id = manager.add(_node(node_id=None))["id"]
        assert manager.view(test_id)["assigned_node_id"] is None  # not before it has started
        manager.attach(_bus(in_use={100, 101}))  # Cynitor's own, and a node on the bus
        view = await manager.start(test_id)
        assert (view["node_id"], view["assigned_node_id"], view["running"]) == (None, 102, True)
        assert started[0].transport == ("transport", 102) and manager.running_node_ids() == {102}
        assert manager.identify(102, None) == test_id

    async def test_not_one_another_test_node_is_set_to_or_was_given(self, manager):
        manager.add(_node("set to 100", node_id=100))  # not running, but 100 is its
        a, b = manager.add(_node("a", node_id=None)), manager.add(_node("b", node_id=None))
        manager.attach(_bus())
        assert (await manager.start(a["id"]))["assigned_node_id"] == 101
        await manager.stop(a["id"])
        assert (await manager.start(b["id"]))["assigned_node_id"] == 102  # 101 is still a's

    async def test_below_100_once_all_above_are_taken(self, manager):
        test_id = manager.add(_node(node_id=None))["id"]
        manager.attach(_bus(in_use=set(range(100, 126)) | {1}))
        assert (await manager.start(test_id))["assigned_node_id"] == 2

    async def test_none_is_free(self, manager, started):
        test_id = manager.add(_node(node_id=None))["id"]
        manager.attach(_bus(in_use=set(range(1, 126))))
        with pytest.raises(RuntimeError, match="No node-ID is free"):
            await manager.start(test_id)
        assert started == [] and manager.view(test_id)["assigned_node_id"] is None

    async def test_it_keeps_the_one_it_had_from_run_to_run(self, manager):
        in_use = set()
        manager.attach(TestNodeBus(lambda node_id: None, True, lambda: in_use))
        test_id = manager.add(_node(node_id=None))["id"]
        assert (await manager.start(test_id))["assigned_node_id"] == 100
        await manager.stop(test_id)
        in_use.add(101)
        assert (await manager.start(test_id))["assigned_node_id"] == 100
        # Kept with the definition: Cynitor started again gives it the same.
        again = TestNodeManager(manager.path)
        again.attach(_bus())
        assert again.view(test_id)["assigned_node_id"] == 100
        assert (await again.start(test_id))["assigned_node_id"] == 100

    async def test_and_another_once_a_node_on_the_bus_has_taken_it(self, manager, monkeypatch):
        monkeypatch.setattr(testnodes, "_STOPPED_GRACE_S", 0.0)
        in_use = set()
        manager.attach(TestNodeBus(lambda node_id: None, True, lambda: in_use))
        test_id = manager.add(_node(node_id=None))["id"]
        await manager.start(test_id)
        await manager.stop(test_id)
        in_use.add(100)  # another node has it now
        assert (await manager.start(test_id))["assigned_node_id"] == 101

    async def test_a_change_that_restarts_it_keeps_its_node_id(self, manager, started):
        manager.attach(_bus())
        test_id = manager.add(_node(node_id=None))["id"]
        await manager.start(test_id)
        view = await manager.replace(test_id, _node(node_id=None, publishers=[_running_publisher()]))
        assert view["assigned_node_id"] == 100 and started[1].transport == ("transport", 100)

    async def test_health_changes_as_it_runs_on_the_node_id_it_was_given(self, manager, started):
        manager.attach(_bus())
        test_id = manager.add(_node(node_id=None))["id"]
        await manager.start(test_id)
        await manager.replace(test_id, _node(node_id=None, health="warning"))
        assert len(started) == 1 and started[0].definition["health"] == "warning"
        assert manager.running_node_ids() == {100} and manager.identify(100, None) == test_id

    async def test_setting_a_node_id_forgets_the_one_given(self, manager):
        manager.attach(_bus())
        test_id = manager.add(_node(node_id=None))["id"]
        await manager.start(test_id)
        await manager.stop(test_id)
        view = await manager.replace(test_id, _node(node_id=70))
        assert (view["node_id"], view["assigned_node_id"]) == (70, None)


class TestChangesWhileRunning:
    @pytest.fixture
    async def running(self, manager) -> str:
        test_id = manager.add(_node())["id"]
        manager.attach(_bus(in_use={61}))
        await manager.start(test_id)
        return test_id

    async def test_health_mode_and_status_change_as_it_runs(self, manager, started, running):
        view = await manager.replace(running, _node(health="warning", mode="maintenance", vendor_status=7))
        assert len(started) == 1 and view["running"]
        assert (started[0].definition["health"], started[0].definition["mode"]) == ("warning", "maintenance")

    async def test_other_changes_restart_it(self, manager, started, running):
        await manager.replace(running, _node(publishers=[_running_publisher()]))
        assert len(started) == 2 and started[0].closed and not started[1].closed
        assert started[1].definition["publishers"] == [_running_publisher()]

    async def test_a_restart_needs_a_free_node_id(self, manager, started, running):
        with pytest.raises(RuntimeError, match="Node-ID 61 is in use"):
            await manager.replace(running, _node(node_id=61))
        assert manager.view(running)["node_id"] == 60 and not started[0].closed

    async def test_deleting_it_stops_it(self, manager, started, running):
        await manager.delete(running)
        assert started[0].closed and manager.running_node_ids() == set()

    async def test_a_stopped_one_is_checked_when_it_starts(self, manager, running):
        await manager.stop(running)
        assert (await manager.replace(running, _node(node_id=61)))["node_id"] == 61
        with pytest.raises(RuntimeError, match="Node-ID 61 is in use"):
            await manager.start(running)


class TestIdentify:
    async def test_by_unique_id_or_by_the_node_id_it_runs_on(self, manager):
        added = manager.add(_node())
        assert manager.identify(12, added["unique_id"]) == added["id"]  # stopped, and on another node-ID since
        assert manager.identify(60, None) is None
        manager.attach(_bus())
        await manager.start(added["id"])
        assert manager.identify(60, None) == added["id"]  # before the scanner has its unique-ID
        assert manager.identify(61, "00" * 16) is None


class TestAPI:
    @pytest.fixture
    def session(self, manager) -> SimpleNamespace:
        return SimpleNamespace(test_nodes=manager, can_interface=None, telemetry=None, simulation=None)

    @pytest.fixture
    async def client(self, session, dsdl):
        server = WebSocketServer(session=session, host="127.0.0.1", port=0, dsdl_manager=dsdl)
        async with TestClient(TestServer(server.app)) as c:
            yield c

    async def test_on_the_simulated_bus_one_starts_while_taking_no_commands(self, client, session):
        # Nothing real can move there, so the session's no-commands mode does not stop it.
        made = await (await client.post("/api/testnodes", json={"name": "probe"})).json()
        session.test_nodes.attach(_bus(simulated=True))
        session.simulation, session.no_commands = object(), True
        resp = await client.post(f"/api/testnodes/{made['id']}/start")
        assert resp.status == 200 and (await resp.json())["running"]

    async def test_one_with_no_node_id_is_given_one_when_it_starts(self, client, session):
        resp = await client.post("/api/testnodes", json={"name": "probe"})
        made = await resp.json()
        assert resp.status == 201 and (made["node_id"], made["assigned_node_id"]) == (None, None)
        session.test_nodes.attach(_bus(in_use={100}))
        started = await (await client.post(f"/api/testnodes/{made['id']}/start")).json()
        assert (started["running"], started["node_id"], started["assigned_node_id"]) == (True, None, 101)
        listing = await (await client.get("/api/testnodes")).json()
        assert listing["test_nodes"][0]["assigned_node_id"] == 101

    async def test_a_test_nodes_life(self, client, session):
        listing = await (await client.get("/api/testnodes")).json()
        assert (listing["test_nodes"], listing["bus"]) == ([], None)
        assert listing["limits"]["rate_hz"] == [0.01, 100.0] and listing["waves"][0] == "sine"

        sine = {"value": _wave("sine", 280, 300, 60)}
        resp = await client.post("/api/testnodes", json={
            "name": "probe", "node_id": 60, "publishers": [{"subject_id": 100, "type": REAL32, "value": sine}]})
        assert resp.status == 201
        made = await resp.json()
        test_id = made["id"]
        assert (made["publishers"][0]["port"], made["publishers"][0]["value"]) == ("scalar_real32", sine)

        resp = await client.post(f"/api/testnodes/{test_id}/start")
        assert resp.status == 409 and "Connect CAN first" in (await resp.json())["error"]

        session.test_nodes.attach(_bus(simulated=False))
        session.can_interface = "pcan:PCAN_USBBUS1"
        resp = await client.post(f"/api/testnodes/{test_id}/start")
        assert resp.status == 409 and (await resp.json())["confirm"] == "real_bus"
        session.no_commands = True  # a session that takes no commands starts none on a real bus (F-024)
        resp = await client.post(f"/api/testnodes/{test_id}/start", json={"confirm_real_bus": True})
        assert resp.status == 409 and "takes no commands" in (await resp.json())["error"]
        session.no_commands = False
        resp = await client.post(f"/api/testnodes/{test_id}/start", json={"confirm_real_bus": True})
        assert resp.status == 200 and (await resp.json())["running"]
        listing = await (await client.get("/api/testnodes")).json()
        assert listing["bus"] == {"simulated": False, "interface": "pcan:PCAN_USBBUS1"}

        resp = await client.put(f"/api/testnodes/{test_id}", json={"name": "probe", "node_id": 60, "health": "warning"})
        assert resp.status == 200 and (await resp.json())["health"] == "warning"
        resp = await client.post(f"/api/testnodes/{test_id}/stop")
        assert resp.status == 200 and not (await resp.json())["running"]

        # Deleted, it is forgotten as a device too: it stayed as an offline one (F-015).
        session.scanner = MagicMock()
        session.event_logger = MagicMock(delete_node_data=AsyncMock())
        resp = await client.delete(f"/api/testnodes/{test_id}")
        assert resp.status == 200 and await resp.json() == {"deleted": test_id}
        session.scanner.identity_map.remove_identity.assert_called_once_with(made["unique_id"])
        session.event_logger.delete_node_data.assert_awaited_once_with(made["unique_id"])
        assert (await client.delete(f"/api/testnodes/{test_id}")).status == 404
        assert (await client.post(f"/api/testnodes/{test_id}/start")).status == 404

    @pytest.mark.parametrize("body, message", [
        ([], "JSON body must be an object"),
        ({"name": "n", "node_id": 300}, "node_id: a whole number from 1 to 125"),
        ({"name": "n", "node_id": 60, "publishers": [{"subject_id": 1, "type": "demo.Nope.1.0"}]},
         "There is no DSDL type demo.Nope.1.0"),
        ({"name": "n", "node_id": 60, "publishers": [{"subject_id": 1, "type": "demo.Ping.1.0"}]},
         "demo.Ping.1.0 is a service type"),
        ({"name": "n", "node_id": 60, "publishers": [{"subject_id": 1, "type": "demo.Uncompiled.1.0"}]},
         "demo.Uncompiled.1.0 is not compiled"),
        ({"name": "n", "node_id": 60, "publishers": [{"subject_id": 1, "type": "demo.Reading.1.0",
                                                      "value": {"level": "high"}}]},
         "publishers[0].value.level: a number or a waveform"),
    ])
    async def test_bad_definitions(self, client, body, message):
        resp = await client.post("/api/testnodes", json=body)
        assert resp.status == 400 and message in (await resp.json())["error"]

    async def test_confirmation_is_a_boolean(self, client):
        test_id = (await (await client.post("/api/testnodes", json={"name": "n", "node_id": 60})).json())["id"]
        resp = await client.post(f"/api/testnodes/{test_id}/start", json={"confirm_real_bus": "yes"})
        assert resp.status == 400

    async def test_a_message_types_value_schema(self, client):
        resp = await client.get("/api/dsdl/value-schema/demo.Reading.1.0")
        assert resp.status == 200 and (await resp.json())["type"] == "demo.Reading.1.0"
        resp = await client.get("/api/dsdl/value-schema/demo.Ping.1.0")
        assert resp.status == 400 and "service type" in (await resp.json())["error"]

    async def test_the_node_list_marks_test_nodes(self, client, session):
        added = session.test_nodes.add(_node())
        nodes = {"60": {"node_id": 60, "unique_id_hex": added["unique_id"]},
                 "61": {"node_id": 61, "unique_id_hex": "00" * 16}}
        session.telemetry = SimpleNamespace(get_all_nodes_info=lambda: {"node_count": 2, "nodes": nodes})
        listed = (await (await client.get("/api/nodes")).json())["nodes"]
        assert listed["60"]["test_node"] == added["id"] and "test_node" not in listed["61"]
