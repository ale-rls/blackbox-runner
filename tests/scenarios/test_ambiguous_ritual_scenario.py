"""Scripted churn scenario: drop two GIDs near each other and respawn two.

This is the ambiguity -> orphaned -> ritual path (docs/architecture.md
§4.2, §7). The binding layer must never guess between two plausible
candidates; both players sit as orphaned until a ritual (or operator)
rebind resolves them. Asserts on binding_events, matching the plan's
"no orphan lasts longer than one ritual" exit criterion.
"""

from __future__ import annotations

import asyncio

import pytest

from server.bindings import BindingManager, PlayerState
from server.models import AudienceSummary
from server.persistence import Database
from server.tracking_client import ChangeEvent, TrackingClient


def _seed(tracking: TrackingClient, gid: int, floor: tuple[float, float], zone=None) -> None:
    tracking._state[gid] = AudienceSummary(
        gid=gid, visible=True, floor=floor, floor_valid=True, zone=zone
    )


@pytest.mark.asyncio
async def test_two_players_lost_together_never_get_guessed_then_ritual_resolves_both():
    db = Database(":memory:")
    tracking = TrackingClient("ws://unused")
    session_id = db.create_session()
    manager = await BindingManager.load(
        db,
        session_id,
        tracking,
        rebind_max_distance=0.1,
        rebind_max_gap_s=5.0,
        orphan_after_s=0.05,
        ritual_zone_id="ritual",
    )

    try:
        # Two players standing close together (a huddle, a dense zone).
        _seed(tracking, 601, (0.50, 0.50))
        _seed(tracking, 602, (0.52, 0.51))
        await manager.claim("seat-3", 601)
        await manager.claim("seat-4", 602)

        # Both drop at once (a clustering mis-track, exactly the scenario
        # docs/architecture.md §4.2 calls out for 40+ person density).
        await manager.handle_tracking_event(ChangeEvent(gid=601, state=None))
        await manager.handle_tracking_event(ChangeEvent(gid=602, state=None))
        assert manager.get("seat-3").state == PlayerState.LOST
        assert manager.get("seat-4").state == PlayerState.LOST

        # Two fresh GIDs appear right in the same spot — equally plausible
        # for either lost player. Never guess.
        _seed(tracking, 701, (0.51, 0.505))
        await manager.handle_tracking_event(ChangeEvent(gid=701, state=tracking.get(701)))
        assert manager.get("seat-3").state == PlayerState.LOST
        assert manager.get("seat-4").state == PlayerState.LOST
        assert manager.player_for_gid(701) is None

        # Both age past the orphan timeout with no confident auto-rebind.
        await asyncio.sleep(0.1)
        assert manager.get("seat-3").state == PlayerState.ORPHANED
        assert manager.get("seat-4").state == PlayerState.ORPHANED

        # Ritual: each of them walks to the lit corner in turn.
        _seed(tracking, 801, (0.9, 0.9), zone="ritual")
        await manager.handle_tracking_event(ChangeEvent(gid=801, state=tracking.get(801)))
        _seed(tracking, 802, (0.9, 0.9), zone="ritual")
        await manager.handle_tracking_event(ChangeEvent(gid=802, state=tracking.get(802)))

        assert manager.get("seat-3").state == PlayerState.BOUND
        assert manager.get("seat-4").state == PlayerState.BOUND
        assert {manager.get("seat-3").gid, manager.get("seat-4").gid} == {801, 802}

        events = db.load_binding_events(session_id)
        ritual_events = [e for e in events if e.reason == "ritual"]
        assert len(ritual_events) == 2
        assert {e.player_id for e in ritual_events} == {"seat-3", "seat-4"}
    finally:
        manager.shutdown()
        db.close()
