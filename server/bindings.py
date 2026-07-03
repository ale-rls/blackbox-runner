"""Player <-> GID binding state machine (docs/architecture.md §4.2).

Phase 1 scope: claim, automatic loss detection (a bound GID disappearing
from TrackingBox, including a full TrackingBox restart), and operator
(manual) rebind. Auto-rebind scoring and the ritual rebind flow for
ambiguous cases are Phase 4.

State machine: unclaimed -> bound -> lost -> bound (via claim or operator
rebind). ``orphaned``/``left`` are reserved for Phase 4/5 flows.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass
from typing import Optional

from .persistence import Database, PlayerRow
from .tracking_client import ChangeEvent, ResyncEvent, TrackingClient, TrackingEvent

log = logging.getLogger("theater_game.bindings")


class PlayerState(str, enum.Enum):
    UNCLAIMED = "unclaimed"
    BOUND = "bound"
    LOST = "lost"
    ORPHANED = "orphaned"
    LEFT = "left"


class BindingReason(str, enum.Enum):
    CLAIM = "claim"
    AUTO_REBIND = "auto_rebind"
    RITUAL = "ritual"
    OPERATOR = "operator"
    GC = "gc"
    LOST = "lost"


class BindingError(ValueError):
    """Invalid claim/rebind attempt (inactive GID, already bound, etc.)."""


@dataclass(slots=True)
class Player:
    id: str
    session_id: int
    gid: Optional[int] = None
    display_name: Optional[str] = None
    state: PlayerState = PlayerState.UNCLAIMED
    last_seen_x: Optional[float] = None
    last_seen_y: Optional[float] = None
    last_seen_at: Optional[float] = None

    def to_row(self) -> PlayerRow:
        return PlayerRow(
            id=self.id,
            session_id=self.session_id,
            gid=self.gid,
            display_name=self.display_name,
            state=self.state.value,
            last_seen_x=self.last_seen_x,
            last_seen_y=self.last_seen_y,
            last_seen_at=self.last_seen_at,
        )

    @classmethod
    def from_row(cls, row: PlayerRow) -> "Player":
        return cls(
            id=row.id,
            session_id=row.session_id,
            gid=row.gid,
            display_name=row.display_name,
            state=PlayerState(row.state),
            last_seen_x=row.last_seen_x,
            last_seen_y=row.last_seen_y,
            last_seen_at=row.last_seen_at,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "gid": self.gid,
            "display_name": self.display_name,
            "state": self.state.value,
            "last_seen_x": self.last_seen_x,
            "last_seen_y": self.last_seen_y,
            "last_seen_at": self.last_seen_at,
        }


class BindingManager:
    """In-memory source of truth for player<->GID bindings.

    Every meaningful transition is written through to SQLite immediately
    (see :meth:`_save`), so :meth:`load` can rebuild identical state after a
    crash. Subscribes to a :class:`TrackingClient` to detect when a bound
    GID disappears.
    """

    def __init__(self, db: Database, session_id: int, tracking: TrackingClient) -> None:
        self._db = db
        self.session_id = session_id
        self._tracking = tracking
        self._players: dict[str, Player] = {}
        self._by_gid: dict[int, str] = {}  # gid -> player_id, bound players only
        self._listeners: list[asyncio.Queue] = []

    @classmethod
    async def load(cls, db: Database, session_id: int, tracking: TrackingClient) -> "BindingManager":
        mgr = cls(db, session_id, tracking)
        rows = await asyncio.to_thread(db.load_players, session_id)
        for row in rows:
            player = Player.from_row(row)
            mgr._players[player.id] = player
            if player.state == PlayerState.BOUND and player.gid is not None:
                mgr._by_gid[player.gid] = player.id
        log.info(
            "Loaded %d player(s) from session %d (%d currently bound)",
            len(mgr._players),
            session_id,
            len(mgr._by_gid),
        )
        return mgr

    # ------------------------------------------------------------------ #
    # Read side
    # ------------------------------------------------------------------ #
    def get(self, player_id: str) -> Optional[Player]:
        return self._players.get(player_id)

    def all_players(self) -> list[Player]:
        return list(self._players.values())

    def player_for_gid(self, gid: int) -> Optional[Player]:
        pid = self._by_gid.get(gid)
        return self._players.get(pid) if pid else None

    # ------------------------------------------------------------------ #
    # Subscription (admin board / TD)
    # ------------------------------------------------------------------ #
    def subscribe(self) -> "asyncio.Queue[Player]":
        q: "asyncio.Queue[Player]" = asyncio.Queue(maxsize=256)
        self._listeners.append(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[Player]") -> None:
        if q in self._listeners:
            self._listeners.remove(q)

    def _publish(self, player: Player) -> None:
        for q in list(self._listeners):
            try:
                q.put_nowait(player)
            except asyncio.QueueFull:
                log.warning("Dropping binding-board event: subscriber queue full")

    # ------------------------------------------------------------------ #
    # Claim / rebind
    # ------------------------------------------------------------------ #
    async def claim(self, player_id: str, gid: int, display_name: Optional[str] = None) -> Player:
        """Self-service claim from the player's phone. Allowed from
        unclaimed/lost/orphaned states; refused if already bound (prevents
        an accidental double-submit from hijacking a live binding).
        """
        existing = self._players.get(player_id)
        if existing is not None and existing.state == PlayerState.BOUND:
            raise BindingError(f"player {player_id!r} is already bound to gid {existing.gid}")

        self._check_gid_claimable(gid, player_id)

        old_gid = existing.gid if existing else None
        player = existing or Player(id=player_id, session_id=self.session_id)
        player.display_name = display_name or player.display_name
        await self._bind(player, gid, old_gid=old_gid, reason=BindingReason.CLAIM)
        return player

    async def operator_rebind(self, player_id: str, gid: int, actor: str = "operator") -> Player:
        """Manual rebind from the admin dashboard: works for a brand-new
        player id (an usher onboarding someone whose phone failed) or an
        existing one in any state, including forcibly moving a GID away
        from another bound player.
        """
        player = self._players.get(player_id) or Player(id=player_id, session_id=self.session_id)

        self._check_gid_claimable(gid, player_id, allow_steal=True)

        old_gid = player.gid
        # If we're stealing the gid from someone else, that player goes lost.
        other_id = self._by_gid.get(gid)
        if other_id is not None and other_id != player_id:
            other = self._players[other_id]
            await self._mark_lost(other)

        await self._bind(player, gid, old_gid=old_gid, reason=BindingReason.OPERATOR, actor=actor)
        return player

    def _check_gid_claimable(self, gid: int, player_id: str, *, allow_steal: bool = False) -> None:
        current = self._tracking.get(gid)
        if current is None or not current.visible:
            raise BindingError(f"gid {gid} is not currently active")
        holder_id = self._by_gid.get(gid)
        if holder_id is not None and holder_id != player_id and not allow_steal:
            raise BindingError(f"gid {gid} is already claimed by player {holder_id!r}")

    async def _bind(
        self,
        player: Player,
        gid: int,
        *,
        old_gid: Optional[int],
        reason: BindingReason,
        actor: Optional[str] = None,
    ) -> None:
        current = self._tracking.get(gid)
        player.gid = gid
        player.state = PlayerState.BOUND
        player.last_seen_x, player.last_seen_y = (current.floor if current else None) or (None, None)
        player.last_seen_at = time.time()
        await self._save(player, old_gid=old_gid, new_gid=gid, reason=reason, actor=actor)

    async def _mark_lost(self, player: Player) -> None:
        if player.state != PlayerState.BOUND:
            return
        old_gid = player.gid
        player.gid = None
        player.state = PlayerState.LOST
        await self._save(player, old_gid=old_gid, new_gid=None, reason=BindingReason.LOST)

    async def _save(
        self,
        player: Player,
        *,
        old_gid: Optional[int],
        new_gid: Optional[int],
        reason: BindingReason,
        actor: Optional[str] = None,
    ) -> None:
        self._players[player.id] = player
        if old_gid is not None:
            self._by_gid.pop(old_gid, None)
        if new_gid is not None and player.state == PlayerState.BOUND:
            self._by_gid[new_gid] = player.id
        await asyncio.to_thread(self._db.upsert_player, player.to_row())
        await asyncio.to_thread(
            self._db.record_binding_event,
            self.session_id,
            player.id,
            old_gid,
            new_gid,
            reason.value,
            actor,
        )
        self._publish(player)

    # ------------------------------------------------------------------ #
    # Tracking event handling — automatic loss detection
    # ------------------------------------------------------------------ #
    async def handle_tracking_event(self, event: TrackingEvent) -> None:
        if isinstance(event, ResyncEvent):
            # A fresh snapshot is the new source of truth (also how a
            # TrackingBox restart is discovered: every GID resets, so every
            # bound player not in the new snapshot is truly gone).
            for player in list(self._players.values()):
                if player.state == PlayerState.BOUND and player.gid not in event.gids:
                    await self._mark_lost(player)
            return

        if isinstance(event, ChangeEvent):
            player = self.player_for_gid(event.gid)
            if player is None:
                return
            if event.state is None:
                await self._mark_lost(player)
            else:
                if event.state.floor is not None:
                    player.last_seen_x, player.last_seen_y = event.state.floor
                player.last_seen_at = time.time()
                self._players[player.id] = player
                await asyncio.to_thread(self._db.upsert_player, player.to_row())

    async def run(self) -> None:
        """Long-running task: consume tracking events and update bindings."""
        async for event in self._tracking.events():
            try:
                await self.handle_tracking_event(event)
            except Exception:
                log.exception("Error handling tracking event in BindingManager")
