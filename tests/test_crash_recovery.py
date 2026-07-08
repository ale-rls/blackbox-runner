"""Phase 1 exit criterion: kill/restart the game server mid-session with no
state loss. Simulated here by closing the PocketBaseClient and
BindingManager and reconstructing fresh ones against the same (fake)
PocketBase instance and session, the same way ``create_app``'s lifespan
does on startup — the instance outlives game-server processes exactly like
the SQLite file used to.
"""

from __future__ import annotations

import pytest

from server.bindings import BindingManager, PlayerState
from server.models import AudienceSummary
from server.pocketbase_client import PocketBaseClient
from server.tracking_client import TrackingClient


def _seed(tracking: TrackingClient, gid: int) -> None:
    tracking._state[gid] = AudienceSummary(gid=gid, visible=True, floor=(0.1, 0.1), floor_valid=True)


@pytest.mark.asyncio
async def test_bindings_survive_server_restart(fake_pocketbase):
    # --- "first run" of the game server ---
    db = PocketBaseClient(fake_pocketbase.url, "test@example.com", "pw")
    await db.connect()
    session_id = await db.create_session()
    tracking = TrackingClient("ws://unused")
    for gid in range(1, 6):
        _seed(tracking, gid)
    bindings = await BindingManager.load(db, session_id, tracking)

    for i in range(5):
        await bindings.claim(f"p{i}", i + 1)

    # p2 goes lost before the crash.
    from server.tracking_client import ChangeEvent

    await bindings.handle_tracking_event(ChangeEvent(gid=3, state=None))

    before = {p.id: (p.state, p.gid) for p in bindings.all_players()}
    assert before["p2"] == (PlayerState.LOST, None)
    assert before["p0"] == (PlayerState.BOUND, 1)

    await db.close()  # simulate the process dying

    # --- "restart" ---
    db2 = PocketBaseClient(fake_pocketbase.url, "test@example.com", "pw")
    await db2.connect()
    tracking2 = TrackingClient("ws://unused")
    for gid in range(1, 6):
        _seed(tracking2, gid)
    recovered_session_id = await db2.get_active_session_id()
    assert recovered_session_id == session_id

    bindings2 = await BindingManager.load(db2, recovered_session_id, tracking2)
    after = {p.id: (p.state, p.gid) for p in bindings2.all_players()}

    assert after == before
    # The reverse gid index must also be rebuilt correctly for bound players.
    assert bindings2.player_for_gid(1).id == "p0"
    assert bindings2.player_for_gid(3) is None  # p2's old gid, now unbound

    await db2.close()


@pytest.mark.asyncio
async def test_live_stats_singleton_survives_restart_and_dedupes(pb, fake_pocketbase):
    session_id = await pb.create_session()

    await pb.publish_zone_counts(session_id, "q1", {"a": 2, "b": 0})
    rows = await pb._list_all("live_stats")
    assert len(rows) == 1
    assert rows[0]["round_id"] == "q1"
    assert rows[0]["zone_counts"] == {"a": 2, "b": 0}
    first_write_at = rows[0]["updated_at"]

    # Identical counts: deduped, no write at all.
    await pb.publish_zone_counts(session_id, "q1", {"b": 0, "a": 2})
    rows = await pb._list_all("live_stats")
    assert rows[0]["updated_at"] == first_write_at

    # A "restarted" server (fresh client, cold cache) reuses the singleton
    # row instead of growing a second one.
    db2 = PocketBaseClient(fake_pocketbase.url, "test@example.com", "pw")
    await db2.connect()
    await db2.publish_zone_counts(session_id, "q2", {"a": 1})
    rows = await db2._list_all("live_stats")
    assert len(rows) == 1
    assert rows[0]["round_id"] == "q2"
    assert rows[0]["zone_counts"] == {"a": 1}
    await db2.close()


@pytest.mark.asyncio
async def test_live_stats_singleton_survives_restart_and_dedupes(pb, fake_pocketbase):
    session_id = await pb.create_session()

    await pb.publish_zone_counts(session_id, "q1", {"a": 2, "b": 0})
    rows = await pb._list_all("live_stats")
    assert len(rows) == 1
    assert rows[0]["round_id"] == "q1"
    assert rows[0]["zone_counts"] == {"a": 2, "b": 0}
    first_write_at = rows[0]["updated_at"]

    # Identical counts: deduped, no write at all.
    await pb.publish_zone_counts(session_id, "q1", {"b": 0, "a": 2})
    rows = await pb._list_all("live_stats")
    assert rows[0]["updated_at"] == first_write_at

    # A "restarted" server (fresh client, cold cache) reuses the singleton
    # row instead of growing a second one.
    db2 = PocketBaseClient(fake_pocketbase.url, "test@example.com", "pw")
    await db2.connect()
    await db2.publish_zone_counts(session_id, "q2", {"a": 1})
    rows = await db2._list_all("live_stats")
    assert len(rows) == 1
    assert rows[0]["round_id"] == "q2"
    assert rows[0]["zone_counts"] == {"a": 1}
    await db2.close()
