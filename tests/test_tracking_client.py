"""Tests against a fake TrackingBox WS server that speaks the real contract:
a ``{"type": "snapshot", ...}`` full state on connect, then either bare
per-GID change events with no ``type`` key (ws_max_rate_hz = 0) or batched
``{"type": "update", "people": [...]}`` messages (rate-limited, the default).
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
async def test_batched_update_events(fake_backend):
    """Rate-limited TrackingBox coalesces changes into ``update`` messages;
    each entry must flow through the same path as a bare change event."""
    fake_backend.batch_changes = True
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

        assert client.get(1).zone == "answer_a"
        assert client.get(2) is None
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
async def test_batched_update_message_applies_each_person():
    """The live TrackingBox sends changes as {"type": "update", "people": [...]}
    batches; each entry must be applied as an individual change event."""
    import json

    client = TrackingClient("ws://unused")
    queue = client.subscribe()
    try:
        update = {
            "type": "update",
            "people": [
                {
                    "gid": 32,
                    "visible": True,
                    "center": [580, 191],
                    "bbox": [541, 66, 620, 315],
                    "floor": [0.7585, 0.5284],
                    "floor_valid": True,
                    "zone": "cross_br",
                },
                {
                    "gid": 33,
                    "visible": True,
                    "center": [100, 200],
                    "bbox": [90, 150, 110, 250],
                    "floor": [0.25, 0.5],
                    "floor_valid": True,
                    "zone": "answer_a",
                },
            ],
        }
        client._handle_message(json.dumps(update))

        # Both GIDs' state updated.
        assert client.get(32) is not None
        assert client.get(32).floor == (0.7585, 0.5284)
        assert client.get(32).zone == "cross_br"
        assert client.get(33) is not None
        assert client.get(33).zone == "answer_a"

        # One ChangeEvent published per person, in order.
        events = [queue.get_nowait() for _ in range(2)]
        assert all(isinstance(e, ChangeEvent) for e in events)
        assert [e.gid for e in events] == [32, 33]
        assert events[0].state is not None and events[1].state is not None

        # A subsequent update batch can also remove a GID (visible: false).
        gone = {
            "type": "update",
            "people": [
                {
                    "gid": 33,
                    "visible": False,
                    "center": None,
                    "bbox": None,
                    "floor": None,
                    "floor_valid": False,
                    "zone": None,
                }
            ],
        }
        client._handle_message(json.dumps(gone))
        assert client.get(33) is None
        event = queue.get_nowait()
        assert isinstance(event, ChangeEvent)
        assert event.gid == 33
        assert event.state is None
    finally:
        client.unsubscribe(queue)


@pytest.mark.asyncio
async def test_fetch_zones_parses_response(fake_zones_http):
    from server.tracking_client import fetch_zones

    zones = await fetch_zones(fake_zones_http)
    assert zones.enabled
    assert zones.zone_ids() == {"answer_a", "answer_b", "outside"}
