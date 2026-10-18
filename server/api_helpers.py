"""What the REST API's handlers share, whichever module they are written in."""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

# One name for the API's log, as when all its handlers were in websocket_server.py.
logger = logging.getLogger("websocket_server")

MAX_NODE_ID = 127
MAX_SUBJECT_ID = 8191
MAX_SERVICE_ID = 511


class ApiHandlers:
    """The base of every handler mixin: what they use of WebSocketServer, which inherits them all and sets these."""

    session: Any  # main.CANSession
    dsdl_manager: Any  # dsdl_manager.DsdlManager, or None
    adapter_catalog: Any  # can_discovery.AdapterCatalog, or None
    cpu: Any  # ProcessCpu
    update_check: Any  # update_check.UpdateCheck, or None
    allow_any_interface: bool
    _dsdl_compile_lock: asyncio.Lock
    # NodesApi's, used by the other handlers too
    _note_in_node_history: Callable[[int, str, dict], Awaitable[None]]
    _forget_device: Callable[[Optional[str]], Awaitable[None]]

    async def _read_json_body(self, request: web.Request) -> tuple[dict, Optional[web.Response]]:
        """The request's JSON object, and None; or an empty dict and the 400 to answer."""
        try:
            body = await request.json()
        except Exception:
            return {}, web.json_response({"error": "Invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return {}, web.json_response({"error": "JSON body must be an object"}, status=400)
        return body, None


def listen_only_refusal(session) -> "web.Response | None":
    """409 for a request that would send on the bus while the session listens only; else None."""
    if getattr(session, "listen_only", False) is True:
        return web.json_response({"error": "The session listens only: Cynitor sends nothing on the bus. "
                                           "Connect without Listen only to ask nodes anything."}, status=409)
    return None


def _parse_int(value: Optional[str], name: str, lo: int = 0, hi: int = MAX_NODE_ID):
    try:
        v = int(value)  # type: ignore[arg-type]  # None raises TypeError, answered below
    except (ValueError, TypeError):
        return None, web.json_response({"error": f"Invalid {name}"}, status=400)
    if v < lo or v > hi:
        return None, web.json_response({"error": f"{name} must be {lo}–{hi}"}, status=400)
    return v, None
