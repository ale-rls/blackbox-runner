"""Shared fixtures: fakes for TrackingBox's WS and REST surface.

These speak the pinned contract (see docs/architecture.md) rather than
importing TrackingBox itself, so the test suite has no dependency on its
(heavy, ML-laden) install.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import websockets

SNAPSHOT = {
    "type": "snapshot",
    "data": {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "active_people": 2,
        "zone_counts": {"answer_a": 1, "answer_b": 1},
        "people": [
            {
                "gid": 1,
                "visible": True,
                "center": [100, 200],
                "bbox": [90, 150, 110, 250],
                "floor": [0.25, 0.5],
                "floor_valid": True,
                "zone": "answer_a",
                "confidence": 0.9,
                "first_seen": "2026-01-01T00:00:00+00:00",
                "last_seen": "2026-01-01T00:00:00+00:00",
                "duration_seen_seconds": 1.0,
            },
            {
                "gid": 2,
                "visible": True,
                "center": [400, 200],
                "bbox": [390, 150, 410, 250],
                "floor": [0.75, 0.5],
                "floor_valid": True,
                "zone": "answer_b",
                "confidence": 0.9,
                "first_seen": "2026-01-01T00:00:00+00:00",
                "last_seen": "2026-01-01T00:00:00+00:00",
                "duration_seen_seconds": 1.0,
            },
        ],
    },
}

CHANGE_MOVE_GID_1 = {
    "gid": 1,
    "visible": True,
    "center": [120, 200],
    "bbox": [110, 150, 130, 250],
    "floor": [0.30, 0.5],
    "floor_valid": True,
    "zone": "answer_a",
}

CHANGE_GID_2_GONE = {
    "gid": 2,
    "visible": False,
    "center": None,
    "bbox": None,
    "floor": None,
    "floor_valid": False,
    "zone": None,
}

ZONES_RESPONSE = {
    "enabled": True,
    "default_zone": "outside",
    "zones": [
        {"id": "answer_a", "label": "Answer A", "points": [[0, 0], [0.5, 0], [0.5, 1], [0, 1]]},
        {"id": "answer_b", "label": "Answer B", "points": [[0.5, 0], [1, 0], [1, 1], [0.5, 1]]},
    ],
}


class FakeTrackingBox:
    """Minimal stand-in for TrackingBox's ``/ws`` endpoint."""

    def __init__(self) -> None:
        self.connection_count = 0
        self._server: websockets.WebSocketServer | None = None
        self.port: int | None = None
        self.close_after_change = False

    async def _handler(self, ws: websockets.WebSocketServerProtocol) -> None:
        self.connection_count += 1
        await ws.send(json.dumps(SNAPSHOT))
        await ws.send(json.dumps(CHANGE_MOVE_GID_1))
        await ws.send(json.dumps(CHANGE_GID_2_GONE))
        if self.close_after_change:
            await ws.close()
            return
        await ws.wait_closed()

    async def start(self) -> None:
        self._server = await websockets.serve(self._handler, "localhost", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def ws_url(self) -> str:
        return f"ws://localhost:{self.port}/ws"


@pytest.fixture
async def fake_backend():
    backend = FakeTrackingBox()
    await backend.start()
    try:
        yield backend
    finally:
        await backend.stop()


class _ZonesHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        if self.path == "/api/zones":
            body = json.dumps(ZONES_RESPONSE).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args) -> None:  # silence stdlib access logs
        pass


@pytest.fixture
def fake_zones_http():
    server = HTTPServer(("localhost", 0), _ZonesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://localhost:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
