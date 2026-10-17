#!/usr/bin/env python3
"""Integration test: the HTTP API and the event stream of a real server, on the simulated bus.

hub_session.py drives a CANSession in-process; this starts the server itself,
`main.py` as a user runs it, on a free port with a data folder of its own and
the simulated bus (sim:classic, or sim:fd with --fd), and walks the API over
HTTP and /ws the way the dashboard does. Every OS can run it: no hardware and
no SocketCAN are needed.

Checks, in order:
  1. the server starts, connects the simulated bus at startup and names its
     demo sensor;
  2. its registers read over HTTP, a register write reads back, and a
     service call (GetInfo) answers;
  3. /ws sends telemetry, every message valid JSON;
  4. a raw log records frames and downloads as candump lines;
  5. the connect contract: 409 while connected, 400 for an interface type
     the API does not open;
  6. listening only: the session reconnects sending nothing, and register
     reads are refused with 409;
  7. a server with a token refuses a request without it (401) and takes one
     with it.

Run from the repository root (needs server/requirements.txt):
    python3 tests/integration/api_walk.py [--fd]
"""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import aiohttp

SERVER_DIR = Path(__file__).resolve().parents[2] / "server"
TIMEOUT = 60.0


def check(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}")
        sys.exit(1)
    print(f"ok:   {message}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """main.py in a process of its own, and its API."""

    def __init__(self, data_dir: Path, args: list[str], token: str | None = None) -> None:
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.token = token
        env = {k: v for k, v in os.environ.items() if k not in ("UAVCAN__NODE__ID", "CYNITOR_AUTH_TOKEN")}
        if token:
            env["CYNITOR_AUTH_TOKEN"] = token
        self.log = open(data_dir / "server.log", "w")
        self.proc = subprocess.Popen(
            [sys.executable, "main.py", "--port", str(self.port), "--data-dir", str(data_dir),
             "--no-update-check", *args],
            cwd=SERVER_DIR, env=env, stdout=self.log, stderr=subprocess.STDOUT)

    def request(self, method: str, path: str, body=None, token: str | None = None):
        """(status, decoded JSON or the raw bytes) of one request."""
        headers = {"Content-Type": "application/json"}
        if token or self.token:
            headers["Authorization"] = f"Bearer {token or self.token}"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw

    def wait_until(self, condition, timeout: float = TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return None
            try:
                found = condition()
                if found:
                    return found
            except (OSError, ValueError, KeyError, TypeError):
                pass
            time.sleep(0.5)
        return None

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


async def stream_messages(server: Server, seconds: float) -> list[str]:
    texts = []
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(server.base.replace("http", "ws") + "/ws") as ws:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                try:
                    msg = await ws.receive(timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if msg.type == aiohttp.WSMsgType.TEXT:
                    texts.append(msg.data)
    return texts


def named_node(server: Server):
    nodes = server.request("GET", "/api/nodes")[1]["nodes"]
    return next(((int(k), n) for k, n in nodes.items() if k.isdigit() and n.get("name")), None)


def walk(data_dir: Path, bus: str) -> None:
    server = Server(data_dir, ["--can", bus])
    try:
        # 1
        found = server.wait_until(lambda: named_node(server))
        check(found is not None, f"the server connects {bus} at start and names its demo sensor")
        node_id, node = found
        status = server.request("GET", "/api/status")[1]
        check(status["status"] == "running" and status["can_interface"] == bus,
              f"/api/status says running on {bus}")

        # 2
        registered = server.wait_until(
            lambda: "registered" in (data_dir / "server.log").read_text(errors="replace"))
        check(bool(registered), f"node {node_id} ({node['name']}) is registered")
        code, body = server.request("GET", f"/api/registers/{node_id}")
        names = {r["register_name"]: r for r in body.get("registers", [])} if code == 200 else {}
        check(code == 200 and "uavcan.node.description" in names,
              f"its registers read over HTTP ({len(names)} of them)")
        code, body = server.request("POST", f"/api/registers/{node_id}/set",
                                    {"name": "uavcan.node.description", "value": "api walk", "type": "string"})
        check(code == 200 and body.get("value") == "api walk", f"a register write reads back: {code} {body}")
        code, body = server.request("POST", f"/api/services/{node_id}/430/call", {"attributes": {}})
        check(code == 200 and body.get("status") == "ok", f"a GetInfo service call answers: {code}")

        # 3
        texts = asyncio.run(stream_messages(server, 4.0))
        events = [json.loads(t) for t in texts]  # raises, failing the run, on a message that is not JSON
        check(any("subject_id" in e for e in events), f"/ws sends telemetry ({len(events)} messages, all JSON)")

        # 4
        code, body = server.request("POST", "/api/rawlogs", {})
        name = body.get("name") if isinstance(body, dict) else None
        check(code in (200, 201) and name, f"a raw log starts: {code}")
        time.sleep(2)
        server.request("POST", "/api/rawlogs/stop", {})
        code, raw = server.request("GET", f"/api/rawlogs/{name}")
        lines = raw.decode().splitlines() if isinstance(raw, bytes) else []
        check(code == 200 and len(lines) > 5 and lines[0].startswith("("),
              f"it downloads as candump lines ({len(lines)})")

        # 5
        code, _ = server.request("POST", "/api/can/connect", {"interface": bus})
        check(code == 409, f"a second connect is refused while connected: {code}")
        server.request("POST", "/api/can/disconnect", {})
        code, body = server.request("POST", "/api/can/connect", {"interface": "udp_multicast:239.0.0.1",
                                                                  "bitrate": 500000})
        check(code == 400 and "--allow-any-interface" in body.get("error", ""),
              f"an interface type the API does not open is refused: {code}")

        # 6
        code, body = server.request("POST", "/api/can/connect", {"interface": bus, "listen_only": True})
        check(code == 200 and body.get("listen_only") is True, f"a listen-only session connects: {code}")
        server.wait_until(lambda: named_node(server) or server.request("GET", "/api/nodes")[1]["nodes"], 15)
        code, body = server.request("GET", f"/api/registers/{node_id}")
        check(code == 409 and "listens only" in body.get("error", ""), f"its register reads are refused: {code}")
    finally:
        server.stop()

    # 7
    token_dir = data_dir / "token"
    token_dir.mkdir()
    server = Server(token_dir, [], token="walk-token-1234")
    try:
        up = server.wait_until(lambda: server.request("GET", "/api/health")[0] == 200)
        check(bool(up), "a server with a token starts")
        without = server.request("GET", "/api/status", token="wrong-token")[0]
        with_token = server.request("GET", "/api/status")[0]
        check((without, with_token) == (401, 200), f"it refuses a wrong token and takes its own: {without}, {with_token}")
    finally:
        server.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--fd", action="store_true", help="walk on sim:fd instead of sim:classic")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        walk(Path(tmp), "sim:fd" if args.fd else "sim:classic")
    print("All checks passed")


if __name__ == "__main__":
    main()
