"""Cynitor's plug-and-play node-ID allocator, for a bus that has none of its own.

Off unless asked for: with --allocator, with "allocator" in POST
/api/can/connect, or in the dashboard's connect form. A bus may have one
allocator only, and one cannot be told from any other node until it answers a
request, so whether Cynitor is to be it is for whoever knows the bus to say.

It runs on the scanner's node, so it takes no node-ID of its own. A node that
asks is given a node-ID nothing has used on the bus this session, and the same
one if it asks again. What was given is forgotten when CAN disconnects: the
table belongs to the bus, and the next one Cynitor is plugged into may be
another.

Should another allocator answer a request, this one answers no more.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional, Union

import uavcan.node
import uavcan.pnp

logger = logging.getLogger(__name__)

# The highest node-IDs of a network are left to diagnostic tools, as in pycyphal's allocator.
RESERVED_TOP_NODE_IDS = 2
# The answers keep their worth, so a long wait for the bus does them no harm.
ANSWER_SEND_TIMEOUT_S = 5.0


def nearest_free_node_id(preferred: int, highest: int, taken: set[int]) -> Optional[int]:
    """The node-ID not in ``taken`` nearest ``preferred`` going up to ``highest``, else going down; None if none is free."""
    preferred = min(max(preferred, 0), highest)
    for node_id in (*range(preferred, highest + 1), *range(preferred - 1, -1, -1)):
        if node_id not in taken:
            return node_id
    return None


class NodeIdAllocator:
    """Answers the node-ID requests heard on ``node``, with node-IDs outside ``node_ids_taken()``."""

    def __init__(self, node, node_ids_taken: Callable[[], set[int]]) -> None:
        self._node_ids_taken = node_ids_taken
        # Unique-ID, or its hash as a Classic CAN request carries it -> the node-ID given.
        self._allocated: dict[Union[int, bytes], int] = {}
        # The node-ID of another allocator heard on the bus; this one is silent from then on.
        self.other_allocator: Optional[int] = None
        self._highest = node.presentation.transport.protocol_parameters.max_nodes - 1 - RESERVED_TOP_NODE_IDS
        # Version 1 of the message fits a Classic CAN frame; version 2 is what CAN FD nodes send.
        self._answers_v1 = node.make_publisher(uavcan.pnp.NodeIDAllocationData_1_0)
        self._answers_v2 = node.make_publisher(uavcan.pnp.NodeIDAllocationData_2_0)
        requests_v1 = node.make_subscriber(uavcan.pnp.NodeIDAllocationData_1_0)
        requests_v2 = node.make_subscriber(uavcan.pnp.NodeIDAllocationData_2_0)
        self._ports = (self._answers_v1, self._answers_v2, requests_v1, requests_v2)
        for answers in (self._answers_v1, self._answers_v2):
            answers.send_timeout = ANSWER_SEND_TIMEOUT_S
        requests_v1.receive_in_background(self._on_v1)
        requests_v2.receive_in_background(self._on_v2)
        logger.info("Allocator started: node %d gives node-IDs to the nodes that ask for one", node.id)

    def status(self) -> dict:
        """For /api/status: the other allocator that silenced this one, if any, and how many node-IDs it gave."""
        return {"other_allocator": self.other_allocator, "allocated": len(self._allocated)}

    def close(self) -> None:
        for port in self._ports:
            port.close()
        logger.info("Allocator stopped")

    async def _on_v1(self, msg, transfer) -> None:
        if not self._is_request_to_answer(transfer):
            return
        node_id = self._allocate(int(msg.unique_id_hash), self._highest)
        if node_id is not None:
            self._answer(self._answers_v1, transfer, uavcan.pnp.NodeIDAllocationData_1_0(
                unique_id_hash=msg.unique_id_hash, allocated_node_id=[uavcan.node.ID_1_0(node_id)]))

    async def _on_v2(self, msg, transfer) -> None:
        if not self._is_request_to_answer(transfer):
            return
        node_id = self._allocate(msg.unique_id.tobytes(), int(msg.node_id.value))  # it names the one it would like
        if node_id is not None:
            self._answer(self._answers_v2, transfer, uavcan.pnp.NodeIDAllocationData_2_0(
                node_id=uavcan.node.ID_1_0(node_id), unique_id=msg.unique_id))

    def _is_request_to_answer(self, transfer) -> bool:
        """A request is anonymous; anything else on these subjects is another allocator answering one."""
        source = transfer.source_node_id
        if source is None:
            return self.other_allocator is None
        if self.other_allocator is None:
            logger.warning(
                "Node %d answers node-ID requests too. A bus has one allocator, so Cynitor's "
                "answers no more: connect without the allocator on this bus", source)
        self.other_allocator = source
        return False

    def _allocate(self, unique_id: Union[int, bytes], preferred: int) -> Optional[int]:
        """The node-ID for the node with ``unique_id``: the one it has, or a free one; None if none is free."""
        node_id = self._allocated.get(unique_id)
        if node_id is None:
            taken = self._node_ids_taken() | set(self._allocated.values())
            node_id = nearest_free_node_id(preferred, self._highest, taken)
            if node_id is None:
                logger.warning("A node asks for a node-ID, and none is free")
                return None
            self._allocated[unique_id] = node_id
            name = unique_id.hex() if isinstance(unique_id, bytes) else f"hash {unique_id:#x}"
            logger.info("Node-ID %d given to the node with unique-ID %s", node_id, name)
        return node_id

    @staticmethod
    def _answer(answers, transfer, message) -> None:
        answers.priority = transfer.priority
        answers.publish_soon(message)
