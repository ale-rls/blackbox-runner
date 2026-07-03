"""Async client for TrackingBox's ``/ws`` and ``/api/zones``.

Reconnects with exponential backoff. On every (re)connect it takes the fresh
``snapshot`` as the new source of truth rather than trying to patch prior
state — this is deliberate: a TrackingBox restart resets all GIDs, and the
same resync path that handles a network blip also handles that case without
special-casing it. Downstream consumers (the binding manager, in a later
phase) see this as a ``resync`` event and treat every previously-bound GID as
provisionally lost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

import httpx
import websockets

from .models import AudienceSummary, ZoneMap

log = logging.getLogger("blackbox_runner.tracking_client")


@dataclass(slots=True)
class ResyncEvent:
    gids: set[int]


@dataclass(slots=True)
class ChangeEvent:
    gid: int
    state: Optional[AudienceSummary]  # None when the GID went invisible


TrackingEvent = ResyncEvent | ChangeEvent


@dataclass(slots=True)
class _HistoryPoint:
    at: float
    floor: tuple[float, float]


async def fetch_zones(
    http_url: str,
    *,
    timeout: float = 10.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> ZoneMap:
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        resp = await client.get(f"{http_url.rstrip('/')}/api/zones")
        resp.raise_for_status()
        return ZoneMap.model_validate(resp.json())


class TrackingClient:
    """Maintains a live mirror of TrackingBox audience state.

    Call :meth:`run` as a background task. Consumers subscribe with
    :meth:`events` for a live async stream, or poll :meth:`get`/:meth:`get_all`
    for current state and :meth:`history` for recent floor positions.
    """

    def __init__(
        self,
        ws_url: str,
        *,
        reconnect_initial_s: float = 0.5,
        reconnect_max_s: float = 30.0,
        history_seconds: float = 5.0,
    ) -> None:
        self._ws_url = ws_url
        self._reconnect_initial_s = reconnect_initial_s
        self._reconnect_max_s = reconnect_max_s
        self._history_seconds = history_seconds

        self._state: dict[int, AudienceSummary] = {}
        self._history: dict[int, deque[_HistoryPoint]] = {}
        self._subscribers: set[asyncio.Queue[TrackingEvent]] = set()
        self._stopped = False
        self._connected = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def wait_connected(self, timeout: Optional[float] = None) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout)

    def stop(self) -> None:
        self._stopped = True

    # ------------------------------------------------------------------ #
    # Read side
    # ------------------------------------------------------------------ #
    def get(self, gid: int) -> Optional[AudienceSummary]:
        return self._state.get(gid)

    def get_all(self) -> dict[int, AudienceSummary]:
        return dict(self._state)

    def history(self, gid: int, seconds: Optional[float] = None) -> list[tuple[float, float, float]]:
        """Recent (at, x, y) floor points for ``gid``, oldest first."""
        window = seconds if seconds is not None else self._history_seconds
        now = time.time()
        points = self._history.get(gid)
        if not points:
            return []
        return [(p.at, p.floor[0], p.floor[1]) for p in points if now - p.at <= window]

    # ------------------------------------------------------------------ #
    # Subscription
    # ------------------------------------------------------------------ #
    def subscribe(self) -> "asyncio.Queue[TrackingEvent]":
        q: asyncio.Queue[TrackingEvent] = asyncio.Queue(maxsize=1024)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[TrackingEvent]") -> None:
        self._subscribers.discard(q)

    async def events(self) -> AsyncIterator[TrackingEvent]:
        q = self.subscribe()
        try:
            while True:
                yield await q.get()
        finally:
            self.unsubscribe(q)

    def _publish(self, event: TrackingEvent) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("Dropping tracking event: subscriber queue full")

    # ------------------------------------------------------------------ #
    # Connection loop
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        backoff = self._reconnect_initial_s
        while not self._stopped:
            try:
                async with websockets.connect(self._ws_url, open_timeout=10) as ws:
                    log.info("Connected to TrackingBox at %s", self._ws_url)
                    backoff = self._reconnect_initial_s
                    self._connected.set()
                    async for raw in ws:
                        self._handle_message(raw)
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                log.warning("TrackingBox connection lost (%s); reconnecting in %.1fs", exc, backoff)
            finally:
                self._connected.clear()
            if self._stopped:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._reconnect_max_s)

    def _handle_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            log.warning("Ignoring malformed TrackingBox message")
            return
        if isinstance(msg, dict) and msg.get("type") == "snapshot":
            self._apply_snapshot(msg.get("data") or {})
        elif isinstance(msg, dict) and "gid" in msg:
            self._apply_change(msg)
        else:
            log.warning("Ignoring unrecognized TrackingBox message shape")

    def _apply_snapshot(self, data: dict) -> None:
        people = data.get("people") or []
        new_state: dict[int, AudienceSummary] = {}
        for raw in people:
            try:
                summary = AudienceSummary.model_validate(raw)
            except Exception:
                continue
            if summary.visible:
                new_state[summary.gid] = summary
                self._record_history(summary)
        self._state = new_state
        self._publish(ResyncEvent(gids=set(new_state)))

    def _apply_change(self, raw: dict) -> None:
        try:
            summary = AudienceSummary.model_validate(raw)
        except Exception:
            log.warning("Ignoring malformed change event: %r", raw)
            return
        if summary.visible:
            self._state[summary.gid] = summary
            self._record_history(summary)
            self._publish(ChangeEvent(gid=summary.gid, state=summary))
        else:
            self._state.pop(summary.gid, None)
            self._publish(ChangeEvent(gid=summary.gid, state=None))

    def _record_history(self, summary: AudienceSummary) -> None:
        if not summary.floor_valid or summary.floor is None:
            return
        now = time.time()
        points = self._history.setdefault(summary.gid, deque())
        points.append(_HistoryPoint(at=now, floor=summary.floor))
        cutoff = now - self._history_seconds
        while points and points[0].at < cutoff:
            points.popleft()
