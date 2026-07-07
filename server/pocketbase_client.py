"""PocketBase persistence (issue #16): same write-through contract as the
old SQLite layer (server/persistence.py), backed by a PocketBase instance's
REST API instead of a local file.

Method names mirror ``persistence.Database`` so callers keep their shape,
with two deliberate differences:

* Everything is ``async`` — these are HTTP calls, not blocking disk I/O, so
  callers ``await`` them directly instead of via ``asyncio.to_thread``.
* Record ids are PocketBase's opaque strings, not SQLite ints. They never
  leave the server as ints anywhere (checked), so the change is contained
  to the row dataclasses here and ``engine.RoundRuntime.row_id``.

Composite keys (players on (session, player_key), answers on
(round, player_key)) don't exist in PocketBase; they're emulated with a
unique index plus an in-client id cache seeded by the load_* calls, so a
known upsert is one PATCH and only a genuinely new key pays the
filter-then-create round trip.

Nullable *numeric* columns (gid, floor coordinates, opened_at/closed_at)
are stored as PocketBase ``json`` fields: PB returns zero-values, not null,
for empty number fields, and 0 is a legitimate gid/coordinate — json is the
only field type that round-trips None faithfully. Nullable text maps
'' <-> None (all such columns are either None or non-empty by construction).

Writes get a small bounded retry: transient HTTP failures are a new
failure mode local SQLite never had, and callers assume a persistence
write basically always succeeds. Startup must hard-fail if PocketBase is
unreachable — unlike tracking, there is no degraded mode without
persistence — so ``connect()`` raises instead of retrying forever.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

log = logging.getLogger("blackbox_runner.pocketbase")

_PAGE_SIZE = 500
_WRITE_RETRIES = 3
_RETRY_BACKOFF_S = (0.2, 0.5, 1.0)


class PocketBaseError(RuntimeError):
    """A PocketBase request failed after retries (or was rejected)."""


# --------------------------------------------------------------------- #
# Row shapes — same fields as persistence.py's rows, ids as str
# --------------------------------------------------------------------- #
@dataclass(slots=True)
class PlayerRow:
    id: str  # business key ("seat-1"), not the PB record id
    session_id: str
    gid: Optional[int]
    display_name: Optional[str]
    state: str
    last_seen_x: Optional[float]
    last_seen_y: Optional[float]
    last_seen_at: Optional[float]


@dataclass(slots=True)
class BindingEventRow:
    id: str
    session_id: str
    player_id: str
    old_gid: Optional[int]
    new_gid: Optional[int]
    reason: str
    actor: Optional[str]
    at: float


@dataclass(slots=True)
class RoundRow:
    id: str
    session_id: str
    idx: int
    question_id: str
    state: str
    opened_at: Optional[float]
    closed_at: Optional[float]


@dataclass(slots=True)
class AnswerRow:
    round_id: str
    session_id: str
    player_id: str
    zone_id: Optional[str]
    resolved: str
    position_x: Optional[float]
    position_y: Optional[float]
    at: float


@dataclass(slots=True)
class ScoreEventRow:
    id: str
    session_id: str
    player_id: str
    round_id: Optional[str]
    points: int
    reason: str
    at: float


@dataclass(slots=True)
class ContentRoundRow:
    """One show round as stored in content_rounds. ``form_labels`` and
    ``options`` are held parsed (dict / list of dicts) — PocketBase json
    fields (de)serialize natively. ``zone_layout`` stays None unless the
    author set it explicitly, so a form change keeps re-deriving the layout
    (content.py's FORM_LAYOUTS)."""

    round_id: str
    ord: int
    question: str
    type: str
    duration_s: float
    grace_s: float
    points: int
    text: Optional[str]
    audio: Optional[str]
    form: str
    zone_layout: Optional[str]
    form_labels: dict
    options: list


@dataclass(slots=True)
class SessionRow:
    id: str
    started_at: float
    content_version: str
    status: str


def _q(value: str) -> str:
    """A string literal for a PocketBase filter expression."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _none_if_empty(value: str) -> Optional[str]:
    return value if value else None


class PocketBaseClient:
    def __init__(self, url: str, admin_email: str, admin_password: str) -> None:
        self._base = url.rstrip("/")
        self._email = admin_email
        self._password = admin_password
        self._http = httpx.AsyncClient(base_url=self._base, timeout=10.0)
        self._token: Optional[str] = None
        # Composite-key upsert emulation: business key -> PB record id.
        self._player_ids: dict[tuple[str, str], str] = {}
        self._answer_ids: dict[tuple[str, str], str] = {}
        self._content_meta_id: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Connection / auth
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        """Health-check and authenticate. Raises on failure — persistence
        has no degraded mode, so the caller should let this kill startup."""
        try:
            resp = await self._http.get("/api/health")
            resp.raise_for_status()
        except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
            raise PocketBaseError(f"PocketBase unreachable at {self._base}: {exc}") from exc
        await self._authenticate()
        log.info("Connected to PocketBase at %s", self._base)

    async def _authenticate(self) -> None:
        try:
            resp = await self._http.post(
                "/api/collections/_superusers/auth-with-password",
                json={"identity": self._email, "password": self._password},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise PocketBaseError(f"PocketBase superuser auth failed: {exc}") from exc
        self._token = resp.json()["token"]

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ #
    # Low-level request helpers
    # ------------------------------------------------------------------ #
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        """One PocketBase API call with bounded retry on transient failures
        and a single re-auth on 401 (superuser tokens expire)."""
        if self._token is None:
            await self._authenticate()
        reauthed = False
        last_exc: Optional[Exception] = None
        for attempt in range(_WRITE_RETRIES):
            try:
                resp = await self._http.request(
                    method,
                    path,
                    json=json,
                    params=params,
                    headers={"Authorization": self._token or ""},
                )
            except httpx.TransportError as exc:
                last_exc = exc
                log.warning("PocketBase %s %s transport error (attempt %d): %s",
                            method, path, attempt + 1, exc)
                await asyncio.sleep(_RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)])
                continue

            if resp.status_code == 401 and not reauthed:
                reauthed = True
                await self._authenticate()
                continue
            if resp.status_code >= 500:
                last_exc = PocketBaseError(f"{method} {path} -> {resp.status_code}: {resp.text}")
                await asyncio.sleep(_RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)])
                continue
            if resp.status_code >= 400:
                raise PocketBaseError(f"{method} {path} -> {resp.status_code}: {resp.text}")
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        raise PocketBaseError(
            f"{method} {path} failed after {_WRITE_RETRIES} attempts: {last_exc}"
        )

    async def _list_all(
        self,
        collection: str,
        *,
        filter_: Optional[str] = None,
        sort: Optional[str] = None,
    ) -> list[dict]:
        """Every record matching the filter, across pages."""
        items: list[dict] = []
        page = 1
        while True:
            params: dict = {"page": page, "perPage": _PAGE_SIZE, "skipTotal": 1}
            if filter_:
                params["filter"] = filter_
            if sort:
                params["sort"] = sort
            data = await self._request(
                "GET", f"/api/collections/{collection}/records", params=params
            )
            batch = data.get("items", [])
            items.extend(batch)
            if len(batch) < _PAGE_SIZE:
                return items
            page += 1

    async def _create(self, collection: str, body: dict) -> dict:
        return await self._request("POST", f"/api/collections/{collection}/records", json=body)

    async def _update(self, collection: str, record_id: str, body: dict) -> dict:
        return await self._request(
            "PATCH", f"/api/collections/{collection}/records/{record_id}", json=body
        )

    async def _delete(self, collection: str, record_id: str) -> None:
        await self._request("DELETE", f"/api/collections/{collection}/records/{record_id}")

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #
    async def get_active_session_id(self) -> Optional[str]:
        data = await self._request(
            "GET",
            "/api/collections/sessions/records",
            params={
                "filter": "status='active'",
                "sort": "-started_at",
                "perPage": 1,
                "skipTotal": 1,
            },
        )
        items = data.get("items", [])
        return items[0]["id"] if items else None

    async def create_session(self, content_version: str = "") -> str:
        rec = await self._create(
            "sessions",
            {"started_at": time.time(), "content_version": content_version, "status": "active"},
        )
        return rec["id"]

    async def list_sessions(self) -> list[SessionRow]:
        records = await self._list_all("sessions", sort="started_at")
        return [
            SessionRow(
                id=r["id"],
                started_at=r["started_at"],
                content_version=r.get("content_version") or "",
                status=r["status"],
            )
            for r in records
        ]

    # ------------------------------------------------------------------ #
    # Players
    # ------------------------------------------------------------------ #
    def _player_from_record(self, r: dict) -> PlayerRow:
        return PlayerRow(
            id=r["player_key"],
            session_id=r["session"],
            gid=r.get("gid"),
            display_name=_none_if_empty(r.get("display_name") or ""),
            state=r["state"],
            last_seen_x=r.get("last_seen_x"),
            last_seen_y=r.get("last_seen_y"),
            last_seen_at=r.get("last_seen_at"),
        )

    async def load_players(self, session_id: str) -> list[PlayerRow]:
        records = await self._list_all("players", filter_=f"session={_q(session_id)}")
        for r in records:
            self._player_ids[(session_id, r["player_key"])] = r["id"]
        return [self._player_from_record(r) for r in records]

    async def upsert_player(self, row: PlayerRow) -> None:
        body = {
            "session": row.session_id,
            "player_key": row.id,
            "gid": row.gid,
            "display_name": row.display_name or "",
            "state": row.state,
            "last_seen_x": row.last_seen_x,
            "last_seen_y": row.last_seen_y,
            "last_seen_at": row.last_seen_at,
        }
        key = (row.session_id, row.id)
        pb_id = self._player_ids.get(key)
        if pb_id is None:
            found = await self._list_all(
                "players",
                filter_=f"session={_q(row.session_id)} && player_key={_q(row.id)}",
            )
            if found:
                pb_id = found[0]["id"]
                self._player_ids[key] = pb_id
        if pb_id is not None:
            await self._update("players", pb_id, body)
        else:
            rec = await self._create("players", body)
            self._player_ids[key] = rec["id"]

    # ------------------------------------------------------------------ #
    # Binding events (audit trail)
    # ------------------------------------------------------------------ #
    async def record_binding_event(
        self,
        session_id: str,
        player_id: str,
        old_gid: Optional[int],
        new_gid: Optional[int],
        reason: str,
        actor: Optional[str] = None,
    ) -> None:
        await self._create(
            "binding_events",
            {
                "session": session_id,
                "player_key": player_id,
                "old_gid": old_gid,
                "new_gid": new_gid,
                "reason": reason,
                "actor": actor or "",
                "at": time.time(),
            },
        )

    async def load_binding_events(self, session_id: str) -> list[BindingEventRow]:
        records = await self._list_all(
            "binding_events", filter_=f"session={_q(session_id)}", sort="at"
        )
        return [
            BindingEventRow(
                id=r["id"],
                session_id=r["session"],
                player_id=r["player_key"],
                old_gid=r.get("old_gid"),
                new_gid=r.get("new_gid"),
                reason=r["reason"],
                actor=_none_if_empty(r.get("actor") or ""),
                at=r["at"],
            )
            for r in records
        ]

    # ------------------------------------------------------------------ #
    # Rounds
    # ------------------------------------------------------------------ #
    async def create_round(self, session_id: str, idx: int, question_id: str) -> str:
        rec = await self._create(
            "rounds",
            {
                "session": session_id,
                "idx": idx,
                "question_id": question_id,
                "state": "pending",
                "opened_at": None,
                "closed_at": None,
            },
        )
        return rec["id"]

    async def update_round_state(
        self,
        round_id: str,
        state: str,
        opened_at: Optional[float],
        closed_at: Optional[float],
    ) -> None:
        await self._update(
            "rounds", round_id, {"state": state, "opened_at": opened_at, "closed_at": closed_at}
        )

    async def load_rounds(self, session_id: str) -> list[RoundRow]:
        records = await self._list_all("rounds", filter_=f"session={_q(session_id)}", sort="idx")
        return [
            RoundRow(
                id=r["id"],
                session_id=r["session"],
                idx=r["idx"],
                question_id=r["question_id"],
                state=r["state"],
                opened_at=r.get("opened_at"),
                closed_at=r.get("closed_at"),
            )
            for r in records
        ]

    # ------------------------------------------------------------------ #
    # Answers
    # ------------------------------------------------------------------ #
    async def record_answer(self, row: AnswerRow) -> None:
        body = {
            "round": row.round_id,
            "session": row.session_id,
            "player_key": row.player_id,
            "zone_id": row.zone_id or "",
            "resolved": row.resolved,
            "position_x": row.position_x,
            "position_y": row.position_y,
            "at": row.at,
        }
        key = (row.round_id, row.player_id)
        pb_id = self._answer_ids.get(key)
        if pb_id is None:
            found = await self._list_all(
                "answers",
                filter_=f"round={_q(row.round_id)} && player_key={_q(row.player_id)}",
            )
            if found:
                pb_id = found[0]["id"]
                self._answer_ids[key] = pb_id
        if pb_id is not None:
            await self._update("answers", pb_id, body)
        else:
            rec = await self._create("answers", body)
            self._answer_ids[key] = rec["id"]

    async def load_answers(self, round_id: str) -> list[AnswerRow]:
        records = await self._list_all("answers", filter_=f"round={_q(round_id)}")
        for r in records:
            self._answer_ids[(round_id, r["player_key"])] = r["id"]
        return [
            AnswerRow(
                round_id=r["round"],
                session_id=r["session"],
                player_id=r["player_key"],
                zone_id=_none_if_empty(r.get("zone_id") or ""),
                resolved=r["resolved"],
                position_x=r.get("position_x"),
                position_y=r.get("position_y"),
                at=r["at"],
            )
            for r in records
        ]

    # ------------------------------------------------------------------ #
    # Score events (scores are always a sum over events, never a counter)
    # ------------------------------------------------------------------ #
    async def record_score_event(
        self,
        session_id: str,
        player_id: str,
        round_id: Optional[str],
        points: int,
        reason: str,
    ) -> None:
        await self._create(
            "score_events",
            {
                "session": session_id,
                "player_key": player_id,
                "round": round_id or "",
                "points": points,
                "reason": reason,
                "at": time.time(),
            },
        )

    async def sum_scores(self, session_id: str) -> dict[str, int]:
        # PocketBase's REST API has no server-side GROUP BY/SUM; fetch the
        # session's events and sum here. Bounded by rounds x cast size.
        totals: dict[str, int] = {}
        for event in await self.load_score_events(session_id):
            totals[event.player_id] = totals.get(event.player_id, 0) + event.points
        return totals

    async def load_score_events(self, session_id: str) -> list[ScoreEventRow]:
        records = await self._list_all(
            "score_events", filter_=f"session={_q(session_id)}", sort="at"
        )
        return [
            ScoreEventRow(
                id=r["id"],
                session_id=r["session"],
                player_id=r["player_key"],
                round_id=_none_if_empty(r.get("round") or ""),
                points=r["points"],
                reason=r["reason"],
                at=r["at"],
            )
            for r in records
        ]

    # ------------------------------------------------------------------ #
    # Show content (imported from the authoring YAML; the DB is the
    # runtime source of truth)
    # ------------------------------------------------------------------ #
    async def load_content(self) -> tuple[str, list[ContentRoundRow]]:
        """Returns (version, rounds ordered by ord). No content imported yet
        is a valid empty state: ("", [])."""
        meta = await self._request(
            "GET",
            "/api/collections/content_meta/records",
            params={"perPage": 1, "skipTotal": 1},
        )
        meta_items = meta.get("items", [])
        version = ""
        if meta_items:
            self._content_meta_id = meta_items[0]["id"]
            version = meta_items[0].get("version") or ""

        records = await self._list_all("content_rounds", sort="ord")
        rows = [
            ContentRoundRow(
                round_id=r["round_id"],
                ord=r["ord"],
                question=r.get("question") or "",
                type=r["type"],
                duration_s=r["duration_s"],
                grace_s=r["grace_s"],
                points=r["points"],
                text=_none_if_empty(r.get("text") or ""),
                audio=_none_if_empty(r.get("audio") or ""),
                form=r["form"],
                zone_layout=_none_if_empty(r.get("zone_layout") or ""),
                form_labels=r.get("form_labels") or {},
                options=r.get("options") or [],
            )
            for r in records
        ]
        return version, rows

    async def save_content(self, version: str, rows: list[ContentRoundRow]) -> None:
        """Wipe-and-replace the whole show. PocketBase has no cross-record
        transaction over REST, so unlike the SQLite layer this has a small
        non-atomic window (documented MVP gap); the count check at the end
        catches a partial replace immediately instead of silently."""
        existing = await self._list_all("content_rounds")
        for record in existing:
            await self._delete("content_rounds", record["id"])

        for r in rows:
            await self._create(
                "content_rounds",
                {
                    "round_id": r.round_id,
                    "ord": r.ord,
                    "question": r.question,
                    "type": r.type,
                    "duration_s": r.duration_s,
                    "grace_s": r.grace_s,
                    "points": r.points,
                    "text": r.text or "",
                    "audio": r.audio or "",
                    "form": r.form,
                    "zone_layout": r.zone_layout or "",
                    "form_labels": r.form_labels,
                    "options": r.options,
                },
            )

        if self._content_meta_id is None:
            meta = await self._request(
                "GET",
                "/api/collections/content_meta/records",
                params={"perPage": 1, "skipTotal": 1},
            )
            items = meta.get("items", [])
            if items:
                self._content_meta_id = items[0]["id"]
        if self._content_meta_id is not None:
            await self._update("content_meta", self._content_meta_id, {"version": version})
        else:
            rec = await self._create("content_meta", {"version": version})
            self._content_meta_id = rec["id"]

        count = await self._request(
            "GET", "/api/collections/content_rounds/records", params={"perPage": 1}
        )
        total = count.get("totalItems", -1)
        if total != len(rows):
            raise PocketBaseError(
                f"content replace incomplete: expected {len(rows)} round(s), found {total}"
            )
