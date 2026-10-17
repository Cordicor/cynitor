"""CAN: status, adapters, connecting and disconnecting, and what the bus's transport and frames show.

Handlers of the REST API, which WebSocketServer (websocket_server.py) inherits and routes to."""

import asyncio

from aiohttp import web

from api_helpers import logger
from bus_errors import BusErrors
from can_config import api_interface_error, is_explicit_spec, is_socketcan, resolve_bitrate, resolve_data_bitrate, socketcan_device
from cyphal_v11 import V11Traffic


class CanApi:
    """The can part of WebSocketServer, which has the session and the rest these use."""

    async def _list_adapters(self, refresh: bool = False) -> list[dict]:
        if self.adapter_catalog is None:
            return []
        # While connected, the last list is served as is: probing a USB
        # adapter the session holds tells nothing new and may disturb it.
        adapters = await asyncio.to_thread(
            self.adapter_catalog.get, refresh, not self.session.is_running,
        )
        return [adapter.as_dict() for adapter in adapters]

    async def _get_status(self, request: web.Request) -> web.Response:
        from main import discover_can_interfaces
        bus_load = self.session.bus_load
        available = await asyncio.to_thread(discover_can_interfaces)
        return web.json_response({
            "status": "running" if self.session.is_running else "idle",
            "can_interface": self.session.can_interface,
            # None for SocketCAN, whose bitrate the kernel sets.
            "can_bitrate": self.session.can_bitrate,
            # The CAN FD data bitrate Cynitor opened the adapter with, or None.
            "can_data_bitrate": self.session.can_data_bitrate,
            # Whether the session runs CAN FD (for SocketCAN, as the interface is set up).
            "can_fd": self.session.can_fd,
            "available_interfaces": available,
            "available_adapters": await self._list_adapters(),
            "bus_utilization": bus_load.utilization if bus_load else None,
            "dropped": self.session.dropped_events(),
            # Why history and recordings are not being written (a full disk), until a write works again.
            "history_error": getattr(getattr(self.session, "event_logger", None), "write_error", None),
            "last_error": self.session.last_error,
            "cyphal_v11": v11.status() if isinstance(v11 := getattr(self.session, "v11", None), V11Traffic) else None,
            "bus_errors": (errors.status() if isinstance(errors := getattr(self.session, "bus_errors", None), BusErrors)
                           else None),
            # Cynitor's node-ID allocator, while it is on; None while it is off.
            "allocator": self.session.allocator_status(),
            # The server's CPU use in percent of one core; None in its first second.
            "server_cpu": self.cpu.read(),
            # Whether only the subjects watched or recorded are decoded (POST /api/decoding).
            "decode_on_demand": self.session.decode_on_demand,
            # Whether the session that runs sends nothing on the bus (connect's listen_only).
            "listen_only": self.session.is_running and self.session.listen_only is True,
            # A newer release, {"version", "url"}, if the update check found one; else None.
            "update": self.update_check.available if self.update_check is not None else None,
        })

    async def _set_decoding(self, request: web.Request) -> web.Response:
        """Decode on demand, or every subject: {"on_demand": true|false}. Takes effect at once."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        on_demand = body.get("on_demand") if isinstance(body, dict) else None
        if not isinstance(on_demand, bool):
            return web.json_response({"error": "Field 'on_demand' must be true or false"}, status=400)
        await self.session.set_decode_on_demand(on_demand)
        return web.json_response({"decode_on_demand": on_demand})

    async def _get_adapters(self, request: web.Request) -> web.Response:
        refresh = request.query.get("refresh", "").lower() in ("1", "true", "yes")
        return web.json_response({"adapters": await self._list_adapters(refresh=refresh)})

    async def _can_connect(self, request: web.Request) -> web.Response:
        from main import discover_can_interfaces
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        iface = payload.get("interface", "").strip() if isinstance(payload, dict) else ""
        if not iface:
            return web.json_response({"error": "Field 'interface' required"}, status=400)

        # Required for every adapter except SocketCAN; the server's --bitrate,
        # if it was started with one, stands in when the request has none.
        bitrate = payload.get("bitrate")
        # Optional; opens the adapter as CAN FD. --data-bitrate stands in likewise.
        data_bitrate = payload.get("data_bitrate")
        try:
            resolve_bitrate(iface, self.session.default_bitrate if bitrate is None else bitrate)
            resolve_data_bitrate(iface, self.session.default_data_bitrate if data_bitrate is None else data_bitrate)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        # Optional; whether Cynitor gives plug-and-play nodes their node-IDs
        # in this session. --allocator stands in when the request says neither.
        allocator = payload.get("allocator")
        if allocator is not None and not isinstance(allocator, bool):
            return web.json_response({"error": "Field 'allocator' must be true or false"}, status=400)
        # Optional; the session sends nothing. --listen-only stands in likewise.
        listen_only = payload.get("listen_only")
        if listen_only is not None and not isinstance(listen_only, bool):
            return web.json_response({"error": "Field 'listen_only' must be true or false"}, status=400)

        if is_explicit_spec(iface) and not self.allow_any_interface and (refused := api_interface_error(iface)):
            return web.json_response({"error": refused}, status=400)

        if self.session.is_running:
            return web.json_response({"error": "Already connected"}, status=409)

        # Only bare SocketCAN names can be checked against discovery. A spec
        # such as "gs_usb:0" names an adapter that discovery does not list yet
        # (and never does off Linux), so opening it is the only real check.
        if not is_explicit_spec(iface):
            available = await asyncio.to_thread(discover_can_interfaces)
            if iface not in available:
                return web.json_response(
                    {"error": f"Unknown interface: {iface}", "available_interfaces": available},
                    status=400,
                )

        try:
            await self.session.connect(iface, bitrate=bitrate, data_bitrate=data_bitrate, allocator=allocator,
                                       listen_only=listen_only)
            return web.json_response({"status": "running", "can_interface": iface,
                                      "can_fd": self.session.can_fd,
                                      "allocator": self.session.allocator_status(),
                                      "listen_only": self.session.listen_only is True})
        except Exception as e:
            logger.error(f"Failed to connect CAN: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def _can_disconnect(self, request: web.Request) -> web.Response:
        from main import discover_can_interfaces
        if not self.session.is_running:
            return web.json_response({"error": "Not connected"}, status=409)

        await self.session.disconnect()
        available = await asyncio.to_thread(discover_can_interfaces)
        return web.json_response({
            "status": "idle",
            "available_interfaces": available,
            "available_adapters": await self._list_adapters(refresh=True),
        })

    async def _get_transport_diagnostics(self, request: web.Request) -> web.Response:
        """Transport-layer ("below the DSDL") diagnostics snapshot.

        Returns ``{"connected": false}`` when no CAN session is active so the
        Debugging view can render an idle state instead of erroring. When
        connected, combines pycyphal transport counters/protocol params (cheap,
        in-process) with controller/bus-state details parsed from iproute2
        (run off the event loop via to_thread).
        """
        from main import get_can_link_diagnostics
        if not self.session.is_running or self.session.scanner is None:
            return web.json_response({"connected": False})

        info = self.session.scanner.get_transport_info()
        capture = self.session.frame_capture
        iface = self.session.can_interface
        # iproute2 only knows SocketCAN devices; an adapter behind the hub
        # reports what the hub knows about it instead.
        if self.session.hub is not None:
            link = self.session.hub.link_diagnostics()
        elif iface and is_socketcan(iface):
            link = await asyncio.to_thread(get_can_link_diagnostics, socketcan_device(iface))
        else:
            link = {}
        bus_load = self.session.bus_load
        return web.json_response({
            "connected": True,
            "interface": iface,
            "protocol": info.get("protocol"),
            "statistics": info.get("statistics"),
            "capture_active": capture is not None and capture.active,
            "link": link,
            "bus_utilization": bus_load.utilization if bus_load else None,
        })

    async def _get_capture(self, request: web.Request) -> web.Response:
        """Snapshot of the raw frame-capture ring buffer + counters, for scripts.

        The Debugging view gets the same frames with its ``capture_status``
        reply. Returns an inactive empty result when no CAN session exists.
        Live frames arrive via the WebSocket ``can_frame`` stream.
        """
        mgr = self.session.frame_capture
        if mgr is None:
            return web.json_response({"active": False, "stats": None, "frames": []})
        try:
            limit = min(int(request.query.get("limit", "500")), 2000)
        except (ValueError, TypeError):
            limit = 500
        return web.json_response({
            "active": mgr.active,
            "stats": mgr.stats(),
            "frames": mgr.snapshot(limit),
        })
