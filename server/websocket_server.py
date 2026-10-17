#!/usr/bin/env python3
"""The HTTP and WebSocket server: its routes, who may call them, and the live stream to the dashboards.

The handlers of the REST API are in the api_*.py modules, one a part of the
API, as classes this server inherits; _setup_routes below has every route.
"""

import asyncio
import hmac
import json
import os
from pathlib import Path
from typing import Optional, Set, Any
from urllib.parse import urlsplit
from aiohttp import web, WSCloseCode

from api_can import CanApi
from api_dsdl import DsdlApi
from api_firmware import FirmwareApi
from api_helpers import logger
from api_nodes import NodesApi
from api_rawlogs import RawLogsApi
from api_recordings import RecordingsApi
from api_testnodes import TestNodesApi
from cpu_load import ProcessCpu
from version import __version__


class WebSocketServer(CanApi, RawLogsApi, TestNodesApi, FirmwareApi, NodesApi, RecordingsApi, DsdlApi):
    """
    WebSocket server for telemetry streaming.

    Features:
    - Real-time event streaming to multiple clients
    - Event filtering (by subject_id, node_id, message_type)
    - REST API for querying latest events
    - CAN interface connect/disconnect via REST
    - CORS support for web clients
    """

    # Paths the auth middleware always lets through. /api/health is the only
    # truly open endpoint (used by load balancers and uptime checks).
    _AUTH_OPEN_PATHS = frozenset({"/api/health"})

    _LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

    def __init__(
        self,
        session: Any,
        host: str = "127.0.0.1",
        port: int = 8080,
        log_store: Optional[Any] = None,
        dsdl_manager: Optional[Any] = None,
        auth_token: Optional[str] = None,
        website_dir: Optional[Path] = None,
        adapter_catalog: Optional[Any] = None,
    ) -> None:
        self.session = session
        self.host = host
        self.port = port
        self.log_store = log_store
        self.dsdl_manager = dsdl_manager
        # One DSDL compile at a time: two would write the same files at once.
        self._dsdl_compile_lock = asyncio.Lock()
        # When present, the dashboard is served from this server so a single
        # binary is all a deployment needs. None means API-only, which is what
        # --no-frontend selects and what a checkout without website/ gets.
        self.website_dir = website_dir if website_dir and website_dir.is_dir() else None
        # When set, every request outside _AUTH_OPEN_PATHS must present this
        # token via Authorization: Bearer <token> (REST) or ?token=<token>
        # (WebSocket). When None, the server runs open — same behaviour as
        # before this option was added.
        self.auth_token = auth_token or None
        # can_discovery.AdapterCatalog: the adapters the dashboard may offer
        # besides SocketCAN. None lists none (and scans nothing), as in tests.
        self.adapter_catalog = adapter_catalog

        # Client management
        self.clients: Set[web.WebSocketResponse] = set()
        self.cpu = ProcessCpu()  # the server's own CPU use, for the dashboard
        self.update_check = None  # update_check.UpdateCheck, unless turned off (main sets it)
        self.allow_any_interface = False  # --allow-any-interface: a request may open any python-can interface
        self.access_log = False  # --access-log: log every HTTP request
        self.client_filters: dict[web.WebSocketResponse, dict[str, Any]] = {}
        # Clients that opted into the raw frame-capture stream → their per-client
        # subscriber queue on the FrameCaptureManager. Empty unless a Debugging
        # view explicitly started capture.
        self.capture_clients: dict[web.WebSocketResponse, asyncio.Queue] = {}

        # App and runner. Auth middleware runs first so unauthorized requests
        # never reach the CORS layer or the handlers.
        self.app = web.Application(middlewares=[
            self._auth_middleware, self._cors_middleware, self._dashboard_cache_middleware,
        ])
        self.app.on_response_prepare.append(self._add_security_headers)
        self.runner: Optional[web.AppRunner] = None
        self._running = False

        self._setup_routes()

    # What a page from this server may load and do. The dashboard has no inline
    # script and no eval; its cards and Tabulator set style attributes; the
    # graph exports through a blob: image; and its Backend URL may name another
    # server, so it may connect anywhere. No page may frame it (click-jacking).
    _CONTENT_SECURITY_POLICY = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data: blob:; font-src 'self' data:; "
                                "connect-src 'self' http: https: ws: wss:; object-src 'none'; base-uri 'none'; "
                                "form-action 'self'; frame-ancestors 'none'")

    @classmethod
    async def _add_security_headers(cls, request: web.Request, response: web.StreamResponse) -> None:
        """Hardening on every response, before its headers go out; and no version banner."""
        response.headers["Content-Security-Policy"] = cls._CONTENT_SECURITY_POLICY
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Server"] = "Cynitor"

    @staticmethod
    def _is_protected_path(path: str) -> bool:
        """Whether a path needs a token when auth is enabled.

        Only the API and the event stream are protected. The dashboard's own
        HTML, CSS and JavaScript are served open, because a browser has to be
        able to load the page before it can prompt for a token — and those
        assets are not secret. What they cannot do without one is read bus
        data or command a node.
        """
        return path == "/ws" or path == "/api" or path.startswith("/api/")

    _AUTH_FAILURE_DELAY_S = 0.25  # how long a request with a wrong token waits for its 401

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if self.auth_token is None or request.method == "OPTIONS":
            return await handler(request)
        if request.path in self._AUTH_OPEN_PATHS:
            return await handler(request)
        if not self._is_protected_path(request.path):
            return await handler(request)
        token = self._extract_token(request)
        if token is None or not hmac.compare_digest(token.encode(), self.auth_token.encode()):
            await asyncio.sleep(self._AUTH_FAILURE_DELAY_S)  # slows guessing down
            return web.json_response({"error": "missing or invalid token"}, status=401)
        return await handler(request)

    @staticmethod
    def _extract_token(request: web.Request) -> Optional[str]:
        # WebSocket clients can't set custom headers in browsers, so the WS
        # handshake accepts ?token=<value>. REST clients use the standard
        # Authorization: Bearer <value> header.
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[7:].strip() or None
        # Only where a header is impossible: the WebSocket handshake and a
        # download link, both GETs. A request that changes something brings the header.
        qs = request.query.get("token") if request.method == "GET" else None
        return qs.strip() if qs else None

    @web.middleware
    async def _dashboard_cache_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        """Make browsers check the dashboard's files for changes before reusing them.

        Without a caching header, browsers reuse a file for as long as their
        own heuristics allow, so after an upgrade a page could keep running
        old JavaScript against the new API. no-cache still allows caching:
        each use is revalidated, and an unchanged file costs a 304 thanks to
        the ETag and Last-Modified headers aiohttp sends for files.
        """
        response = await handler(request)
        if not self._is_protected_path(request.path):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    _DEV_SERVER_PORTS = frozenset({5500})  # `python3 -m http.server 5500` in website/, as the docs say

    @staticmethod
    def _dev_origins() -> set[str]:
        """Other origins a dashboard may be served from, in CYNITOR_DEV_ORIGINS (comma-separated)."""
        return {o.strip().rstrip("/").lower() for o in os.environ.get("CYNITOR_DEV_ORIGINS", "").split(",") if o.strip()}

    def _browser_request_allowed(self, request: web.Request) -> bool:
        """Whether a request may come from the browser page that sent it.

        The API commands nodes on the bus, and a page on any website can send
        requests to localhost through its visitor's browser. Browsers name the
        sending page in the Origin header; allowed are the dashboard this server
        serves (same origin) and pages served from this machine, such as the
        frontend dev server on :5500. Tools like curl send no Origin.

        A site can also re-point its own name at 127.0.0.1 (DNS rebinding) so
        that its requests look same-origin. A server listening on loopback only
        is never legitimately called by another name, so there the Host header
        must name loopback too.
        """
        host = (urlsplit(f"//{request.host}").hostname or "").lower()
        if self.host in self._LOOPBACK_HOSTS and host not in self._LOOPBACK_HOSTS:
            return False
        # Browsers say where a request comes from even when they send no Origin,
        # as for an <img>: a GET that reads registers costs frames on the bus.
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        origin = request.headers.get("Origin")
        if origin is None:
            return True
        parts = urlsplit(origin)  # "null" (sandboxed frames, file://) has no host
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False
        if parts.netloc.lower() == request.host.lower():
            return True
        # Another page on this machine: the frontend dev server only, not any port.
        return parts.hostname in self._LOOPBACK_HOSTS and (
            parts.port in self._DEV_SERVER_PORTS or origin.rstrip("/").lower() in self._dev_origins())

    @web.middleware
    async def _cors_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if self._is_protected_path(request.path) and not self._browser_request_allowed(request):
            return web.json_response({"error": "cross-origin request refused"}, status=403)
        if request.method == "OPTIONS":
            response = web.Response()
        else:
            try:
                response = await handler(request)
            except web.HTTPException as ex:
                response = ex
        origin = request.headers.get("Origin")
        if origin is not None and self._is_protected_path(request.path):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Headers"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
            response.headers.add("Vary", "Origin")
        return response

    def _setup_routes(self) -> None:
        """Configure HTTP routes and WebSocket handler."""
        self.app.router.add_get('/ws', self._websocket_handler)

        self.app.router.add_get('/api', self._api_info)
        self.app.router.add_get('/api/info', self._api_info)
        self.app.router.add_get('/api/status', self._get_status)
        self.app.router.add_get('/api/nodes', self._get_all_nodes)
        self.app.router.add_get('/api/latest/subject/{subject_id}', self._get_latest_subject)
        self.app.router.add_get('/api/latest/node/{node_id}', self._get_latest_node)
        self.app.router.add_get('/api/logs', self._get_logs)
        self.app.router.add_get('/api/health', self._health_check)
        self.app.router.add_get('/api/services/{node_id}', self._get_services)
        self.app.router.add_get('/api/clients/{node_id}', self._get_clients)
        self.app.router.add_get('/api/registers/{node_id}', self._get_registers)
        self.app.router.add_post('/api/registers/{node_id}/set', self._set_register)
        self.app.router.add_post('/api/services/{node_id}/{service_id}/call', self._call_service)
        self.app.router.add_get('/api/nodes/events', self._get_recent_node_events)
        self.app.router.add_get('/api/nodes/{node_id}/history', self._get_node_history)
        self.app.router.add_get('/api/nodes/{node_id}/history/subjects', self._get_node_subject_summary)
        self.app.router.add_get('/api/services/{service_id}/history', self._get_service_call_history)
        self.app.router.add_get('/api/identity-map', self._get_identity_map)
        self.app.router.add_delete('/api/identity/{unique_id}', self._delete_identity)
        self.app.router.add_get('/api/can/adapters', self._get_adapters)
        self.app.router.add_post('/api/can/connect', self._can_connect)
        self.app.router.add_post('/api/can/disconnect', self._can_disconnect)
        self.app.router.add_post('/api/decoding', self._set_decoding)
        self.app.router.add_get('/api/can/transport', self._get_transport_diagnostics)
        self.app.router.add_get('/api/can/capture', self._get_capture)

        self.app.router.add_get('/api/recordings', self._get_recordings)
        self.app.router.add_post('/api/recordings', self._post_recording)
        self.app.router.add_post('/api/recordings/quick', self._post_quick_recording)
        self.app.router.add_get('/api/recordings/buffer', self._get_recordings_buffer)
        self.app.router.add_get('/api/recordings/{rec_id}', self._get_recording)
        self.app.router.add_patch('/api/recordings/{rec_id}', self._patch_recording)
        self.app.router.add_delete('/api/recordings/{rec_id}', self._delete_recording)
        self.app.router.add_post('/api/recordings/{rec_id}/stop', self._post_recording_stop)
        self.app.router.add_get('/api/recordings/{rec_id}/export', self._export_recording)

        self.app.router.add_get('/api/rawlogs', self._get_raw_logs)
        self.app.router.add_post('/api/rawlogs', self._start_raw_log)
        self.app.router.add_post('/api/rawlogs/stop', self._stop_raw_log)
        self.app.router.add_get('/api/rawlogs/{name}', self._download_raw_log)
        self.app.router.add_delete('/api/rawlogs/{name}', self._delete_raw_log)
        self.app.router.add_post('/api/rawlogs/{name}/play', self._play_raw_log)
        self.app.router.add_put('/api/subjects/{subject_id}/type', self._set_subject_type)
        self.app.router.add_delete('/api/subjects/{subject_id}/type', self._clear_subject_type)
        self.app.router.add_get('/api/subjects/{subject_id}/type-guesses', self._guess_subject_type)
        self.app.router.add_get('/api/testnodes', self._get_test_nodes)
        self.app.router.add_post('/api/testnodes', self._add_test_node)
        self.app.router.add_put('/api/testnodes/{test_id}', self._replace_test_node)
        self.app.router.add_delete('/api/testnodes/{test_id}', self._delete_test_node)
        self.app.router.add_post('/api/testnodes/{test_id}/start', self._start_test_node)
        self.app.router.add_post('/api/testnodes/{test_id}/stop', self._stop_test_node)
        self.app.router.add_get('/api/firmware', self._get_firmware)
        self.app.router.add_post('/api/firmware', self._upload_firmware)
        self.app.router.add_delete('/api/firmware/{name}', self._delete_firmware)
        self.app.router.add_post('/api/nodes/{node_id}/firmware', self._update_firmware)

        self.app.router.add_post('/api/replay/start', self._replay_start)
        self.app.router.add_post('/api/replay/control', self._replay_control)
        self.app.router.add_post('/api/replay/seek', self._replay_seek)
        self.app.router.add_post('/api/replay/speed', self._replay_speed)
        self.app.router.add_get('/api/replay/status', self._replay_status)

        self.app.router.add_get('/api/dsdl/status', self._dsdl_status)
        self.app.router.add_get('/api/dsdl/namespaces', self._dsdl_namespaces)
        self.app.router.add_get('/api/dsdl/type/{full_name:.+}', self._dsdl_type_detail)
        self.app.router.add_get('/api/dsdl/value-schema/{full_name:.+}', self._dsdl_value_schema)
        self.app.router.add_post('/api/dsdl/custom/namespace', self._dsdl_create_namespace)
        self.app.router.add_delete('/api/dsdl/custom/namespace/{namespace}', self._dsdl_delete_namespace)
        self.app.router.add_post('/api/dsdl/custom/type', self._dsdl_save_type)
        self.app.router.add_delete('/api/dsdl/custom/type/{full_name:.+}', self._dsdl_delete_type)
        self.app.router.add_get('/api/dsdl/custom/namespaces', self._dsdl_list_custom_namespaces)
        self.app.router.add_post('/api/dsdl/compile', self._dsdl_compile)

        # The dashboard, when this server is also hosting it. Registered last:
        # aiohttp matches resources in registration order, so every /api and
        # /ws route above wins over the catch-all static mount below.
        if self.website_dir:
            self.app.router.add_get('/', self._serve_index)
            self.app.router.add_get('/config.js', self._serve_config_js)
            self.app.router.add_static('/', self.website_dir)
            logger.info("Serving the dashboard from %s", self.website_dir)

    async def _serve_index(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(self.website_dir / "index.html")

    async def _serve_config_js(self, request: web.Request) -> web.Response:
        """Tell the page which backend to talk to: this one.

        The frontend's default address is only correct when it is served by a
        separate static file server on the developer's own machine. Served
        from here, the API lives at this request's own origin, whatever host
        and port the user reached us on. website/config.js is an empty
        placeholder so the development flow keeps the built-in default.
        """
        origin = f"{request.scheme}://{request.host}"
        body = f"window.__CYNITOR = {json.dumps({'apiBase': origin, 'version': __version__})};\n"
        return web.Response(text=body, content_type="application/javascript")

    async def start(self) -> None:
        """Start the WebSocket server."""
        try:
            # A line per request is a few a second per open dashboard: only when asked for.
            self.runner = web.AppRunner(self.app, **({} if self.access_log else {"access_log": None}))
            await self.runner.setup()
            site = web.TCPSite(self.runner, self.host, self.port)
            await site.start()
            self._running = True
            logger.info(f"WebSocket server started on ws://{self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Failed to start WebSocket server: {e}", exc_info=True)
            raise

    async def stop(self) -> None:
        """Stop the WebSocket server and clean up."""
        self._running = False

        for ws in list(self.clients):
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"Server shutdown")

        if self.runner:
            await self.runner.cleanup()

        logger.info("WebSocket server stopped")


    async def _read_json_body(self, request: web.Request) -> tuple[Optional[dict], Optional[web.Response]]:
        try:
            body = await request.json()
        except Exception:
            return None, web.json_response({"error": "Invalid JSON body"}, status=400)
        if not isinstance(body, dict):
            return None, web.json_response({"error": "JSON body must be an object"}, status=400)
        return body, None


    # ------------------------------------------------------------------
    # WebSocket handler
    # ------------------------------------------------------------------

    async def _websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        self.clients.add(ws)
        self.client_filters[ws] = {}

        logger.info(f"Client connected. Total clients: {len(self.clients)}")
        queue: Optional[asyncio.Queue] = None

        try:
            recv_task = asyncio.create_task(self._receive_client_messages(ws))
            metrics_task = asyncio.create_task(self._send_metrics_loop(ws))
            capture_task = asyncio.create_task(self._capture_loop(ws))

            while not ws.closed:
                source = self._event_source()
                if source is not None:
                    queue = source.subscribe(max_queue=100)
                    consume_task = asyncio.create_task(self._consume_and_send(ws, queue, source))
                    done, pending = await asyncio.wait(
                        [consume_task, recv_task, metrics_task, capture_task],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        exc = task.exception()
                        if exc is not None:
                            logger.error(
                                "WebSocket subtask finished with exception",
                                exc_info=(type(exc), exc, exc.__traceback__),
                            )
                    if done == {consume_task} and consume_task.exception() is None and consume_task.result():
                        # The session changed (CAN disconnected or connected
                        # again): follow it on the same socket.
                        source.unsubscribe(queue)
                        queue = None
                        continue
                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                    break

                done, _ = await asyncio.wait(
                    [recv_task], timeout=1.0
                )
                if done:
                    break

            if not recv_task.done():
                recv_task.cancel()
                try:
                    await recv_task
                except asyncio.CancelledError:
                    pass
            if not metrics_task.done():
                metrics_task.cancel()
                try:
                    await metrics_task
                except asyncio.CancelledError:
                    pass
            if not capture_task.done():
                capture_task.cancel()
                try:
                    await capture_task
                except asyncio.CancelledError:
                    pass

        except Exception as e:
            logger.error(f"Error in WebSocket handler: {e}", exc_info=True)
        finally:
            self.clients.discard(ws)
            self.client_filters.pop(ws, None)
            self.session.set_watched(ws, None)
            cap_queue = self.capture_clients.pop(ws, None)
            if cap_queue is not None and self.session.frame_capture is not None:
                self.session.frame_capture.unsubscribe(cap_queue)
            if queue is not None:
                # The queue belongs to whichever source was active when we
                # subscribed. Both sources are unsubscribe-safe with an
                # unknown queue (set.discard is a no-op for missing keys).
                if self.session.telemetry is not None:
                    self.session.telemetry.unsubscribe(queue)
                if self.session.replay is not None:
                    self.session.replay.unsubscribe(queue)
            logger.info(f"Client disconnected. Total clients: {len(self.clients)}")

        return ws

    # Cap how long a single send may block waiting for a slow client. If TCP
    # backpressure stalls the send beyond this, the connection is considered
    # dead and the consumer exits — the handler then tears the WS down and
    # the broadcast loop stops queuing for the orphaned subscriber. Cynitor
    # broadcasts at most ~1 kHz of small JSON frames, so 10 s is far above
    # any healthy peer's worst-case latency.
    _WS_SEND_TIMEOUT = 10.0

    def _event_source(self):
        """What the stream comes from now: the CAN session's telemetry, else a replay, else nothing."""
        return self.session.telemetry if self.session.telemetry is not None else self.session.replay

    async def _consume_and_send(self, ws: web.WebSocketResponse, queue: asyncio.Queue, source=None) -> bool:
        """Send ``queue``'s events to ``ws``. True once ``source`` is no longer the session's, to subscribe anew."""
        while self._running and not ws.closed:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if source is not None and self._event_source() is not source:
                    return True
                continue  # no events in the last second; loop back and re-check ws.closed
            except asyncio.CancelledError:
                break

            # End-of-session sentinel from ReplayManager: forward to the
            # client (so the UI can switch back to idle / re-fetch state)
            # then exit the consume loop. The WS handler will tear the
            # connection down and the client reconnects to pick up the
            # new state on a fresh subscriber.
            if isinstance(event, dict) and event.get("type") == "replay_ended":
                try:
                    await asyncio.wait_for(ws.send_json(event), timeout=self._WS_SEND_TIMEOUT)
                except Exception:
                    pass
                break

            if not self._event_matches_filter(event, ws):
                continue

            try:
                await asyncio.wait_for(ws.send_json(event), timeout=self._WS_SEND_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    "WebSocket send blocked > %ss — closing slow/stuck client",
                    self._WS_SEND_TIMEOUT,
                )
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error sending to client: %s", e)
                break

    async def _send_metrics_loop(self, ws: web.WebSocketResponse) -> None:
        """Send the bus load and the server's CPU use to the client every second."""
        try:
            while self._running and not ws.closed:
                await asyncio.sleep(1.0)
                bus_load = self.session.bus_load
                await ws.send_json({
                    "type": "metrics",
                    "bus_utilization": bus_load.utilization if bus_load else None,
                    "server_cpu": self.cpu.read(),
                })
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Metrics loop ended: {e}")

    # Batch window for the raw frame stream. Captured frames are coalesced into
    # one message per window to bound message rate under heavy bus load. A
    # message takes all that is queued, up to about 16,000 frames a second,
    # more than a 1 Mbit/s bus carries: with less, a busy bus outran the
    # stream, which fell behind and dropped frames.
    _CAPTURE_BATCH_WINDOW = 0.12
    _CAPTURE_BATCH_MAX = 2000
    # Frames caught before a client subscribed, sent with the reply.
    _CAPTURE_EARLIER = 500

    async def _capture_loop(self, ws: web.WebSocketResponse) -> None:
        """Forward raw captured frames to a client that opted in via a
        ``{"type":"capture","enabled":true}`` message. No-op (idle poll) until
        the client subscribes; batches frames to bound the WS message rate.
        """
        try:
            while self._running and not ws.closed:
                q = self.capture_clients.get(ws)
                if q is None:
                    await asyncio.sleep(0.2)
                    continue
                try:
                    first = await asyncio.wait_for(q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                frames = [first]
                await asyncio.sleep(self._CAPTURE_BATCH_WINDOW)
                while len(frames) < self._CAPTURE_BATCH_MAX:
                    try:
                        frames.append(q.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                mgr = self.session.frame_capture
                await asyncio.wait_for(ws.send_json({
                    "type": "can_frame",
                    "frames": frames,
                    "stats": mgr.stats() if mgr else None,
                }), timeout=self._WS_SEND_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Capture loop ended: {e}")

    async def _handle_capture_message(self, ws: web.WebSocketResponse, enabled: bool) -> None:
        """Enable/disable raw frame forwarding for this client. The first client
        to enable starts capture, and the last to disable (or leave) stops it.

        Each reply says both: ``capturing`` (capture runs, for any client) and
        ``forwarding`` (frames sent to this client). ``active`` stays for
        dashboards from before those two."""
        mgr = self.session.frame_capture
        if enabled:
            if mgr is None:
                await ws.send_json({"type": "capture_status", "active": False, "capturing": False,
                                    "forwarding": False, "error": "CAN not connected"})
                return
            earlier = []
            if ws not in self.capture_clients:
                try:
                    self.capture_clients[ws] = mgr.subscribe()
                except Exception as exc:  # the tap would not open
                    logger.warning("Cannot capture CAN frames: %s", exc)
                    await ws.send_json({"type": "capture_status", "active": False, "capturing": mgr.active,
                                        "forwarding": False, "error": f"Cannot capture frames: {exc}"})
                    return
                # Taken in the same step as subscribing: frames reach the ring and
                # the queues on this event loop, so none falls between the two or
                # comes twice.
                earlier = mgr.snapshot(self._CAPTURE_EARLIER)
            await ws.send_json({"type": "capture_status", "active": mgr.active, "capturing": mgr.active,
                                "forwarding": True, "stats": mgr.stats(), "frames": earlier})
        else:
            q = self.capture_clients.pop(ws, None)
            if q is not None and mgr is not None:
                mgr.unsubscribe(q)
            await ws.send_json({"type": "capture_status", "active": False,
                                "capturing": mgr is not None and mgr.active, "forwarding": False})

    _MAX_WATCHED = 8192  # there are no more subject-IDs

    async def _receive_client_messages(self, ws: web.WebSocketResponse) -> None:
        async for msg in ws:
            try:
                if msg.type == web.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if not isinstance(data, dict):
                        await ws.send_json({"error": "A message must be a JSON object"})
                        continue

                    if data.get("type") == "filter":
                        self.client_filters[ws] = {
                            "subject_ids": data.get("subject_ids"),
                            "node_ids": data.get("node_ids"),
                            "message_types": data.get("message_types"),
                        }
                        logger.debug(f"Updated filter for client: {self.client_filters[ws]}")

                        await ws.send_json({
                            "type": "filter_updated",
                            "filter": self.client_filters[ws]
                        })

                    elif data.get("type") == "watch":
                        # The subjects this dashboard shows the values of: what
                        # is decoded for it while the server decodes on demand.
                        subject_ids = data.get("subject_ids")
                        if isinstance(subject_ids, list) and len(subject_ids) <= self._MAX_WATCHED and all(
                                isinstance(s, int) and not isinstance(s, bool) and 0 <= s <= 8191
                                for s in subject_ids):
                            self.session.set_watched(ws, subject_ids)
                        else:
                            await ws.send_json({"error": "watch: subject_ids must be a list of subject-IDs"})

                    elif data.get("type") == "ping":
                        await ws.send_json({"type": "pong"})

                    elif data.get("type") == "capture":
                        await self._handle_capture_message(ws, bool(data.get("enabled")))

                    else:
                        logger.warning(f"Unknown message type: {data.get('type')}")

                elif msg.type == web.WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {ws.exception()}")
                    break

            except json.JSONDecodeError:
                logger.warning("Invalid JSON from client")
                await ws.send_json({"error": "Invalid JSON"})
            except Exception as e:
                logger.error(f"Error processing client message: {e}")
                break

    def _event_matches_filter(self, event: dict[str, Any], ws: web.WebSocketResponse) -> bool:
        filters = self.client_filters.get(ws, {})

        if not any(filters.values()):
            return True

        if filters.get("subject_ids"):
            if event.get("subject_id") not in filters["subject_ids"]:
                return False

        if filters.get("node_ids"):
            if event.get("publisher_node_id") not in filters["node_ids"]:
                return False

        if filters.get("message_types"):
            if event.get("message_type") not in filters["message_types"]:
                return False

        return True

    # ------------------------------------------------------------------
    # REST endpoints
    # ------------------------------------------------------------------

    async def _api_info(self, request: web.Request) -> web.Response:
        return web.json_response({
            "name": "UAVCAN Telemetry API",
            "version": "2.0",
            "endpoints": {
                "WebSocket": {
                    # Derived from the request so it stays correct behind a
                    # proxy, on another host, or on a non-default port.
                    "url": f"ws://{request.host}/ws",
                    "description": "Real-time event streaming with optional filtering"
                },
                "REST": {
                    "/api": "API information (this endpoint)",
                    "/api/status": "Server status, CAN interface, available interfaces",
                    "/api/health": "Server health check",
                    "/api/can/adapters": "GET - CAN adapters to connect to (?refresh=1 rescans)",
                    "/api/can/connect": "POST - Connect to a CAN interface",
                    "/api/decoding": "POST - Decode on demand, or every subject",
                    "/api/can/disconnect": "POST - Disconnect from CAN interface",
                    "/api/can/transport": "Transport-layer diagnostics (MTU, frame stats, bus state)",
                    "/api/can/capture": "Raw frame-capture ring-buffer snapshot (WS 'capture' message streams live)",
                    "/api/nodes": "Get info about all discovered nodes",
                    "/api/nodes/events": "Recent lifecycle events of every node (?range=15m&types=a,b&limit=500)",
                    "/api/latest/subject/{subject_id}": "Get latest event for a subject",
                    "/api/latest/node/{node_id}": "Get latest events from a node",
                    "/api/identity-map": "Get unique_id to node_id mappings",
                    "/api/logs": "Get recent application logs",
                }
            },
        })

    async def _get_logs(self, request: web.Request) -> web.Response:
        if self.log_store is None:
            return web.json_response(
                {"error": "Log store is not configured"},
                status=503,
            )

        limit_raw = request.query.get("limit", "200")
        level = request.query.get("level")
        min_level = request.query.get("min_level")
        since = request.query.get("since")

        try:
            limit = int(limit_raw)
        except ValueError:
            return web.json_response(
                {"error": "Query parameter 'limit' must be an integer"},
                status=400,
            )

        try:
            logs = self.log_store.get_logs(limit=limit, level=level, min_level=min_level, since=since)
        except ValueError as ex:
            return web.json_response({"error": str(ex)}, status=400)
        except Exception as ex:
            logger.error("Error while reading logs: %s", ex, exc_info=True)
            return web.json_response({"error": str(ex)}, status=500)

        return web.json_response({
            "count": len(logs),
            "logs": logs,
        })

    async def _health_check(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "healthy" if self._running else "degraded",
            "can_status": "running" if self.session.is_running else "idle",
            "can_interface": self.session.can_interface,
            "connected_clients": len(self.clients),
            "server": {
                "host": self.host,
                "port": self.port,
            }
        })


