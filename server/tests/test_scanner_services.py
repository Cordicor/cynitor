"""Tests for the scanner's service calls (scanner_services.py): how a request is built, and what a failure says.

Composite fields set by name, and the response as JSON, are tested in
test_scanner_node.py (TestServiceCallFields).
"""

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydsdl import CompositeType

import scanner_node
from scanner_node import ScannerNode  # uavcan.* is stubbed in conftest.py


class Primitive:
    """A pydsdl primitive type, of which the scanner reads the name alone."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return self.name


class Point:
    _MODEL_ = types.SimpleNamespace(fields=[types.SimpleNamespace(name="x"), types.SimpleNamespace(name="y")])


class Empty:
    _MODEL_ = types.SimpleNamespace(fields=[])


class Request:
    point, gain, count = None, 0.0, 0   # as a DSDL object has every field, at its default

    _MODEL_ = types.SimpleNamespace(attributes=[
        types.SimpleNamespace(name="point", data_type=MagicMock(spec=CompositeType)),
        types.SimpleNamespace(name="gain", data_type=Primitive("saturated float16")),
        types.SimpleNamespace(name="count", data_type=Primitive("saturated uint8")),
    ])


@pytest.fixture
def service():
    client = MagicMock()
    client.dtype = types.SimpleNamespace(Request=Request)
    client.call = AsyncMock(return_value=(object(), None))
    node = ScannerNode.__new__(ScannerNode)
    node.service_clients = {(42, 100): client}
    module = types.SimpleNamespace(Point_1_0=Point, Empty_1_0=Empty)
    where = sys.modules[ScannerNode.make_service_call.__module__]   # the module the call is written in
    with patch.object(scanner_node.importlib, "import_module", return_value=module), \
            patch.object(where, "to_builtin", return_value={"status": 0}):
        yield node, client


async def call(node, attributes, service_id=100):
    return await node.make_service_call(42, service_id, "Svc_1_0", attributes)


class TestRequest:
    async def test_plain_fields_are_set_as_given_and_a_float16_as_a_float(self, service):
        node, client = service
        await call(node, {"gain": {"value": "2.5"}, "count": {"value": 3}})
        sent = client.call.await_args.args[0]
        assert sent.gain == 2.5 and isinstance(sent.gain, float) and sent.count == 3

    async def test_a_field_not_given_is_left_as_the_type_has_it(self, service):
        node, client = service
        await call(node, {"count": {"value": 3}})
        sent = client.call.await_args.args[0]
        assert (sent.point, sent.gain, sent.count) == (None, 0.0, 3)

    async def test_a_field_the_request_does_not_have_is_passed_over(self, service):
        node, client = service
        await call(node, {"count": {"value": 3}, "nonsense": {"value": 1}})
        assert not hasattr(client.call.await_args.args[0], "nonsense")


class TestWhatAFailureSays:
    """The message goes to the dashboard as it is, so it is part of what the call does."""

    async def refused(self, node, attributes, service_id=100) -> str:
        with pytest.raises(ValueError) as failure:
            await call(node, attributes, service_id)
        return str(failure.value)

    async def test_no_such_service_on_the_node(self, service):
        node, _ = service
        assert await self.refused(node, {}, service_id=101) == "No client found for service 101 on node 42"

    async def test_a_field_the_composite_does_not_have(self, service):
        node, client = service
        message = await self.refused(node, {"point": {"type": "ns.Point.1.0", "value": {"z": 1}}})
        assert message == ("Error processing attribute point: Error processing attribute point: "
                           "Error processing composite attribute point: Point_1_0 has no field 'z'")
        client.call.assert_not_awaited()

    async def test_a_composite_given_without_its_type(self, service):
        node, _ = service
        message = await self.refused(node, {"point": {"value": {"x": 1}}})
        assert message == ("Error processing attribute point: Error processing attribute point: "
                           "Attribute point missing 'type' or 'value'")

    async def test_a_type_that_is_no_dsdl_type_name(self, service):
        node, _ = service
        message = await self.refused(node, {"point": {"type": "Point", "value": {"x": 1}}})
        assert message == ("Error processing attribute point: Error processing attribute point: "
                           "Invalid type format: Point")

    async def test_a_type_that_is_not_compiled(self, service):
        node, _ = service
        message = await self.refused(node, {"point": {"type": "ns.Other.1.0", "value": {"x": 1}}})
        assert message.startswith("Error processing attribute point: Error processing attribute point: "
                                  "Cannot import Other_1_0: ")

    async def test_a_composite_with_no_fields_to_set(self, service):
        node, _ = service
        message = await self.refused(node, {"point": {"type": "ns.Empty.1.0", "value": 1}})
        assert message == ("Error processing attribute point: Error processing attribute point: "
                           "Error processing composite attribute point: No fields defined in Empty_1_0")

    async def test_a_plain_field_given_without_a_value(self, service):
        node, _ = service
        message = await self.refused(node, {"gain": {"type": "saturated float16"}})
        assert message == ("Error processing attribute gain: Error processing attribute gain: "
                           "Attribute gain missing 'value'")

    async def test_a_float16_that_is_no_number(self, service):
        node, _ = service
        message = await self.refused(node, {"gain": {"value": "fast"}})
        assert message.startswith("Error processing attribute gain: Error processing attribute gain: "
                                  "Invalid value fast for attribute gain: ")

    async def test_a_node_that_does_not_answer(self, service):
        # A timeout, which the API answers with 504 "no answer"; it was a generic
        # error, a 500, after pycyphal's own 1 s (F-045).
        node, client = service
        client.call = AsyncMock(return_value=None)
        with pytest.raises(asyncio.TimeoutError, match=r"^Service 100 on node 42 gave no answer in 5s$"):
            await call(node, {})
        assert client.response_timeout == ScannerNode.SERVICE_CALL_TIMEOUT

    async def test_a_call_that_takes_too_long(self, service):
        node, client = service
        client.call = AsyncMock(side_effect=asyncio.TimeoutError)
        with pytest.raises(asyncio.TimeoutError, match=r"^Service 100 on node 42 gave no answer in 5s$"):
            await call(node, {})

    async def test_a_node_dropped_while_the_call_waits(self, service):
        # pycyphal closes the client: its object's repr reached the page.
        import pycyphal.presentation
        node, client = service
        client.call = AsyncMock(side_effect=pycyphal.presentation.PortClosedError("ClientImpl(...)"))
        with pytest.raises(ConnectionError, match="^Node 42 went offline while the call waited for its answer$"):
            await call(node, {})
