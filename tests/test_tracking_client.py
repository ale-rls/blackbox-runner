"""Tests against a fake TrackingBox WS server that speaks the real contract:
a ``{"type": "snapshot", ...}`` full state on connect, then bare per-GID
change events with no ``type`` key.
"""

from __future__ import annotations

import asyncio

import pytest

from server.tracking_client import ChangeEvent, ResyncEvent, TrackingClient


async def _collect(queue: "asyncio.Queue", n: int) -> list:
    return [await queue.get() for _ in range(n)]


@pytest.mark.asyncio
async def test_snapshot_and_change_events(fake_backend):
    client = TrackingClient(fake_backend.ws_url)
    queue = client.subscribe()
    task = asyncio.create_task(client.run())
    try:
        events = await asyncio.wait_for(_collect(queue, 3), timeout=5)

        assert isinstance(events[0], ResyncEvent)
        assert events[0].gids == {1, 2}

        assert isinstance(events[1], ChangeEvent)
        assert events[1].gid == 1
        assert events[1].state is not None
        assert events[1].state.floor == (0.30, 0.5)

        assert isinstance(events[2], ChangeEvent)
        assert events[2].gid == 2
        assert events[2].state is None

        # State mirrors what the events implied.
        assert client.get(1).zone == "answer_a"
        assert client.get(2) is None

        history = client.history(1)
        assert len(history) >= 1
    finally:
        client.unsubscribe(queue)
        client.stop()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_reconnect_resyncs_from_fresh_snapshot(fake_backend):
    fake_backend.close_after_change = True
    client = TrackingClient(
        fake_backend.ws_url,
        reconnect_initial_s=0.05,
        reconnect_max_s=0.1,
    )
    queue = client.subscribe()
    task = asyncio.create_task(client.run())
    try:
        # First connection: snapshot + 2 changes, then server closes it.
        await asyncio.wait_for(_collect(queue, 3), timeout=5)
        # Client should reconnect and receive a fresh ResyncEvent.
        events = await asyncio.wait_for(_collect(queue, 1), timeout=5)
        assert isinstance(events[0], ResyncEvent)
        assert fake_backend.connection_count >= 2
    finally:
        client.unsubscribe(queue)
        client.stop()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_fetch_zones_parses_response(fake_zones_http):
    from server.tracking_client import fetch_zones

    zones = await fetch_zones(fake_zones_http)
    assert zones.enabled
    assert zones.zone_ids() == {"answer_a", "answer_b", "outside"}
