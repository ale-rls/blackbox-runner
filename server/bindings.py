"""Player <-> GID binding state machine (docs/architecture.md §4.2).

State machine: unclaimed -> bound -> lost -> (bound | orphaned) -> bound.

* **Lost**: a bound GID disappears (individually, or via a TrackingBox
  restart's full resync). A per-player timer starts.
* **Auto-rebind**: whenever an unbound GID appears, it's matched against
  every lost/orphaned player by floor-position proximity and recency. If
  exactly one player is a plausible match, the rebind is silent. If more
  than one is plausible, we refuse to guess — see §4.2's "never guess
  between two candidates" — and leave everyone as they are.
  ``_plausible_rebind_candidates`` deliberately uses hard thresholds
  (distance + time-gap cutoffs) rather than a fully general weighted score
  matcher; retuning those thresholds from rehearsal telemetry is exactly
  the feedback loop the design doc describes for this section.
* **Orphaned**: a lost player who hasn't auto-rebound within
  ``orphan_after_s`` needs a ritual or operator rebind. Every unbound GID
  that steps into ``ritual_zone_id`` resolves the longest-waiting orphan.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass
from typing import Optional

from .pocketbase_client import PlayerRow, PocketBaseClient
from .tracking_client import ChangeEvent, ResyncEvent, TrackingClient, TrackingEvent

log = logging.getLogger("blackbox_runner.bindings")


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
    session_id: str
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

    Every meaningful transition is written through to PocketBase immediately
    (see :meth:`_save`), so :meth:`load` can rebuild identical state after a
    crash. Subscribes to a :class:`TrackingClient` to detect when a bound
    GID disappears.
    """

    def __init__(
        self,
        db: PocketBaseClient,
        session_id: str,
        tracking: TrackingClient,
        *,
        rebind_max_distance: float = 0.15,
        rebind_max_gap_s: float = 8.0,
        orphan_after_s: float = 3.0,
        ritual_zone_id: Optional[str] = None,
    ) -> None:
        self._db = db
        self.session_id = session_id
        self._tracking = tracking
        self._rebind_max_distance = rebind_max_distance
        self._rebind_max_gap_s = rebind_max_gap_s
        self._orphan_after_s = orphan_after_s
        self._ritual_zone_id = ritual_zone_id
        self._players: dict[str, Player] = {}
        self._by_gid: dict[int, str] = {}  # gid -> player_id, bound players only
        self._orphan_tasks: dict[str, asyncio.Task] = {}
        self._listeners: list[asyncio.Queue] = []

    @classmethod
    async def load(
        cls,
        db: PocketBaseClient,
        session_id: str,
        tracking: TrackingClient,
        **kwargs,
    ) -> "BindingManager":
        mgr = cls(db, session_id, tracking, **kwargs)
        rows = await db.load_players(session_id)
        for row in rows:
            player = Player.from_row(row)
            mgr._players[player.id] = player
            if player.state == PlayerState.BOUND and player.gid is not None:
                mgr._by_gid[player.gid] = player.id
        log.info(
            "Loaded %d player(s) from session %s (%d currently bound)",
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
        self._cancel_orphan_task(player.id)
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
        self._cancel_orphan_task(player.id)
        self._orphan_tasks[player.id] = asyncio.create_task(self._orphan_after_delay(player.id))

    def _cancel_orphan_task(self, player_id: str) -> None:
        task = self._orphan_tasks.pop(player_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _orphan_after_delay(self, player_id: str) -> None:
        try:
            await asyncio.sleep(self._orphan_after_s)
        except asyncio.CancelledError:
            return
        player = self._players.get(player_id)
        if player is None or player.state != PlayerState.LOST:
            return
        player.state = PlayerState.ORPHANED
        self._players[player.id] = player
        await self._db.upsert_player(player.to_row())
        self._publish(player)
        log.info(
            "Player %s orphaned after %.1fs with no confident auto-rebind",
            player_id,
            self._orphan_after_s,
        )

    # ------------------------------------------------------------------ #
    # Auto-rebind: a fresh GID appears near where someone was lost
    # ------------------------------------------------------------------ #
    def _plausible_rebind_candidates(self, gid: int) -> list[Player]:
        state = self._tracking.get(gid)
        if state is None or not state.floor_valid or state.floor is None:
            return []
        now = time.time()
        candidates = []
        for player in self._players.values():
            if player.state not in (PlayerState.LOST, PlayerState.ORPHANED):
                continue
            if player.last_seen_x is None or player.last_seen_y is None or player.last_seen_at is None:
                continue
            if now - player.last_seen_at > self._rebind_max_gap_s:
                continue
            dx = player.last_seen_x - state.floor[0]
            dy = player.last_seen_y - state.floor[1]
            if (dx * dx + dy * dy) ** 0.5 > self._rebind_max_distance:
                continue
            candidates.append(player)
        return candidates

    async def _on_gid_appeared(self, gid: int) -> None:
        if gid in self._by_gid:
            # Already bound — e.g. a heartbeat resync resent every visible
            # gid, not just genuinely new ones. Never treat an owned gid as
            # a rebind candidate.
            return
        candidates = self._plausible_rebind_candidates(gid)
        if len(candidates) == 1:
            player = candidates[0]
            await self._bind(player, gid, old_gid=None, reason=BindingReason.AUTO_REBIND)
            log.info("Auto-rebound player %s to gid %d", player.id, gid)
        elif len(candidates) > 1:
            log.info(
                "gid %d is ambiguous between %d lost/orphaned players; not guessing",
                gid,
                len(candidates),
            )

    async def _maybe_resolve_ritual(self, gid: int) -> None:
        if self._ritual_zone_id is None or gid in self._by_gid:
            return
        state = self._tracking.get(gid)
        if state is None or state.zone != self._ritual_zone_id:
            return
        orphaned = sorted(
            (p for p in self._players.values() if p.state == PlayerState.ORPHANED),
            key=lambda p: p.last_seen_at or 0.0,
        )
        if not orphaned:
            return
        player = orphaned[0]
        await self._bind(player, gid, old_gid=None, reason=BindingReason.RITUAL)
        log.info("Ritual-rebound player %s to gid %d", player.id, gid)

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
        await self._db.upsert_player(player.to_row())
        await self._db.record_binding_event(
            self.session_id,
            player.id,
            old_gid,
            new_gid,
            reason.value,
            actor,
        )
        self._publish(player)

    # ------------------------------------------------------------------ #
    # Tracking event handling — loss detection, auto-rebind, ritual
    # ------------------------------------------------------------------ #
    async def handle_tracking_event(self, event: TrackingEvent) -> None:
        if isinstance(event, ResyncEvent):
            # A fresh snapshot is the new source of truth (also how a
            # TrackingBox restart is discovered: every GID resets, so every
            # bound player not in the new snapshot is truly gone).
            for player in list(self._players.values()):
                if player.state == PlayerState.BOUND and player.gid not in event.gids:
                    await self._mark_lost(player)
            for gid in event.gids:
                if gid not in self._by_gid:
                    await self._on_gid_appeared(gid)
                    await self._maybe_resolve_ritual(gid)
            return

        if isinstance(event, ChangeEvent):
            if event.state is None:
                player = self.player_for_gid(event.gid)
                if player is not None:
                    await self._mark_lost(player)
                return

            player = self.player_for_gid(event.gid)
            if player is not None:
                if event.state.floor is not None:
                    player.last_seen_x, player.last_seen_y = event.state.floor
                player.last_seen_at = time.time()
                self._players[player.id] = player
                await self._db.upsert_player(player.to_row())
                return

            # An unbound GID: try auto-rebind (and the ritual zone) on every
            # sighting, not just the first. TrackingBox reuses GID numbers
            # when ReID recovers someone after a brief occlusion — the same
            # gid can go visible -> invisible -> visible again, and that
            # *return* is exactly the case auto-rebind exists for, so it
            # can't be gated on "have we ever seen this gid before."
            await self._on_gid_appeared(event.gid)
            await self._maybe_resolve_ritual(event.gid)

    async def run(self) -> None:
        """Long-running task: consume tracking events and update bindings."""
        async for event in self._tracking.events():
            try:
                await self.handle_tracking_event(event)
            except Exception:
                log.exception("Error handling tracking event in BindingManager")

    def shutdown(self) -> None:
        for task in self._orphan_tasks.values():
            if not task.done():
                task.cancel()
        self._orphan_tasks.clear()
