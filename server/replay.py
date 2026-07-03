"""Post-show replay: rebuild the full event timeline, or the binding/score
state at any single moment, from the persisted ``binding_events``/
``answers``/``score_events`` tables (docs/architecture.md §5, §7).

This is what makes "every rebind must be explainable after the show"
concrete rather than aspirational — a real question a director asks the
morning after ("why did seat-14 lose their points in round 3?") should be
answerable by pointing at a specific row, not a shrug.

Read-only: this module never writes to the database, so it's safe to run
against a live show's DB file (WAL mode allows concurrent reads) as well as
after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .persistence import Database


@dataclass(slots=True)
class TimelineEntry:
    at: float
    kind: str  # "binding" | "answer" | "score"
    player_id: str
    detail: dict


def build_timeline(db: Database, session_id: int) -> list[TimelineEntry]:
    """Every binding, answer, and score event for a session, chronological."""
    entries: list[TimelineEntry] = []

    for e in db.load_binding_events(session_id):
        entries.append(
            TimelineEntry(
                at=e.at,
                kind="binding",
                player_id=e.player_id,
                detail={"old_gid": e.old_gid, "new_gid": e.new_gid, "reason": e.reason, "actor": e.actor},
            )
        )

    for r in db.load_rounds(session_id):
        for a in db.load_answers(r.id):
            entries.append(
                TimelineEntry(
                    at=a.at,
                    kind="answer",
                    player_id=a.player_id,
                    detail={
                        "round_id": r.id,
                        "question_id": r.question_id,
                        "zone_id": a.zone_id,
                        "resolved": a.resolved,
                    },
                )
            )

    for s in db.load_score_events(session_id):
        entries.append(
            TimelineEntry(
                at=s.at,
                kind="score",
                player_id=s.player_id,
                detail={"round_id": s.round_id, "points": s.points, "reason": s.reason},
            )
        )

    entries.sort(key=lambda e: e.at)
    return entries


@dataclass(slots=True)
class PlayerSnapshot:
    gid: Optional[int]
    state: str  # "bound" | "lost" (only states binding_events distinguish)


def binding_state_at(db: Database, session_id: int, at: float) -> dict[str, PlayerSnapshot]:
    """Every player's gid/state as of ``at``, by replaying binding_events
    in order up to (and including) that timestamp."""
    snapshots: dict[str, PlayerSnapshot] = {}
    for e in db.load_binding_events(session_id):
        if e.at > at:
            break
        if e.reason == "lost":
            snapshots[e.player_id] = PlayerSnapshot(gid=None, state="lost")
        else:
            snapshots[e.player_id] = PlayerSnapshot(gid=e.new_gid, state="bound")
    return snapshots


def scores_at(db: Database, session_id: int, at: float) -> dict[str, int]:
    """Every player's running total as of ``at``."""
    totals: dict[str, int] = {}
    for s in db.load_score_events(session_id):
        if s.at > at:
            break
        totals[s.player_id] = totals.get(s.player_id, 0) + s.points
    return totals


def explain_player(db: Database, session_id: int, player_id: str) -> list[TimelineEntry]:
    """The full history for one player — binding changes, answers, and
    score events, chronological. Directly answers "why did X happen to
    seat-14?"."""
    return [e for e in build_timeline(db, session_id) if e.player_id == player_id]
