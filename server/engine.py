"""Round state machine, timing, zone evaluation, and scoring
(docs/architecture.md §4.3).

Zone evaluation reads TrackingBox's own live ``zone`` field for each bound
player's GID rather than re-implementing point-in-polygon matching: the
zone map fetched at startup is used only to validate content (server.content),
not to re-derive what TrackingBox already computes with identical
first-match semantics.

State machine per round: pending -> active -> closing -> revealed -> done.
At ``closing`` every bound player's current zone is captured as their
answer; players who are lost/orphaned are recorded ``absent``, never wrong.
During the grace window that follows, any player who reclaims a binding and
is standing in a valid answer zone is upgraded to ``late_grace``.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .bindings import BindingManager, PlayerState
from .content import RoundContent, ShowContent
from .persistence import AnswerRow, Database
from .tracking_client import TrackingClient

log = logging.getLogger("blackbox_runner.engine")


class RoundState(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    CLOSING = "closing"
    REVEALED = "revealed"
    DONE = "done"


class EngineError(ValueError):
    """Invalid round-control command (e.g. closing with nothing active)."""


@dataclass(slots=True)
class RoundRuntime:
    content: RoundContent
    row_id: int
    index: int
    state: RoundState = RoundState.PENDING
    opened_at: Optional[float] = None
    closed_at: Optional[float] = None
    answers: dict[str, tuple[Optional[str], str]] = field(default_factory=dict)
    tally: dict[str, int] = field(default_factory=dict)
    winning_zones: list[str] = field(default_factory=list)


@dataclass(slots=True)
class EngineEvent:
    type: str
    payload: dict


class GameEngine:
    def __init__(
        self,
        db: Database,
        session_id: int,
        show: ShowContent,
        bindings: BindingManager,
        tracking: TrackingClient,
        *,
        zone_count_interval_s: float = 1.0,
    ) -> None:
        self._db = db
        self.session_id = session_id
        self.show = show
        self._bindings = bindings
        self._tracking = tracking
        self._zone_count_interval_s = zone_count_interval_s
        self._index = -1
        self._current: Optional[RoundRuntime] = None
        self._listeners: list[asyncio.Queue] = []
        self._timer_task: Optional[asyncio.Task] = None
        self._zone_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ #
    # Subscription (player / TD / admin WS)
    # ------------------------------------------------------------------ #
    def subscribe(self) -> "asyncio.Queue[EngineEvent]":
        q: "asyncio.Queue[EngineEvent]" = asyncio.Queue(maxsize=256)
        self._listeners.append(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[EngineEvent]") -> None:
        if q in self._listeners:
            self._listeners.remove(q)

    def _publish(self, event: EngineEvent) -> None:
        for q in list(self._listeners):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("Dropping engine event: subscriber queue full")

    def publish_cue(self, cue_type: str, payload: Optional[dict] = None) -> None:
        """Fire an arbitrary named cue to every subscriber (TD, admin, rehearsal
        scripts). The round lifecycle events below are just the built-in cues —
        this is the same channel the plan calls the "round/cue WS".
        """
        self._publish(EngineEvent(cue_type, payload or {}))

    @classmethod
    async def load(
        cls,
        db: Database,
        session_id: int,
        show: ShowContent,
        bindings: BindingManager,
        tracking: TrackingClient,
        **kwargs,
    ) -> "GameEngine":
        """Rebuild engine state after a crash mid-show.

        Recovery contract: if the most recently created round for this
        session had already finished (``done``), we simply resume from the
        round *after* it — nothing is lost. If it hadn't finished, we can't
        safely resume its original timers (we don't know how long the
        server was down for), so we load it as ``closing`` with whatever
        answers were already persisted, and leave it for the operator to
        call reveal — no answers are lost, but the round doesn't silently
        replay either.
        """
        engine = cls(db, session_id, show, bindings, tracking, **kwargs)
        rows = await asyncio.to_thread(db.load_rounds, session_id)
        if not rows:
            return engine

        last = rows[-1]
        engine._index = last.idx
        if last.state == RoundState.DONE.value:
            return engine

        content = next((r for r in show.rounds if r.id == last.question_id), None)
        if content is None:
            log.warning(
                "Recovered round row %d references unknown content id %r; treating as done",
                last.id,
                last.question_id,
            )
            return engine

        rt = RoundRuntime(content=content, row_id=last.id, index=last.idx)
        rt.state = RoundState.CLOSING
        rt.opened_at = last.opened_at
        rt.closed_at = last.closed_at or time.time()
        answer_rows = await asyncio.to_thread(db.load_answers, last.id)
        for row in answer_rows:
            rt.answers[row.player_id] = (row.zone_id, row.resolved)
        engine._current = rt
        await asyncio.to_thread(
            db.update_round_state, last.id, RoundState.CLOSING.value, rt.opened_at, rt.closed_at
        )
        log.warning(
            "Recovered mid-round state for round %r (index %d) after restart; %d answer(s) "
            "preserved. An operator must call reveal to finish it.",
            content.id,
            last.idx,
            len(rt.answers),
        )
        return engine

    # ------------------------------------------------------------------ #
    # Read side
    # ------------------------------------------------------------------ #
    @property
    def current(self) -> Optional[RoundRuntime]:
        return self._current

    @property
    def has_more_rounds(self) -> bool:
        return self._index + 1 < len(self.show.rounds)

    async def scores(self) -> dict[str, int]:
        return await asyncio.to_thread(self._db.sum_scores, self.session_id)

    def player_answer(self, player_id: str) -> Optional[tuple[Optional[str], str]]:
        if self._current is None:
            return None
        return self._current.answers.get(player_id)

    def current_zone_counts(self) -> dict[str, int]:
        rt = self._current
        if rt is None:
            return {}
        counts = {opt.zone: 0 for opt in rt.content.options}
        for player in self._bindings.all_players():
            zone = self._current_zone(player)
            if zone in counts:
                counts[zone] += 1
        return counts

    def reload_show(self, show: ShowContent) -> None:
        """Hot-reload content (docs/architecture.md §3). Only safe between
        rounds — refused while one is in flight, since ``_index`` is a
        position into ``show.rounds`` and swapping the list out from under
        an active round could change what "the current round" even means.
        A full content freeze ahead of the show is still the right process
        for anything that touches rounds already played; see
        docs/runbook.md.
        """
        if self._current is not None and self._current.state != RoundState.DONE:
            raise EngineError("cannot reload content while a round is in progress")
        self.show = show
        log.info("Reloaded show content: %d round(s)", len(show.rounds))

    # ------------------------------------------------------------------ #
    # Round control
    # ------------------------------------------------------------------ #
    async def start_next_round(self) -> RoundRuntime:
        if self._current is not None and self._current.state != RoundState.DONE:
            raise EngineError("a round is already in progress")
        if not self.has_more_rounds:
            raise EngineError("no more rounds in this show")

        self._index += 1
        content = self.show.rounds[self._index]
        row_id = await asyncio.to_thread(
            self._db.create_round, self.session_id, self._index, content.id
        )
        rt = RoundRuntime(content=content, row_id=row_id, index=self._index)
        rt.state = RoundState.ACTIVE
        rt.opened_at = time.time()
        self._current = rt
        await asyncio.to_thread(
            self._db.update_round_state, row_id, RoundState.ACTIVE.value, rt.opened_at, None
        )

        self._publish(EngineEvent("round_opened", self.round_payload(rt)))
        self._cancel_timer()
        # duration_s <= 0 means "no auto-close": narration monologues and
        # untimed steps stay active until the operator advances them.
        if content.duration_s > 0:
            self._timer_task = asyncio.create_task(self._run_active_timer(rt))
        self._cancel_zone_task()
        if content.type != "narration":
            self._zone_task = asyncio.create_task(self._run_zone_counts(rt))
        return rt

    async def close_round(self) -> RoundRuntime:
        rt = self._current
        if rt is None or rt.state != RoundState.ACTIVE:
            raise EngineError("no active round to close")
        self._cancel_timer()
        await self._do_close(rt)
        self._timer_task = asyncio.create_task(self._run_grace_timer(rt))
        return rt

    async def reveal_round(self) -> RoundRuntime:
        rt = self._current
        if rt is None or rt.state not in (RoundState.CLOSING, RoundState.ACTIVE):
            raise EngineError("no round ready to reveal")
        self._cancel_timer()
        if rt.state == RoundState.ACTIVE:
            await self._do_close(rt)
        await self._do_reveal(rt)
        return rt

    def _cancel_timer(self) -> None:
        if self._timer_task is not None and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = None

    def _cancel_zone_task(self) -> None:
        if self._zone_task is not None and not self._zone_task.done():
            self._zone_task.cancel()
        self._zone_task = None

    def shutdown(self) -> None:
        self._cancel_timer()
        self._cancel_zone_task()

    async def _run_zone_counts(self, rt: RoundRuntime) -> None:
        """Live per-zone headcount while a round is active, for TD's bar
        visuals — distinct from the one-shot tally computed at reveal.
        """
        zones = [opt.zone for opt in rt.content.options]
        try:
            while self._current is rt and rt.state == RoundState.ACTIVE:
                counts = {zone: 0 for zone in zones}
                for player in self._bindings.all_players():
                    zone = self._current_zone(player)
                    if zone in counts:
                        counts[zone] += 1
                self._publish(EngineEvent("zone_counts", {"round_id": rt.content.id, "counts": counts}))
                await asyncio.sleep(self._zone_count_interval_s)
        except asyncio.CancelledError:
            return

    async def _run_active_timer(self, rt: RoundRuntime) -> None:
        try:
            await asyncio.sleep(rt.content.duration_s)
        except asyncio.CancelledError:
            return
        if self._current is rt and rt.state == RoundState.ACTIVE:
            await self._do_close(rt)
            self._timer_task = asyncio.create_task(self._run_grace_timer(rt))

    async def _run_grace_timer(self, rt: RoundRuntime) -> None:
        try:
            await asyncio.sleep(rt.content.grace_s)
        except asyncio.CancelledError:
            return
        if self._current is rt and rt.state == RoundState.CLOSING:
            await self._do_reveal(rt)

    # ------------------------------------------------------------------ #
    # Zone evaluation
    # ------------------------------------------------------------------ #
    async def _do_close(self, rt: RoundRuntime) -> None:
        rt.state = RoundState.CLOSING
        rt.closed_at = time.time()
        self._cancel_zone_task()
        await asyncio.to_thread(
            self._db.update_round_state, rt.row_id, RoundState.CLOSING.value, rt.opened_at, rt.closed_at
        )

        # Narration has no answers to capture — every player would just be
        # recorded absent, polluting the answers table.
        if rt.content.type != "narration":
            valid_zones = {opt.zone for opt in rt.content.options}
            for player in self._bindings.all_players():
                zone = self._current_zone(player)
                if zone in valid_zones:
                    await self._set_answer(rt, player.id, zone, "answered")
                else:
                    await self._set_answer(rt, player.id, None, "absent")

        self._publish(EngineEvent("round_closing", self.round_payload(rt)))

    async def _do_reveal(self, rt: RoundRuntime) -> None:
        # Grace window: any player who wasn't a confirmed "answered" at close
        # (either recorded absent, or claiming for the first time mid-grace —
        # both look identical from here: not captured at close, captured now)
        # and is now standing in a valid zone gets upgraded to late_grace.
        valid_zones = {opt.zone for opt in rt.content.options}
        for player in self._bindings.all_players():
            _, resolved = rt.answers.get(player.id, (None, "absent"))
            if resolved not in ("absent",):
                continue
            new_zone = self._current_zone(player)
            if new_zone in valid_zones:
                await self._set_answer(rt, player.id, new_zone, "late_grace")

        self._publish(EngineEvent("answers_locked", self.round_payload(rt)))

        tally = {opt.zone: 0 for opt in rt.content.options}
        for zone, resolved in rt.answers.values():
            if resolved in ("answered", "late_grace") and zone in tally:
                tally[zone] += 1
        rt.tally = tally
        rt.winning_zones = self._winning_zones(rt)
        rt.state = RoundState.REVEALED
        await asyncio.to_thread(
            self._db.update_round_state, rt.row_id, RoundState.REVEALED.value, rt.opened_at, rt.closed_at
        )

        for player_id, (zone, resolved) in rt.answers.items():
            if resolved in ("answered", "late_grace") and zone in rt.winning_zones:
                await asyncio.to_thread(
                    self._db.record_score_event,
                    self.session_id,
                    player_id,
                    rt.row_id,
                    rt.content.points,
                    rt.content.type,
                )

        self._publish(
            EngineEvent(
                "reveal",
                self.round_payload(rt)
                | {"tally": rt.tally, "winning_zones": rt.winning_zones},
            )
        )
        scores = await self.scores()
        self._publish(EngineEvent("scores_updated", {"scores": scores}))
        rt.state = RoundState.DONE
        await asyncio.to_thread(
            self._db.update_round_state, rt.row_id, RoundState.DONE.value, rt.opened_at, rt.closed_at
        )

    def _current_zone(self, player) -> Optional[str]:
        if player.state != PlayerState.BOUND or player.gid is None:
            return None
        state = self._tracking.get(player.gid)
        return state.zone if state else None

    async def _set_answer(
        self, rt: RoundRuntime, player_id: str, zone: Optional[str], resolved: str
    ) -> None:
        rt.answers[player_id] = (zone, resolved)
        player = self._bindings.get(player_id)
        x = y = None
        if player is not None and player.gid is not None:
            state = self._tracking.get(player.gid)
            if state and state.floor:
                x, y = state.floor
        await asyncio.to_thread(
            self._db.record_answer,
            AnswerRow(
                round_id=rt.row_id,
                session_id=self.session_id,
                player_id=player_id,
                zone_id=zone,
                resolved=resolved,
                position_x=x,
                position_y=y,
                at=time.time(),
            ),
        )

    def _winning_zones(self, rt: RoundRuntime) -> list[str]:
        if rt.content.type == "correct_zone":
            return [opt.zone for opt in rt.content.options if opt.correct]

        counts = rt.tally
        if rt.content.type == "majority":
            best = max(counts.values(), default=0)
            return [z for z, c in counts.items() if c == best and c > 0]
        if rt.content.type == "minority":
            nonzero = {z: c for z, c in counts.items() if c > 0}
            if not nonzero:
                return []
            best = min(nonzero.values())
            return [z for z, c in nonzero.items() if c == best]
        return []

    def round_payload(self, rt: RoundRuntime) -> dict:
        return {
            "round_id": rt.content.id,
            "index": rt.index,
            "state": rt.state.value,
            # Named round_type (not "type"): the player/TD WS wraps this
            # payload as {"type": <event name>, **payload}, so a "type" key
            # here would clobber the event name.
            "round_type": rt.content.type,
            "question": rt.content.question,
            "text": rt.content.text,
            "audio_url": f"/audio/{rt.content.audio}" if rt.content.audio else None,
            "form": rt.content.form,
            "form_labels": rt.content.form_labels,
            "options": [{"zone": o.zone, "label": o.label} for o in rt.content.options],
            "duration_s": rt.content.duration_s,
            "grace_s": rt.content.grace_s,
            "opened_at": rt.opened_at,
            "closed_at": rt.closed_at,
        }
