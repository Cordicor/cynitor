"""Test nodes: the nodes made in the dashboard's Test nodes tab (see testnodes.py).

Handlers of the REST API, which WebSocketServer (websocket_server.py) inherits and routes to."""

import asyncio
from typing import Optional

from aiohttp import web

from api_helpers import MAX_SUBJECT_ID
from testnodes import (HEALTH, MAX_NODE_ID as MAX_TEST_NODE_ID, MAX_PUBLISHERS, MAX_RATE_HZ, MAX_TEST_NODES,
                       MAX_TOTAL_RATE_HZ, MIN_NODE_ID as MIN_TEST_NODE_ID, MIN_RATE_HZ, MODES, WAVES,
                       ConfirmationRequired, UnknownTestNode, normalize_definition, value_schema)


class TestNodesApi:
    """The testnodes part of WebSocketServer, which has the session and the rest these use."""

    _TEST_NODE_OPTIONS = {
        "health": list(HEALTH), "modes": list(MODES), "waves": list(WAVES),
        "limits": {"test_nodes": MAX_TEST_NODES, "publishers": MAX_PUBLISHERS,
                   "node_id": [MIN_TEST_NODE_ID, MAX_TEST_NODE_ID], "subject_id": [0, MAX_SUBJECT_ID],
                   "rate_hz": [MIN_RATE_HZ, MAX_RATE_HZ], "total_rate_hz": MAX_TOTAL_RATE_HZ},
    }

    async def _get_test_nodes(self, request: web.Request) -> web.Response:
        manager = self.session.test_nodes
        bus = manager.bus
        return web.json_response({
            "test_nodes": manager.list(),
            # Where they would run: None while CAN is not connected.
            "bus": None if bus is None else {"simulated": bus.simulated, "interface": self.session.can_interface},
            **self._TEST_NODE_OPTIONS,
        })

    async def _add_test_node(self, request: web.Request) -> web.Response:
        definition, err = await self._read_test_node(request)
        if err:
            return err
        try:
            return web.json_response(self.session.test_nodes.add(definition), status=201)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=409)

    async def _replace_test_node(self, request: web.Request) -> web.Response:
        definition, err = await self._read_test_node(request)
        if err:
            return err
        return await self._test_node_action(self.session.test_nodes.replace(request.match_info["test_id"], definition))

    async def _delete_test_node(self, request: web.Request) -> web.Response:
        test_id = request.match_info["test_id"]

        async def delete() -> dict:
            unique_id = self.session.test_nodes.view(test_id).get("unique_id")
            await self.session.test_nodes.delete(test_id)
            await self._forget_device(unique_id)  # else it stays as an offline device for good
            return {"deleted": test_id}
        return await self._test_node_action(delete())

    async def _start_test_node(self, request: web.Request) -> web.Response:
        body, err = {}, None
        if request.can_read_body:
            body, err = await self._read_json_body(request)
        if err:
            return err
        confirm = body.get("confirm_real_bus", False)
        if not isinstance(confirm, bool):
            return web.json_response({"error": "confirm_real_bus is true or false"}, status=400)
        return await self._test_node_action(
            self.session.test_nodes.start(request.match_info["test_id"], confirm_real_bus=confirm))

    async def _stop_test_node(self, request: web.Request) -> web.Response:
        return await self._test_node_action(self.session.test_nodes.stop(request.match_info["test_id"]))

    async def _read_test_node(self, request: web.Request) -> tuple[Optional[dict], Optional[web.Response]]:
        """The test node in the request's body, normalized; else the response saying what is wrong."""
        body, err = await self._read_json_body(request)
        if err:
            return None, err
        try:
            # Reads each publisher's type from its source: off the event loop.
            return await asyncio.to_thread(normalize_definition, body, self._message_value_schema), None
        except ValueError as e:
            return None, web.json_response({"error": str(e)}, status=400)

    @staticmethod
    async def _test_node_action(action) -> web.Response:
        """Await ``action``, a test node manager's coroutine, and answer with what it gives or raises."""
        try:
            return web.json_response(await action)
        except UnknownTestNode as e:
            return web.json_response({"error": str(e)}, status=404)
        except ConfirmationRequired as e:
            # The dashboard asks, then starts it again with confirm_real_bus.
            return web.json_response({"error": str(e), "confirm": "real_bus"}, status=409)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=409)

    def _message_value_schema(self, full_name: str) -> dict:
        """The value schema of message type ``full_name``, from its source. ValueError if
        there is no such message type, or it is not compiled, so cannot be published."""
        import pydsdl
        if not self.dsdl_manager:
            raise ValueError("DSDL types are not available")
        model = self.dsdl_manager.read_model(full_name)
        if isinstance(model, pydsdl.ServiceType):
            raise ValueError(f"{full_name} is a service type, not a message type")
        if not self.dsdl_manager.is_compiled(full_name):
            raise ValueError(f"{full_name} is not compiled: compile it in the DSDL tab first")
        return value_schema(model)

    async def _dsdl_value_schema(self, request: web.Request) -> web.Response:
        try:
            schema = await asyncio.to_thread(self._message_value_schema, request.match_info["full_name"])
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(schema)
