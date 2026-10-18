"""Firmware updates: the files kept, and a node told to update from one (see firmware.py).

Handlers of the REST API, which WebSocketServer (websocket_server.py) inherits and routes to."""

from aiohttp import web

from api_helpers import ApiHandlers, _parse_int, listen_only_refusal, MAX_NODE_ID
from firmware import firmware_path, list_firmware, MAX_FIRMWARE_BYTES, MAX_FIRMWARE_FOLDER_BYTES


class FirmwareApi(ApiHandlers):
    """The firmware part of WebSocketServer, which has the session and the rest these use."""

    async def _get_firmware(self, request: web.Request) -> web.Response:
        firmware, scanner = self.session.firmware, self.session.scanner
        updates = {str(nid): {**u, "mode": scanner.node_mode(nid)}
                   for nid, u in firmware.updates.items()} if firmware else {}
        return web.json_response({"files": list_firmware(self.session.firmware_folder), "updates": updates})

    async def _upload_firmware(self, request: web.Request) -> web.Response:
        """The request body is the file; ?name= names it, replacing any file of that name."""
        name = request.query.get("name", "")
        path = firmware_path(self.session.firmware_folder, name)
        if path is None:
            return web.json_response(
                {"error": "name must be a plain file name: letters, digits, '.', '_' or '-'"}, status=400)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f".{path.name}.part")  # not a firmware name until complete
        # The folder's other files, which this one may not push over the quota.
        others = sum(f.stat().st_size for f in path.parent.iterdir() if f.is_file() and f.name != path.name)
        size = 0
        try:
            with partial.open("wb") as f:
                async for chunk in request.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > MAX_FIRMWARE_BYTES:
                        raise ValueError
                    if others + size > MAX_FIRMWARE_FOLDER_BYTES:
                        raise OverflowError
                    f.write(chunk)
            if size == 0:
                raise ValueError
            partial.replace(path)
        except ValueError:
            partial.unlink(missing_ok=True)
            return web.json_response(
                {"error": f"A firmware file holds 1 byte to {MAX_FIRMWARE_BYTES // (1024 * 1024)} MiB"},
                status=413 if size else 400)
        except OverflowError:
            partial.unlink(missing_ok=True)
            return web.json_response(
                {"error": f"The firmware files hold {MAX_FIRMWARE_FOLDER_BYTES // (1024 * 1024)} MiB together: "
                          "delete one first"}, status=413)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return web.json_response({"name": name, "bytes": size}, status=201)

    async def _delete_firmware(self, request: web.Request) -> web.Response:
        path = firmware_path(self.session.firmware_folder, request.match_info["name"])
        if path is None or not path.is_file():
            return web.json_response({"error": "No such firmware file"}, status=404)
        path.unlink()
        return web.json_response({"deleted": path.name})

    async def _update_firmware(self, request: web.Request) -> web.Response:
        """Tell a node to update its software from an uploaded file."""
        node_id, err = _parse_int(request.match_info.get('node_id'), 'node_id', 0, MAX_NODE_ID)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        name = body.get("file") if isinstance(body, dict) else None
        if not isinstance(name, str):
            return web.json_response({"error": "file is required"}, status=400)
        if refused := listen_only_refusal(self.session):
            return refused
        try:
            update = await self.session.begin_firmware_update(node_id, name)
        except FileNotFoundError as e:
            return web.json_response({"error": str(e)}, status=404)
        except TimeoutError as e:
            return web.json_response({"error": str(e)}, status=504)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=409)
        await self._note_in_node_history(node_id, "firmware_update", {"file": name})
        return web.json_response(update)
