"""Tests for Cynitor's node-ID allocator (allocator.py): what it gives, and when it keeps quiet.

Run with its messages on a real bus, against pycyphal's own Allocatee, in the
integration test (tests/integration/hub_session.py).
"""

from types import SimpleNamespace

import pytest

import allocator
from allocator import NodeIdAllocator, nearest_free_node_id

HIGHEST = 125  # of a CAN bus's 128 node-IDs, the two highest are left to diagnostic tools


def v1(**fields):
    """uavcan.pnp.NodeIDAllocationData.1.0, as Classic CAN nodes send it."""
    return SimpleNamespace(**fields)


def v2(**fields):
    """uavcan.pnp.NodeIDAllocationData.2.0, as CAN FD nodes send it."""
    return SimpleNamespace(**fields)


class FakePort:
    """A publisher or a subscriber of the node the allocator runs on."""

    def __init__(self):
        self.sent = []
        self.closed = False
        self.handler = None

    def publish_soon(self, message):
        self.sent.append((message, self.priority))

    def receive_in_background(self, handler):
        self.handler = handler

    def close(self):
        self.closed = True


class Bus:
    """A node with the allocator on it, and the nodes that ask it for a node-ID."""

    def __init__(self, seen=()):
        self.seen = set(seen)  # node-IDs heard on the bus
        self.publishers = {v1: FakePort(), v2: FakePort()}
        self.subscribers = {v1: FakePort(), v2: FakePort()}
        node = SimpleNamespace(
            id=10,
            presentation=SimpleNamespace(transport=SimpleNamespace(protocol_parameters=SimpleNamespace(max_nodes=128))),
            make_publisher=self.publishers.get, make_subscriber=self.subscribers.get,
        )
        self.allocator = NodeIdAllocator(node, lambda: set(self.seen))

    async def hear(self, dtype, message, source=None, priority="nominal"):
        """Deliver ``message``: from ``source``, or anonymous as a request is. Returns the answers so far."""
        await self.subscribers[dtype].handler(message, SimpleNamespace(source_node_id=source, priority=priority))
        return self.publishers[dtype].sent

    async def ask(self, unique_id_hash):
        """A Classic CAN node asks; the node-ID in the answer, or None if there was none."""
        before = len(self.publishers[v1].sent)
        answers = await self.hear(v1, v1(unique_id_hash=unique_id_hash, allocated_node_id=[]))
        return answers[-1][0].allocated_node_id[0].value if len(answers) > before else None

    async def ask_fd(self, unique_id: bytes, preferred: int):
        """A CAN FD node asks, naming the node-ID it would like; the node-ID in the answer, or None."""
        before = len(self.publishers[v2].sent)
        request = v2(unique_id=SimpleNamespace(tobytes=lambda: unique_id), node_id=SimpleNamespace(value=preferred))
        answers = await self.hear(v2, request)
        return answers[-1][0].node_id.value if len(answers) > before else None


@pytest.fixture(autouse=True)
def messages(monkeypatch):
    """Messages the tests can read: the DSDL types are mocks here (see conftest)."""
    monkeypatch.setattr(allocator, "uavcan", SimpleNamespace(
        node=SimpleNamespace(ID_1_0=lambda value: SimpleNamespace(value=value)),
        pnp=SimpleNamespace(NodeIDAllocationData_1_0=v1, NodeIDAllocationData_2_0=v2),
    ))


class TestNearestFreeNodeId:
    def test_the_preferred_one_if_it_is_free(self):
        assert nearest_free_node_id(50, HIGHEST, {49, 51}) == 50

    def test_else_the_next_one_up(self):
        assert nearest_free_node_id(50, HIGHEST, {50, 51}) == 52

    def test_else_the_next_one_down(self):
        assert nearest_free_node_id(124, HIGHEST, {124, 125}) == 123

    def test_never_above_the_highest(self):
        assert nearest_free_node_id(127, HIGHEST, set()) == HIGHEST

    def test_none_when_all_are_taken(self):
        assert nearest_free_node_id(50, HIGHEST, set(range(HIGHEST + 1))) is None


class TestGivingNodeIds:
    async def test_a_node_that_asks_is_answered_with_its_hash_at_its_priority(self):
        bus = Bus()
        answers = await bus.hear(v1, v1(unique_id_hash=0xABC, allocated_node_id=[]), priority="slow")
        (answer, priority), = answers
        assert answer.unique_id_hash == 0xABC and answer.allocated_node_id[0].value == HIGHEST
        assert priority == "slow"

    async def test_no_node_id_heard_on_the_bus_is_given(self):
        # The static node at 125 that pycyphal's allocator, told nothing, would be given away.
        bus = Bus(seen={125, 124, 10})
        assert await bus.ask(0xABC) == 123

    async def test_each_node_gets_its_own(self):
        bus = Bus()
        assert [await bus.ask(0xA), await bus.ask(0xB)] == [125, 124]

    async def test_a_node_that_asks_again_gets_the_one_it_had(self):
        bus = Bus()
        given = await bus.ask(0xA)
        bus.seen.add(given)  # it came online with it, then restarted
        await bus.ask(0xB)
        assert await bus.ask(0xA) == given

    async def test_a_can_fd_node_gets_the_one_it_would_like(self):
        bus = Bus()
        unique_id = bytes(range(16))
        assert await bus.ask_fd(unique_id, 42) == 42
        assert bus.publishers[v2].sent[0][0].unique_id.tobytes() == unique_id  # the answer names whom it is for

    async def test_or_the_nearest_free_one(self):
        bus = Bus(seen={42, 43})
        assert await bus.ask_fd(bytes(16), 42) == 44

    async def test_no_answer_when_none_is_free(self):
        bus = Bus(seen=range(HIGHEST + 1))
        assert await bus.ask(0xA) is None

    async def test_status_counts_what_was_given(self):
        bus = Bus()
        await bus.ask(0xA)
        await bus.ask(0xA)
        await bus.ask(0xB)
        assert bus.allocator.status() == {"other_allocator": None, "allocated": 2}


class TestAnotherAllocator:
    """A bus has one allocator: Cynitor's answers no more once another does."""

    async def test_its_answer_silences_cynitors(self):
        bus = Bus()
        assert await bus.ask(0xA) == 125
        answer = v1(unique_id_hash=0xB, allocated_node_id=[SimpleNamespace(value=124)])
        await bus.hear(v1, answer, source=42)   # another allocator's, not a request
        assert await bus.ask(0xB) is None
        assert await bus.ask(0xA) is None       # not even the node it had answered
        assert await bus.ask_fd(bytes(16), 50) is None
        assert bus.allocator.status() == {"other_allocator": 42, "allocated": 1}

    async def test_an_answer_is_not_answered(self):
        bus = Bus()
        answers = await bus.hear(v1, v1(unique_id_hash=0xB, allocated_node_id=[SimpleNamespace(value=124)]), source=42)
        assert answers == []


def test_close_lets_go_of_its_ports():
    bus = Bus()
    bus.allocator.close()
    assert all(port.closed for port in (*bus.publishers.values(), *bus.subscribers.values()))
