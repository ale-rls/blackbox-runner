"""Scripted churn scenario: TrackingBox restarts mid-round (full resync).

Every GID resets. The game server must not treat this as a network blip to
patch around — the same resync path recovers it, and bound players who
vanish from the fresh snapshot are recorded absent, never wrong, when the
round closes (docs/architecture.md §4.1, §4.3, §7).
"""

from __future__ import annotations

import pytest

from server.bindings import BindingManager, PlayerState
from server.content import ShowContent
from server.engine import GameEngine, RoundState
from server.models import AudienceSummary
from server.persistence import Database
from server.tracking_client import ResyncEvent, TrackingClient

SHOW = {
    "version": "1",
    "rounds": [
        {
            "id": "r1",
            "question": "Left or right?",
            "type": "majority",
            "duration_s": 100,
            "grace_s": 100,
            "points": 10,
            "options": [{"zone": "a", "label": "Left"}, {"zone": "b", "label": "Right"}],
        }
    ],
}


def _seed(tracking: TrackingClient, gid: int, zone: str) -> None:
    tracking._state[gid] = AudienceSummary(
        gid=gid, visible=True, floor=(0.1, 0.1), floor_valid=True, zone=zone
    )


@pytest.mark.asyncio
async def test_trackingbox_restart_mid_round_marks_players_absent_not_wrong():
    db = Database(":memory:")
    tracking = TrackingClient("ws://unused")
    session_id = db.create_session()
    manager = await BindingManager.load(db, session_id, tracking)
    show = ShowContent.model_validate(SHOW)
    engine = GameEngine(db, session_id, show, manager, tracking)

    try:
        _seed(tracking, 1, "a")
        _seed(tracking, 2, "b")
        await manager.claim("p1", 1)
        await manager.claim("p2", 2)

        await engine.start_next_round()

        # TrackingBox restarts: brand-new GID numbering, nobody from before
        # exists in the fresh snapshot.
        await manager.handle_tracking_event(ResyncEvent(gids=set()))
        assert manager.get("p1").state == PlayerState.LOST
        assert manager.get("p2").state == PlayerState.LOST

        rt = await engine.close_round()
        assert rt.answers["p1"] == (None, "absent")
        assert rt.answers["p2"] == (None, "absent")

        # Reconnect: new people (new GIDs) come back into frame.
        _seed(tracking, 11, "a")
        await manager.handle_tracking_event(ResyncEvent(gids={11}))

        final = await engine.reveal_round()
        assert final.state == RoundState.DONE
        assert final.tally == {"a": 0, "b": 0}
        assert final.winning_zones == []

        events = db.load_binding_events(session_id)
        assert [e.reason for e in events if e.player_id == "p1"] == ["claim", "lost"]

        answer_rows = db.load_answers(rt.row_id)
        assert {(r.player_id, r.resolved) for r in answer_rows} == {
            ("p1", "absent"),
            ("p2", "absent"),
        }
    finally:
        engine.shutdown()
        manager.shutdown()
        db.close()
