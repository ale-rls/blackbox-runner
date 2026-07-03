"""Binding state machine tests. The TrackingClient is seeded directly (no
network) since its wire parsing is already covered by test_tracking_client.py
— these tests are about claim/rebind/loss semantics.
"""

from __future__ import annotations

import pytest

from server.bindings import BindingError, BindingManager, PlayerState
from server.models import AudienceSummary
from server.persistence import Database
from server.tracking_client import ChangeEvent, ResyncEvent, TrackingClient


def _seed(tracking: TrackingClient, gid: int, *, visible: bool = True, floor=(0.1, 0.2)) -> None:
    tracking._state[gid] = AudienceSummary(
        gid=gid, visible=visible, floor=floor, floor_valid=floor is not None, zone=None
    )


@pytest.fixture
def db():
    database = Database(":memory:")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def tracking():
    return TrackingClient("ws://unused")


@pytest.fixture
async def manager(db, tracking):
    session_id = db.create_session()
    return await BindingManager.load(db, session_id, tracking)


@pytest.mark.asyncio
async def test_claim_success(manager, tracking, db):
    _seed(tracking, 101)
    player = await manager.claim("p1", 101, display_name="Alex")
    assert player.state == PlayerState.BOUND
    assert player.gid == 101
    assert manager.player_for_gid(101).id == "p1"

    rows = db.load_players(manager.session_id)
    assert len(rows) == 1
    assert rows[0].gid == 101
    events = db.load_binding_events(manager.session_id)
    assert events[-1].reason == "claim"
    assert events[-1].new_gid == 101


@pytest.mark.asyncio
async def test_claim_rejects_inactive_gid(manager):
    with pytest.raises(BindingError):
        await manager.claim("p1", 999)


@pytest.mark.asyncio
async def test_claim_rejects_gid_already_bound_to_other_player(manager, tracking):
    _seed(tracking, 101)
    await manager.claim("p1", 101)
    with pytest.raises(BindingError):
        await manager.claim("p2", 101)


@pytest.mark.asyncio
async def test_claim_rejects_double_claim_while_already_bound(manager, tracking):
    _seed(tracking, 101)
    _seed(tracking, 102)
    await manager.claim("p1", 101)
    with pytest.raises(BindingError):
        await manager.claim("p1", 102)


@pytest.mark.asyncio
async def test_gid_disappearing_marks_player_lost(manager, tracking):
    _seed(tracking, 101)
    await manager.claim("p1", 101)

    await manager.handle_tracking_event(ChangeEvent(gid=101, state=None))

    player = manager.get("p1")
    assert player.state == PlayerState.LOST
    assert player.gid is None
    assert manager.player_for_gid(101) is None


@pytest.mark.asyncio
async def test_reclaim_allowed_after_loss(manager, tracking):
    _seed(tracking, 101)
    await manager.claim("p1", 101)
    await manager.handle_tracking_event(ChangeEvent(gid=101, state=None))

    _seed(tracking, 202)
    player = await manager.claim("p1", 202)
    assert player.state == PlayerState.BOUND
    assert player.gid == 202


@pytest.mark.asyncio
async def test_resync_marks_missing_bound_players_lost(manager, tracking):
    _seed(tracking, 101)
    _seed(tracking, 102)
    await manager.claim("p1", 101)
    await manager.claim("p2", 102)

    # TrackingBox restarted: fresh snapshot only has gid 102 (101 is gone).
    await manager.handle_tracking_event(ResyncEvent(gids={102}))

    assert manager.get("p1").state == PlayerState.LOST
    assert manager.get("p2").state == PlayerState.BOUND


@pytest.mark.asyncio
async def test_operator_rebind_steals_gid_from_other_player(manager, tracking):
    _seed(tracking, 101)
    await manager.claim("p1", 101)

    player = await manager.operator_rebind("p2", 101)
    assert player.gid == 101
    assert player.state == PlayerState.BOUND
    assert manager.get("p1").state == PlayerState.LOST
    assert manager.player_for_gid(101).id == "p2"


@pytest.mark.asyncio
async def test_operator_rebind_rejects_inactive_gid(manager):
    with pytest.raises(BindingError):
        await manager.operator_rebind("p1", 999)


@pytest.mark.asyncio
async def test_operator_rebind_onboards_unknown_player(manager, tracking):
    """An usher can bind a never-claimed player id directly from the
    dashboard (e.g. a phone that failed during onboarding)."""
    _seed(tracking, 101)
    player = await manager.operator_rebind("p1", 101, actor="usher-1")
    assert player.state == PlayerState.BOUND
    assert player.gid == 101
    assert manager.get("p1") is player


@pytest.mark.asyncio
async def test_forty_players_claimable(manager, tracking):
    for gid in range(1, 41):
        _seed(tracking, gid)

    for i, gid in enumerate(range(1, 41)):
        player = await manager.claim(f"p{i}", gid)
        assert player.state == PlayerState.BOUND

    bound = [p for p in manager.all_players() if p.state == PlayerState.BOUND]
    assert len(bound) == 40
