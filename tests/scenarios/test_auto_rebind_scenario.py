"""Scripted churn scenario: drop a GID and respawn a fresh one nearby.

This is the auto-rebind path (docs/architecture.md §4.2, §7). Asserts on
the resulting binding_events and players rows, not just in-memory state —
the audit trail is the thing that has to hold up after the show.
"""

from __future__ import annotations

import pytest

from server.bindings import BindingManager, PlayerState
from server.models import AudienceSummary
from server.tracking_client import ChangeEvent, TrackingClient


def _seed(tracking: TrackingClient, gid: int, floor: tuple[float, float]) -> None:
    tracking._state[gid] = AudienceSummary(gid=gid, visible=True, floor=floor, floor_valid=True)


@pytest.mark.asyncio
async def test_gid_drop_and_nearby_respawn_auto_rebinds(pb):
    db = pb
    tracking = TrackingClient("ws://unused")
    session_id = await db.create_session()
    manager = await BindingManager.load(
        db, session_id, tracking, rebind_max_distance=0.1, rebind_max_gap_s=5.0
    )

    try:
        # A player is happily bound, standing at floor (0.40, 0.60).
        _seed(tracking, 501, (0.40, 0.60))
        player = await manager.claim("seat-14", 501)
        assert player.state == PlayerState.BOUND

        # Their tracker drops (occlusion, brief exit from frame, whatever).
        await manager.handle_tracking_event(ChangeEvent(gid=501, state=None))
        assert manager.get("seat-14").state == PlayerState.LOST

        # A fresh GID appears a beat later, right where they were standing.
        _seed(tracking, 502, (0.41, 0.605))
        await manager.handle_tracking_event(ChangeEvent(gid=502, state=tracking.get(502)))

        # Resolved silently: no operator, no ritual, just a clean rebind.
        recovered = manager.get("seat-14")
        assert recovered.state == PlayerState.BOUND
        assert recovered.gid == 502

        # The audit trail records exactly what happened and why.
        events = await db.load_binding_events(session_id)
        reasons = [e.reason for e in events]
        assert reasons == ["claim", "lost", "auto_rebind"]
        assert events[-1].old_gid is None
        assert events[-1].new_gid == 502
        assert events[-1].actor is None  # silent, not operator-driven

        rows = await db.load_players(session_id)
        assert len(rows) == 1
        assert rows[0].gid == 502
        assert rows[0].state == "bound"
    finally:
        manager.shutdown()
