"""Recordings: named stretches of the event history, their export, and their replay.

Handlers of the REST API, which WebSocketServer (websocket_server.py) inherits and routes to."""

import json
import re
import time
from typing import Any, Optional

from aiohttp import web

from api_helpers import _parse_int, logger


def _csv_escape(value: Any) -> str:
    """Escape a value for inclusion in a CSV cell."""
    if value is None:
        return ""
    s = str(value)
    if any(c in s for c in (",", '"', "\n", "\r")):
        s = '"' + s.replace('"', '""') + '"'
    return s


class RecordingsApi:
    """The recordings part of WebSocketServer, which has the session and the rest these use."""

    _MAX_REC_ID = 2**31 - 1

    _EXPORT_PAGE_SIZE = 2000

    # Recordings live in the data folder: every recording route works with CAN
    # disconnected too, starting the session's event logger if need be.

    async def _get_recordings(self, request: web.Request) -> web.Response:
        await self.session.ensure_event_logger()
        recs = await self.session.event_logger.list_recordings()
        return web.json_response({"recordings": recs})

    @staticmethod
    def _parse_limits(body: dict) -> tuple[Optional[float], Optional[int], bool, Optional[web.Response]]:
        """Extract (max_length_seconds, max_events, stop_on_limit) from a POST
        body. Returns the parsed values + an optional error response."""
        max_length = body.get("max_length_seconds")
        if max_length is not None:
            if isinstance(max_length, bool) or not isinstance(max_length, (int, float)) or max_length <= 0:
                return None, None, False, web.json_response(
                    {"error": "max_length_seconds must be a positive number"}, status=400
                )
            max_length = float(max_length)
        max_events = body.get("max_events")
        if max_events is not None:
            if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events <= 0:
                return None, None, False, web.json_response(
                    {"error": "max_events must be a positive integer"}, status=400
                )
        stop_on_limit = bool(body.get("stop_on_limit", False))
        return max_length, max_events, stop_on_limit, None

    async def _post_recording(self, request: web.Request) -> web.Response:
        await self.session.ensure_event_logger()
        body, err = await self._read_json_body(request)
        if err:
            return err
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return web.json_response({"error": "Name is required"}, status=400)
        max_length, max_events, stop_on_limit, err = self._parse_limits(body)
        if err:
            return err
        notes = body.get("notes")
        rec_id = await self.session.event_logger.create_recording(
            name=name.strip(),
            filter_spec=body.get("filter"),
            notes=notes if isinstance(notes, str) else None,
            max_length_seconds=max_length,
            max_events=max_events,
            stop_on_limit=stop_on_limit,
        )
        rec = await self.session.event_logger.get_recording_stats(rec_id)
        return web.json_response({"recording": rec}, status=201)

    async def _post_quick_recording(self, request: web.Request) -> web.Response:
        await self.session.ensure_event_logger()
        body, err = await self._read_json_body(request)
        if err:
            return err
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return web.json_response({"error": "Name is required"}, status=400)
        last_seconds = body.get("last_seconds")
        if not isinstance(last_seconds, (int, float)) or isinstance(last_seconds, bool) or last_seconds <= 0:
            return web.json_response({"error": "last_seconds must be a positive number"}, status=400)
        notes = body.get("notes")
        now = time.time()
        rec_id = await self.session.event_logger.create_recording(
            name=name.strip(),
            start_unix=now - float(last_seconds),
            end_unix=now,
            filter_spec=body.get("filter"),
            notes=notes if isinstance(notes, str) else None,
        )
        rec = await self.session.event_logger.get_recording_stats(rec_id)
        return web.json_response({"recording": rec}, status=201)

    async def _get_recordings_buffer(self, request: web.Request) -> web.Response:
        await self.session.ensure_event_logger()
        stats = await self.session.event_logger.get_buffer_stats()
        return web.json_response({"buffer": stats})

    async def _post_recording_stop(self, request: web.Request) -> web.Response:
        rec_id, err = _parse_int(request.match_info.get('rec_id'), 'recording_id', 1, self._MAX_REC_ID)
        if err:
            return err
        await self.session.ensure_event_logger()
        ok = await self.session.event_logger.stop_recording(rec_id)
        if not ok:
            return web.json_response({"error": "Recording not found or already stopped"}, status=404)
        rec = await self.session.event_logger.get_recording_stats(rec_id)
        return web.json_response({"recording": rec})

    async def _patch_recording(self, request: web.Request) -> web.Response:
        rec_id, err = _parse_int(request.match_info.get('rec_id'), 'recording_id', 1, self._MAX_REC_ID)
        if err:
            return err
        await self.session.ensure_event_logger()
        body, err = await self._read_json_body(request)
        if err:
            return err
        name = body.get("name")
        notes = body.get("notes")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            return web.json_response({"error": "Name must be a non-empty string"}, status=400)
        if notes is not None and not isinstance(notes, str):
            return web.json_response({"error": "Notes must be a string"}, status=400)

        max_length = body.get("max_length_seconds")
        if max_length is not None:
            if isinstance(max_length, bool) or not isinstance(max_length, (int, float)) or max_length <= 0:
                return web.json_response({"error": "max_length_seconds must be a positive number"}, status=400)
            max_length = float(max_length)
        max_events = body.get("max_events")
        if max_events is not None:
            if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events <= 0:
                return web.json_response({"error": "max_events must be a positive integer"}, status=400)
        stop_on_limit = body.get("stop_on_limit")
        if stop_on_limit is not None and not isinstance(stop_on_limit, bool):
            return web.json_response({"error": "stop_on_limit must be a boolean"}, status=400)

        ok = await self.session.event_logger.update_recording(
            rec_id,
            name=name.strip() if isinstance(name, str) else None,
            notes=notes,
            max_length_seconds=max_length,
            max_events=max_events,
            stop_on_limit=stop_on_limit,
        )
        if not ok:
            existing = await self.session.event_logger.get_recording(rec_id)
            if not existing:
                return web.json_response({"error": "Recording not found"}, status=404)
            return web.json_response({"error": "Nothing to update"}, status=400)
        rec = await self.session.event_logger.get_recording_stats(rec_id)
        return web.json_response({"recording": rec})

    async def _get_recording(self, request: web.Request) -> web.Response:
        rec_id, err = _parse_int(request.match_info.get('rec_id'), 'recording_id', 1, self._MAX_REC_ID)
        if err:
            return err
        await self.session.ensure_event_logger()
        rec = await self.session.event_logger.get_recording_stats(rec_id)
        if not rec:
            return web.json_response({"error": "Recording not found"}, status=404)
        return web.json_response({"recording": rec})

    async def _delete_recording(self, request: web.Request) -> web.Response:
        rec_id, err = _parse_int(request.match_info.get('rec_id'), 'recording_id', 1, self._MAX_REC_ID)
        if err:
            return err
        await self.session.ensure_event_logger()
        purge = request.query.get("purge", "false").lower() == "true"
        ok = await self.session.event_logger.delete_recording(rec_id, purge_events=purge)
        if not ok:
            return web.json_response({"error": "Recording not found"}, status=404)
        return web.json_response({"deleted": True, "purged": purge})

    async def _export_recording(self, request: web.Request) -> web.StreamResponse:
        rec_id, err = _parse_int(request.match_info.get('rec_id'), 'recording_id', 1, self._MAX_REC_ID)
        if err:
            return err
        await self.session.ensure_event_logger()
        fmt = request.query.get("format", "csv").lower()
        if fmt not in ("csv", "jsonl"):
            return web.json_response({"error": "format must be csv or jsonl"}, status=400)
        rec = await self.session.event_logger.get_recording(rec_id)
        if not rec:
            return web.json_response({"error": "Recording not found"}, status=404)

        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", rec["name"])[:60] or f"recording-{rec_id}"
        if fmt == "csv":
            return await self._export_recording_csv(request, rec, safe_name)
        return await self._export_recording_jsonl(request, rec, safe_name)

    async def _export_recording_csv(self, request: web.Request, rec: dict, safe_name: str) -> web.StreamResponse:
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/csv; charset=utf-8",
                "Content-Disposition": f'attachment; filename="{safe_name}.csv"',
            }
        )
        await resp.prepare(request)
        header = "recording_id,timestamp_unix,timestamp,subject_id,service_id,publisher_node_id,unique_id,message_type,rate,attribute,value,unit\n"
        await resp.write(header.encode("utf-8"))

        offset = 0
        while True:
            events = await self.session.event_logger.get_recording_events(
                rec["id"], limit=self._EXPORT_PAGE_SIZE, offset=offset
            )
            if not events:
                break
            lines: list[str] = []
            for ev in events:
                base = [
                    str(rec["id"]),
                    f"{ev['timestamp_unix']:.6f}" if ev.get("timestamp_unix") is not None else "",
                    _csv_escape(ev.get("timestamp")),
                    _csv_escape(ev.get("subject_id")),
                    _csv_escape(ev.get("service_id")),
                    _csv_escape(ev.get("publisher_node_id")),
                    _csv_escape(ev.get("unique_id")),
                    _csv_escape(ev.get("message_type")),
                    _csv_escape(ev.get("rate")),
                ]
                attrs = ev.get("attributes") or []
                if isinstance(attrs, dict):  # a service call's fields, a row each
                    attrs = [{"attribute": name, "value": value} for name, value in attrs.items()]
                if not attrs:
                    lines.append(",".join(base + ["", "", ""]))
                else:
                    for a in attrs:
                        lines.append(",".join(base + [
                            _csv_escape(a.get("attribute")),
                            _csv_escape(a.get("value")),
                            _csv_escape(a.get("unit")),
                        ]))
            await resp.write(("\n".join(lines) + "\n").encode("utf-8"))
            offset += len(events)
            if len(events) < self._EXPORT_PAGE_SIZE:
                break
        await resp.write_eof()
        return resp

    async def _export_recording_jsonl(self, request: web.Request, rec: dict, safe_name: str) -> web.StreamResponse:
        resp = web.StreamResponse(
            headers={
                "Content-Type": "application/x-ndjson; charset=utf-8",
                "Content-Disposition": f'attachment; filename="{safe_name}.jsonl"',
            }
        )
        await resp.prepare(request)
        header = json.dumps({"recording": rec, "exported_at_unix": time.time()})
        await resp.write((header + "\n").encode("utf-8"))

        offset = 0
        while True:
            events = await self.session.event_logger.get_recording_events(
                rec["id"], limit=self._EXPORT_PAGE_SIZE, offset=offset
            )
            if not events:
                break
            chunk = "".join(json.dumps(ev) + "\n" for ev in events)
            await resp.write(chunk.encode("utf-8"))
            offset += len(events)
            if len(events) < self._EXPORT_PAGE_SIZE:
                break
        await resp.write_eof()
        return resp

    async def _replay_start(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        rec_id, err = _parse_int(body.get("recording_id"), "recording_id", 1, 2**31 - 1)
        if err:
            return err
        speed = float(body.get("speed", 1.0)) if body.get("speed") is not None else 1.0
        start_offset_s = float(body.get("start_offset_s", 0.0))
        try:
            status = await self.session.start_replay(rec_id, speed=speed,
                                                    start_offset_s=start_offset_s)
            return web.json_response(status, status=200)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=404)
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=409)
        except Exception as e:
            logger.error(f"Replay start failed: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def _replay_control(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        action = body.get("action")
        if self.session.replay is None and action != "stop":
            return web.json_response({"error": "No replay running"}, status=404)
        if action == "pause":
            self.session.replay.pause()
        elif action == "resume":
            self.session.replay.resume()
        elif action == "stop":
            await self.session.stop_replay()
            return web.json_response({"active": False}, status=200)
        else:
            return web.json_response({"error": f"Unknown action: {action}"}, status=400)
        return web.json_response(self.session.replay.status(), status=200)

    async def _replay_seek(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        if self.session.replay is None:
            return web.json_response({"error": "No replay running"}, status=404)
        try:
            position_s = float(body.get("position_s"))
        except (TypeError, ValueError):
            return web.json_response({"error": "position_s must be a number"}, status=400)
        self.session.replay.seek(position_s)
        return web.json_response(self.session.replay.status(), status=200)

    async def _replay_speed(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        if self.session.replay is None:
            return web.json_response({"error": "No replay running"}, status=404)
        try:
            speed = float(body.get("speed"))
        except (TypeError, ValueError):
            return web.json_response({"error": "speed must be a number"}, status=400)
        self.session.replay.set_speed(speed)
        return web.json_response(self.session.replay.status(), status=200)

    async def _replay_status(self, request: web.Request) -> web.Response:
        if self.session.replay is None:
            return web.json_response({"active": False}, status=200)
        return web.json_response(self.session.replay.status(), status=200)
