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
