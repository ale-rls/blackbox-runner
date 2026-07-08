"""In-process fake of PocketBase's records REST API, for fast offline
tests (same convention as conftest.py's FakeTrackingBox: speak the pinned
wire contract, no dependency on the real binary).

Implements only the surface server/pocketbase_client.py actually uses:
superuser auth-with-password, health, and per-collection record CRUD with
``filter`` (conjunctions of field='value'), ``sort`` (single field,
optional leading -), ``page``/``perPage``, and ``totalItems``.

State lives in ``self.store`` ({collection: {record_id: record}}), which
tests may inspect or pre-seed directly. One deliberate fidelity choice:
like real PocketBase, unknown collections 404 and missing auth 401s, so a
client bug can't silently pass here and fail live.
"""

from __future__ import annotations

import json
import random
import re
import string
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_FILTER_CLAUSE = re.compile(r"^\s*(\w+)\s*=\s*'((?:[^'\\]|\\.)*)'\s*$")

_GAME_COLLECTIONS = (
    "sessions",
    "players",
    "binding_events",
    "rounds",
    "answers",
    "score_events",
    "content_meta",
    "content_rounds",
    # issue #16: the deployed phone's PocketBase-only bridge.
    "game_state",
    "claim_requests",
    "player_reveals",
)


def _new_id() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=15))


def _parse_filter(expr: str) -> list[tuple[str, str]]:
    clauses = []
    for part in expr.split("&&"):
        m = _FILTER_CLAUSE.match(part)
        if not m:
            raise ValueError(f"fake PocketBase can't parse filter clause: {part!r}")
        clauses.append((m.group(1), m.group(2).replace("\\'", "'").replace("\\\\", "\\")))
    return clauses


class FakePocketBase:
    def __init__(self) -> None:
        self.store: dict[str, dict[str, dict]] = defaultdict(dict)
        self.lock = threading.Lock()
        self.auth_calls = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    @property
    def url(self) -> str:
        # 127.0.0.1 explicitly: "localhost" makes httpx try ::1 first while
        # the server listens on IPv4 only — on Windows that per-connection
        # fallback stall is the difference between fast and unusable.
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            # Keep-alive (every response carries Content-Length), so httpx
            # reuses one connection instead of reconnecting per request.
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def _send(self, code: int, body: dict | None = None) -> None:
                data = json.dumps(body or {}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self) -> dict:
                """JSON or multipart/form-data (file uploads). For file
                parts, store the filename string — the shape real
                PocketBase returns for a file field (modulo its random
                suffix, which nothing in the tests depends on)."""
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                ctype = self.headers.get("Content-Type") or ""
                if not ctype.startswith("multipart/form-data"):
                    return json.loads(raw or b"{}")

                import email.parser
                import email.policy

                msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                    b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw
                )
                record: dict = {}
                for part in msg.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    if name is None:
                        continue
                    if part.get_filename():
                        record[name] = part.get_filename()
                        continue
                    text = (part.get_payload(decode=True) or b"").decode()
                    try:
                        # The client stringifies multipart form values
                        # (numbers via str(), json fields via json.dumps);
                        # real PocketBase coerces them back by field type.
                        record[name] = json.loads(text)
                    except (ValueError, TypeError):
                        record[name] = text
                return record

            def _authed(self) -> bool:
                return bool(self.headers.get("Authorization"))

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/api/health":
                    self._send(200, {"message": "API is healthy.", "code": 200})
                    return
                m = re.match(r"^/api/collections/(\w+)/records$", parsed.path)
                if not m:
                    self._send(404, {"message": "not found"})
                    return
                if not self._authed():
                    self._send(401, {"message": "missing auth"})
                    return
                coll = m.group(1)
                if coll not in _GAME_COLLECTIONS:
                    self._send(404, {"message": f"unknown collection {coll}"})
                    return
                qs = parse_qs(parsed.query)
                with fake.lock:
                    records = list(fake.store[coll].values())
                if "filter" in qs:
                    clauses = _parse_filter(qs["filter"][0])
                    records = [
                        r for r in records
                        if all(str(r.get(f) or "") == v for f, v in clauses)
                    ]
                if "sort" in qs:
                    key = qs["sort"][0]
                    reverse = key.startswith("-")
                    key = key.lstrip("-")
                    records.sort(key=lambda r: (r.get(key) is None, r.get(key)), reverse=reverse)
                total = len(records)
                page = int(qs.get("page", ["1"])[0])
                per_page = int(qs.get("perPage", ["30"])[0])
                items = records[(page - 1) * per_page : page * per_page]
                body = {"page": page, "perPage": per_page, "items": items}
                if "skipTotal" not in qs:
                    body["totalItems"] = total
                self._send(200, body)

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/api/collections/_superusers/auth-with-password":
                    fake.auth_calls += 1
                    self._send(200, {"token": f"fake-token-{fake.auth_calls}", "record": {}})
                    return
                m = re.match(r"^/api/collections/(\w+)/records$", parsed.path)
                if not m:
                    self._send(404, {"message": "not found"})
                    return
                if not self._authed():
                    self._send(401, {"message": "missing auth"})
                    return
                coll = m.group(1)
                if coll not in _GAME_COLLECTIONS:
                    self._send(404, {"message": f"unknown collection {coll}"})
                    return
                record = self._body()
                record["id"] = _new_id()
                with fake.lock:
                    fake.store[coll][record["id"]] = record
                self._send(200, record)

            def do_PATCH(self) -> None:
                parsed = urlparse(self.path)
                m = re.match(r"^/api/collections/(\w+)/records/(\w+)$", parsed.path)
                if not m or not self._authed():
                    self._send(401 if m else 404, {"message": "bad request"})
                    return
                coll, rid = m.group(1), m.group(2)
                with fake.lock:
                    record = fake.store[coll].get(rid)
                    if record is None:
                        self._send(404, {"message": f"no record {rid} in {coll}"})
                        return
                    record.update(self._body())
                    record["id"] = rid
                self._send(200, record)

            def do_DELETE(self) -> None:
                parsed = urlparse(self.path)
                m = re.match(r"^/api/collections/(\w+)/records/(\w+)$", parsed.path)
                if not m or not self._authed():
                    self._send(401 if m else 404, {"message": "bad request"})
                    return
                coll, rid = m.group(1), m.group(2)
                with fake.lock:
                    if fake.store[coll].pop(rid, None) is None:
                        self._send(404, {"message": f"no record {rid} in {coll}"})
                        return
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self._server = ThreadingHTTPServer(("localhost", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
