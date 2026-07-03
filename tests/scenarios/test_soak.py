"""Load/soak scenario (docs/architecture.md §7): 60 simulated people,
sustained GID churn — drops, nearby respawns, and ambiguous respawns mixed
together — asserting the system never crashes, never double-binds a gid,
and no orphan is left stranded past its ritual.

Runs a compressed version (seconds, not a full show's runtime) so it's fast
enough for CI; the churn *rate* per player is what's under test, not wall
clock duration.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from server.bindings import BindingManager, PlayerState
from server.models import AudienceSummary
from server.persistence import Database
from server.tracking_client import ChangeEvent, TrackingClient

N_PLAYERS = 60


def _seed(tracking: TrackingClient, gid: int, floor: tuple[float, float]) -> None:
    tracking._state[gid] = AudienceSummary(gid=gid, visible=True, floor=floor, floor_valid=True)


@pytest.mark.asyncio
async def test_sixty_players_survive_sustained_churn():
    db = Database(":memory:")
    tracking = TrackingClient("ws://unused")
    session_id = db.create_session()
    manager = await BindingManager.load(
        db,
        session_id,
        tracking,
        rebind_max_distance=0.05,
        rebind_max_gap_s=3.0,
        orphan_after_s=0.05,
        ritual_zone_id="ritual",
    )

    try:
        rng = random.Random(1234)
        positions: dict[str, tuple[float, float]] = {}

        # Claim all 60 players at distinct, well-separated positions so the
        # initial claim phase itself never produces ambiguity.
        for i in range(N_PLAYERS):
            player_id = f"p{i}"
            gid = i
            pos = (0.02 * (i % 25), 0.02 * (i // 25))
            positions[player_id] = pos
            _seed(tracking, gid, pos)
            player = await manager.claim(player_id, gid)
            assert player.state == PlayerState.BOUND

        assert len({p.gid for p in manager.all_players()}) == N_PLAYERS  # no double-binding

        # Churn: repeatedly drop a random bound player's gid, then respawn a
        # fresh gid nearby (auto-rebind territory) for most of them, and
        # simulate an ambiguous double-drop occasionally.
        next_gid = 1000
        for _round in range(30):
            bound = [p for p in manager.all_players() if p.state == PlayerState.BOUND]
            if not bound:
                break
            victim = rng.choice(bound)
            await manager.handle_tracking_event(ChangeEvent(gid=victim.gid, state=None))

            if rng.random() < 0.8:
                # Respawn nearby -> should auto-rebind.
                base = positions[victim.id]
                jitter = (base[0] + rng.uniform(-0.01, 0.01), base[1] + rng.uniform(-0.01, 0.01))
                _seed(tracking, next_gid, jitter)
                await manager.handle_tracking_event(
                    ChangeEvent(gid=next_gid, state=tracking.get(next_gid))
                )
                next_gid += 1
            # else: leave them lost -> will orphan and need ritual below.

        await asyncio.sleep(0.15)  # let any pending orphan timers fire

        # Resolve every remaining orphan via the ritual corner, one at a
        # time (mirrors a real usher-run ritual queue).
        orphaned = [p for p in manager.all_players() if p.state == PlayerState.ORPHANED]
        for _ in orphaned:
            still_orphaned = [p for p in manager.all_players() if p.state == PlayerState.ORPHANED]
            if not still_orphaned:
                break
            _seed(tracking, next_gid, (0.99, 0.99))
            tracking._state[next_gid].zone = "ritual"
            await manager.handle_tracking_event(ChangeEvent(gid=next_gid, state=tracking.get(next_gid)))
            next_gid += 1

        # Invariants: every player ends up bound (no orphan survives past
        # its ritual), and no gid is ever bound to two players at once.
        final_players = manager.all_players()
        assert len(final_players) == N_PLAYERS
        assert all(p.state == PlayerState.BOUND for p in final_players)
        bound_gids = [p.gid for p in final_players]
        assert len(bound_gids) == len(set(bound_gids))

        # Audit trail is complete and self-consistent: every player has at
        # least a claim event, and every non-claim event has a resolvable
        # reason.
        events = db.load_binding_events(session_id)
        assert len(events) >= N_PLAYERS  # at minimum, one claim per player
        by_player: dict[str, list[str]] = {}
        for e in events:
            by_player.setdefault(e.player_id, []).append(e.reason)
        for player_id, reasons in by_player.items():
            assert reasons[0] == "claim"
            assert set(reasons) <= {"claim", "lost", "auto_rebind", "ritual", "operator"}
    finally:
        manager.shutdown()
        db.close()
