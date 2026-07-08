"""Binding state machine tests. The TrackingClient is seeded directly (no
network) since its wire parsing is already covered by test_tracking_client.py
— these tests are about claim/rebind/loss semantics.
"""

from __future__ import annotations

import asyncio

import pytest

from server.bindings import BindingError, BindingManager, PlayerState
from server.models import AudienceSummary
from server.tracking_client import ChangeEvent, ResyncEvent, TrackingClient


def _seed(tracking: TrackingClient, gid: int, *, visible: bool = True, floor=(0.1, 0.2)) -> None:
    tracking._state[gid] = AudienceSummary(
        gid=gid, visible=visible, floor=floor, floor_valid=floor is not None, zone=None
    )


@pytest.fixture
async def db(pb):
    """The connected PocketBaseClient (against the in-process fake) — same
    role the ':memory:' Database fixture used to play."""
    return pb


@pytest.fixture
def tracking():
    return TrackingClient("ws://unused")


@pytest.fixture
async def manager(db, tracking):
    session_id = await db.create_session()
    return await BindingManager.load(db, session_id, tracking)


@pytest.fixture
async def churn_manager(db, tracking):
    """A manager tuned for fast, deterministic churn tests: short orphan
    delay, a small rebind radius/window, and a ritual zone enabled."""
    session_id = await db.create_session()
    return await BindingManager.load(
        db,
        session_id,
        tracking,
        rebind_max_distance=0.1,
        rebind_max_gap_s=5.0,
        orphan_after_s=0.05,
        ritual_zone_id="ritual",
    )


@pytest.mark.asyncio
async def test_claim_success(manager, tracking, db):
    _seed(tracking, 101)
    player = await manager.claim("p1", 101, display_name="Alex")
    assert player.state == PlayerState.BOUND
    assert player.gid == 101
    assert manager.player_for_gid(101).id == "p1"

    rows = await db.load_players(manager.session_id)
    assert len(rows) == 1
    assert rows[0].gid == 101
    events = await db.load_binding_events(manager.session_id)
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


# ---------------------------------------------------------------------- #
# Phase 4: auto-rebind, orphan timer, ritual rebind
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_auto_rebind_single_confident_candidate(churn_manager, tracking):
    _seed(tracking, 101, floor=(0.5, 0.5))
    await churn_manager.claim("p1", 101)

    await churn_manager.handle_tracking_event(ChangeEvent(gid=101, state=None))
    assert churn_manager.get("p1").state == PlayerState.LOST

    # A new gid appears right where p1 was last seen, moments later.
    _seed(tracking, 202, floor=(0.51, 0.51))
    await churn_manager.handle_tracking_event(
        ChangeEvent(gid=202, state=tracking.get(202))
    )

    player = churn_manager.get("p1")
    assert player.state == PlayerState.BOUND
    assert player.gid == 202


@pytest.mark.asyncio
async def test_auto_rebind_records_binding_event(churn_manager, tracking, db):
    _seed(tracking, 101, floor=(0.5, 0.5))
    await churn_manager.claim("p1", 101)
    await churn_manager.handle_tracking_event(ChangeEvent(gid=101, state=None))

    _seed(tracking, 202, floor=(0.51, 0.51))
    await churn_manager.handle_tracking_event(ChangeEvent(gid=202, state=tracking.get(202)))

    events = await db.load_binding_events(churn_manager.session_id)
    assert events[-1].reason == "auto_rebind"
    assert events[-1].new_gid == 202
    assert events[-1].player_id == "p1"


@pytest.mark.asyncio
async def test_auto_rebind_refuses_ambiguous_candidates(churn_manager, tracking):
    """Two lost players near where one new gid appears: never guess."""
    _seed(tracking, 101, floor=(0.5, 0.5))
    _seed(tracking, 102, floor=(0.52, 0.52))
    await churn_manager.claim("p1", 101)
    await churn_manager.claim("p2", 102)
    await churn_manager.handle_tracking_event(ChangeEvent(gid=101, state=None))
    await churn_manager.handle_tracking_event(ChangeEvent(gid=102, state=None))

    _seed(tracking, 303, floor=(0.51, 0.51))  # equidistant-ish from both
    await churn_manager.handle_tracking_event(ChangeEvent(gid=303, state=tracking.get(303)))

    # Neither player should have been guessed onto gid 303.
    assert churn_manager.get("p1").state == PlayerState.LOST
    assert churn_manager.get("p2").state == PlayerState.LOST
    assert churn_manager.player_for_gid(303) is None


@pytest.mark.asyncio
async def test_auto_rebind_ignores_distant_candidate(churn_manager, tracking):
    _seed(tracking, 101, floor=(0.1, 0.1))
    await churn_manager.claim("p1", 101)
    await churn_manager.handle_tracking_event(ChangeEvent(gid=101, state=None))

    _seed(tracking, 202, floor=(0.9, 0.9))  # far away
    await churn_manager.handle_tracking_event(ChangeEvent(gid=202, state=tracking.get(202)))

    assert churn_manager.get("p1").state == PlayerState.LOST
    assert churn_manager.player_for_gid(202) is None


@pytest.mark.asyncio
async def test_auto_rebind_ignores_stale_lost_player(churn_manager, tracking):
    _seed(tracking, 101, floor=(0.5, 0.5))
    await churn_manager.claim("p1", 101)
    await churn_manager.handle_tracking_event(ChangeEvent(gid=101, state=None))
    churn_manager.get("p1").last_seen_at -= 999  # simulate a long time lost

    _seed(tracking, 202, floor=(0.5, 0.5))
    await churn_manager.handle_tracking_event(ChangeEvent(gid=202, state=tracking.get(202)))

    assert churn_manager.get("p1").state == PlayerState.LOST
    assert churn_manager.player_for_gid(202) is None


@pytest.mark.asyncio
async def test_resync_never_steals_a_still_bound_gid(churn_manager, tracking):
    """A heartbeat resync resends every visible gid, including ones already
    bound — this must never be mistaken for a rebind candidate, even if a
    just-lost player happens to be near it."""
    _seed(tracking, 101, floor=(0.5, 0.5))
    _seed(tracking, 102, floor=(0.51, 0.51))
    await churn_manager.claim("p1", 101)
    await churn_manager.claim("p2", 102)

    # p1's gid vanishes; p2's gid (very close by) is still present.
    await churn_manager.handle_tracking_event(ResyncEvent(gids={102}))
    assert churn_manager.get("p1").state == PlayerState.LOST
    assert churn_manager.get("p2").state == PlayerState.BOUND
    assert churn_manager.player_for_gid(102).id == "p2"


@pytest.mark.asyncio
async def test_lost_player_becomes_orphaned_after_timeout(churn_manager, tracking):
    _seed(tracking, 101)
    await churn_manager.claim("p1", 101)
    await churn_manager.handle_tracking_event(ChangeEvent(gid=101, state=None))
    assert churn_manager.get("p1").state == PlayerState.LOST

    await asyncio.sleep(0.15)  # churn_manager.orphan_after_s == 0.05
    assert churn_manager.get("p1").state == PlayerState.ORPHANED


@pytest.mark.asyncio
async def test_rebinding_before_timeout_cancels_orphan(churn_manager, tracking):
    _seed(tracking, 101, floor=(0.5, 0.5))
    await churn_manager.claim("p1", 101)
    await churn_manager.handle_tracking_event(ChangeEvent(gid=101, state=None))

    _seed(tracking, 202, floor=(0.51, 0.51))
    await churn_manager.handle_tracking_event(ChangeEvent(gid=202, state=tracking.get(202)))
    assert churn_manager.get("p1").state == PlayerState.BOUND

    await asyncio.sleep(0.15)  # would have orphaned p1 if the timer weren't cancelled
    assert churn_manager.get("p1").state == PlayerState.BOUND


@pytest.mark.asyncio
async def test_ritual_rebind_resolves_orphaned_player(churn_manager, tracking):
    _seed(tracking, 101, floor=(0.1, 0.1))
    await churn_manager.claim("p1", 101)
    await churn_manager.handle_tracking_event(ChangeEvent(gid=101, state=None))
    await asyncio.sleep(0.15)
    assert churn_manager.get("p1").state == PlayerState.ORPHANED

    # An unbound gid walks into the ritual corner, far from p1's last seen
    # position — auto-rebind by proximity would never have matched this.
    _seed(tracking, 909, floor=(0.9, 0.9))
    tracking._state[909].zone = "ritual"
    await churn_manager.handle_tracking_event(ChangeEvent(gid=909, state=tracking.get(909)))

    player = churn_manager.get("p1")
    assert player.state == PlayerState.BOUND
    assert player.gid == 909


@pytest.mark.asyncio
async def test_ritual_rebind_ignores_zone_without_orphaned_players(churn_manager, tracking):
    _seed(tracking, 909, floor=(0.9, 0.9))
    tracking._state[909].zone = "ritual"
    await churn_manager.handle_tracking_event(ChangeEvent(gid=909, state=tracking.get(909)))
    assert churn_manager.player_for_gid(909) is None


@pytest.mark.asyncio
async def test_ritual_rebind_resolves_oldest_orphan_first(churn_manager, tracking):
    _seed(tracking, 101)
    _seed(tracking, 102)
    await churn_manager.claim("p1", 101)
    await churn_manager.claim("p2", 102)
    await churn_manager.handle_tracking_event(ChangeEvent(gid=101, state=None))
    await asyncio.sleep(0.08)
    await churn_manager.handle_tracking_event(ChangeEvent(gid=102, state=None))
    await asyncio.sleep(0.08)
    assert churn_manager.get("p1").state == PlayerState.ORPHANED
    assert churn_manager.get("p2").state == PlayerState.ORPHANED

    _seed(tracking, 909, floor=(0.9, 0.9))
    tracking._state[909].zone = "ritual"
    await churn_manager.handle_tracking_event(ChangeEvent(gid=909, state=tracking.get(909)))

    # p1 has been orphaned longer, so the ritual resolves them first.
    assert churn_manager.get("p1").state == PlayerState.BOUND
    assert churn_manager.get("p2").state == PlayerState.ORPHANED


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_orphan_tasks(churn_manager, tracking):
    _seed(tracking, 101)
    await churn_manager.claim("p1", 101)
    await churn_manager.handle_tracking_event(ChangeEvent(gid=101, state=None))

    churn_manager.shutdown()
    await asyncio.sleep(0.15)
    # The orphan task was cancelled, so the state should still be LOST.
    assert churn_manager.get("p1").state == PlayerState.LOST
