"""Scripted churn scenario: the game server itself crashes and restarts
mid-round (docs/architecture.md §5, §7 — persistence recovery).

Combines Phase 1's binding-state recovery (tests/test_crash_recovery.py)
with Phase 4's round-state recovery (GameEngine.load): after "restart",
every player's binding and the in-flight round's answers must be exactly
as they were the instant before the crash, and the round must be
finishable without repeating it.
"""

from __future__ import annotations

import pytest

from server.bindings import BindingManager, PlayerState
from server.content import ShowContent
from server.engine import GameEngine, RoundState
from server.models import AudienceSummary
from server.pocketbase_client import PocketBaseClient
from server.tracking_client import ChangeEvent, TrackingClient

SHOW = {
    "version": "1",
    "rounds": [
        {
            "id": "r1",
            "question": "Coffee or tea?",
            "type": "majority",
            "duration_s": 100,
            "grace_s": 100,
            "points": 10,
            "options": [{"zone": "a", "label": "Coffee"}, {"zone": "b", "label": "Tea"}],
        },
        {
            "id": "r2",
            "question": "Follow-up",
            "type": "majority",
            "duration_s": 100,
            "grace_s": 100,
            "points": 10,
            "options": [{"zone": "a", "label": "A"}, {"zone": "b", "label": "B"}],
        },
    ],
}


def _seed(tracking: TrackingClient, gid: int, zone: str) -> None:
    tracking._state[gid] = AudienceSummary(
        gid=gid, visible=True, floor=(0.1, 0.1), floor_valid=True, zone=zone
    )


@pytest.mark.asyncio
async def test_server_restart_mid_round_preserves_bindings_and_round_state(fake_pocketbase):
    # --- "first run" ---
    db = PocketBaseClient(fake_pocketbase.url, "test@example.com", "pw")
    await db.connect()
    tracking = TrackingClient("ws://unused")
    session_id = await db.create_session()
    manager = await BindingManager.load(db, session_id, tracking)
    show = ShowContent.model_validate(SHOW)
    engine = GameEngine(db, session_id, show, manager, tracking)

    _seed(tracking, 1, "a")
    _seed(tracking, 2, "a")
    _seed(tracking, 3, "b")
    await manager.claim("p1", 1)
    await manager.claim("p2", 2)
    await manager.claim("p3", 3)

    await engine.start_next_round()
    await engine.close_round()  # crash happens right here

    manager.shutdown()
    engine.shutdown()
    await db.close()  # simulate the process dying

    # --- "restart": fresh everything, same PocketBase instance and session ---
    db2 = PocketBaseClient(fake_pocketbase.url, "test@example.com", "pw")
    await db2.connect()
    tracking2 = TrackingClient("ws://unused")
    _seed(tracking2, 1, "a")
    _seed(tracking2, 2, "a")
    _seed(tracking2, 3, "b")

    recovered_session_id = await db2.get_active_session_id()
    assert recovered_session_id == session_id

    manager2 = await BindingManager.load(db2, recovered_session_id, tracking2)
    engine2 = await GameEngine.load(db2, recovered_session_id, show, manager2, tracking2)

    try:
        # Bindings: unchanged.
        assert manager2.get("p1").gid == 1
        assert manager2.get("p2").gid == 2
        assert manager2.get("p3").gid == 3

        # Round: recovered mid-flight, answers preserved, nothing repeated.
        assert engine2.current is not None
        assert engine2.current.state == RoundState.CLOSING
        assert engine2.current.content.id == "r1"
        assert engine2.current.answers["p1"] == ("a", "answered")
        assert engine2.current.answers["p2"] == ("a", "answered")
        assert engine2.current.answers["p3"] == ("b", "answered")

        final = await engine2.reveal_round()
        assert final.state == RoundState.DONE
        scores = await engine2.scores()
        assert scores.get("p1") == 10
        assert scores.get("p2") == 10

        next_round = await engine2.start_next_round()
        assert next_round.content.id == "r2"  # r1 is not replayed
    finally:
        manager2.shutdown()
        engine2.shutdown()
        await db2.close()
