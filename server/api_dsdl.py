"""DSDL: the types loaded, and the user's own, made, edited and compiled (see dsdl_manager.py).

Handlers of the REST API, which WebSocketServer (websocket_server.py) inherits and routes to."""

import asyncio

from aiohttp import web

from api_helpers import ApiHandlers


class DsdlApi(ApiHandlers):
    """The dsdl part of WebSocketServer, which has the session and the rest these use."""

    async def _dsdl_status(self, request: web.Request) -> web.Response:
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        data = await asyncio.to_thread(self.dsdl_manager.get_status)
        return web.json_response(data)

    async def _dsdl_namespaces(self, request: web.Request) -> web.Response:
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        data = await asyncio.to_thread(self.dsdl_manager.get_namespaces)
        return web.json_response(data)

    async def _dsdl_type_detail(self, request: web.Request) -> web.Response:
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        full_name = request.match_info["full_name"]
        data = await asyncio.to_thread(self.dsdl_manager.get_type_detail, full_name)
        if data is None:
            return web.json_response({"error": f"Type not found: {full_name}"}, status=404)
        return web.json_response(data)

    async def _dsdl_create_namespace(self, request: web.Request) -> web.Response:
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        namespace = body.get("namespace", "").strip()
        if not namespace:
            return web.json_response({"error": "namespace is required"}, status=400)
        try:
            data = await asyncio.to_thread(self.dsdl_manager.create_namespace, namespace)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(data, status=201)

    async def _dsdl_delete_namespace(self, request: web.Request) -> web.Response:
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        try:
            data = await asyncio.to_thread(self.dsdl_manager.delete_namespace, request.match_info["namespace"])
        except FileNotFoundError as e:
            return web.json_response({"error": str(e)}, status=404)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(data)

    async def _dsdl_save_type(self, request: web.Request) -> web.Response:
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        namespace = body.get("namespace", "").strip()
        type_name = body.get("type_name", "").strip()
        version = body.get("version", "").strip()
        source_text = body.get("source_text", "")
        port_id = body.get("fixed_port_id")
        overwrite = bool(body.get("overwrite", False))
        if not all([namespace, type_name, version, source_text]):
            return web.json_response({"error": "namespace, type_name, version, source_text are required"}, status=400)
        if overwrite:
            full_name = f"{namespace}.{type_name}.{version}"
            if self.dsdl_manager.is_compiled(full_name):
                return web.json_response(
                    {"error": f"Cannot edit '{full_name}': type is already compiled. Delete it and save it again, or save it under a new version."},
                    status=409,
                )
        try:
            data = await asyncio.to_thread(
                self.dsdl_manager.save_type, namespace, type_name, version, source_text, port_id, overwrite
            )
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(data, status=201)

    async def _dsdl_delete_type(self, request: web.Request) -> web.Response:
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        full_name = request.match_info["full_name"]
        parts = full_name.split(".")
        if len(parts) < 4:
            return web.json_response({"error": "Invalid full name (expected namespace.Type.major.minor)"}, status=400)
        namespace = ".".join(parts[:-3])
        type_name = parts[-3]
        version = f"{parts[-2]}.{parts[-1]}"
        try:
            # Deletion removes the .dsdl source AND any matching compiled .py
            # output, so the type disappears from both the tree and the
            # runtime. The namespace's __init__.py is intentionally left to
            # the next recompile (no clean way to patch it in-place when
            # other types in the namespace might still depend on it).
            data = await asyncio.to_thread(
                self.dsdl_manager.delete_type, namespace, type_name, version
            )
        except FileNotFoundError as e:
            return web.json_response({"error": str(e)}, status=404)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(data)

    async def _dsdl_list_custom_namespaces(self, request: web.Request) -> web.Response:
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        data = await asyncio.to_thread(self.dsdl_manager.list_custom_namespaces)
        return web.json_response({"namespaces": data})

    async def _dsdl_compile(self, request: web.Request) -> web.Response:
        if not self.dsdl_manager:
            return web.json_response({"error": "DSDL manager not available"}, status=503)
        try:
            body = await request.json()
        except Exception:
            body = {}
        scope = body.get("scope", "all")
        async with self._dsdl_compile_lock:
            if scope == "custom":
                data = await asyncio.to_thread(self.dsdl_manager.compile_custom)
            elif scope == "public":
                data = await asyncio.to_thread(self.dsdl_manager.compile_public)
            else:
                data = await asyncio.to_thread(self.dsdl_manager.compile_all)
        if not data["ok"]:
            return web.json_response({"error": data["error"]}, status=422)
        if hasattr(self.session, "rescan_registrations"):
            self.session.rescan_registrations()
        return web.json_response(data)
