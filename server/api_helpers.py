"""What the REST API's handlers share, whichever module they are written in."""

import logging

from aiohttp import web

# One name for the API's log, as when all its handlers were in websocket_server.py.
logger = logging.getLogger("websocket_server")

MAX_NODE_ID = 127
MAX_SUBJECT_ID = 8191
MAX_SERVICE_ID = 511


def _parse_int(value: str, name: str, lo: int = 0, hi: int = MAX_NODE_ID):
    try:
        v = int(value)
    except (ValueError, TypeError):
        return None, web.json_response({"error": f"Invalid {name}"}, status=400)
    if v < lo or v > hi:
        return None, web.json_response({"error": f"{name} must be {lo}–{hi}"}, status=400)
    return v, None
