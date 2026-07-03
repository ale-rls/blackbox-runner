"""SQLite persistence (WAL mode), write-through on every meaningful state
transition so a crashed game server reloads mid-show exactly where it died.

Synchronous by design (stdlib ``sqlite3``); callers on the asyncio event loop
should invoke these methods via ``asyncio.to_thread`` to avoid blocking on
disk I/O. A single connection is shared and guarded by a lock, matching
TrackingBox's own thread-safety pattern for its in-memory store.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    content_version TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS players (
    id TEXT NOT NULL,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    gid INTEGER,
    display_name TEXT,
    state TEXT NOT NULL,
    last_seen_x REAL,
    last_seen_y REAL,
    last_seen_at REAL,
    PRIMARY KEY (session_id, id)
);
CREATE INDEX IF NOT EXISTS idx_players_gid ON players(session_id, gid);

CREATE TABLE IF NOT EXISTS binding_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    player_id TEXT NOT NULL,
    old_gid INTEGER,
    new_gid INTEGER,
    reason TEXT NOT NULL,
    actor TEXT,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_binding_events_session ON binding_events(session_id);

CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    idx INTEGER NOT NULL,
    question_id TEXT NOT NULL,
    state TEXT NOT NULL,
    opened_at REAL,
    closed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_rounds_session ON rounds(session_id);

CREATE TABLE IF NOT EXISTS answers (
    round_id INTEGER NOT NULL REFERENCES rounds(id),
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    player_id TEXT NOT NULL,
    zone_id TEXT,
    resolved TEXT NOT NULL,
    position_x REAL,
    position_y REAL,
    at REAL NOT NULL,
    PRIMARY KEY (round_id, player_id)
);

CREATE TABLE IF NOT EXISTS score_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    player_id TEXT NOT NULL,
    round_id INTEGER REFERENCES rounds(id),
    points INTEGER NOT NULL,
    reason TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_score_events_session ON score_events(session_id);
"""


@dataclass(slots=True)
class PlayerRow:
    id: str
    session_id: int
    gid: Optional[int]
    display_name: Optional[str]
    state: str
    last_seen_x: Optional[float]
    last_seen_y: Optional[float]
    last_seen_at: Optional[float]


@dataclass(slots=True)
class BindingEventRow:
    id: int
    session_id: int
    player_id: str
    old_gid: Optional[int]
    new_gid: Optional[int]
    reason: str
    actor: Optional[str]
    at: float


@dataclass(slots=True)
class RoundRow:
    id: int
    session_id: int
    idx: int
    question_id: str
    state: str
    opened_at: Optional[float]
    closed_at: Optional[float]


@dataclass(slots=True)
class AnswerRow:
    round_id: int
    session_id: int
    player_id: str
    zone_id: Optional[str]
    resolved: str
    position_x: Optional[float]
    position_y: Optional[float]
    at: float


@dataclass(slots=True)
class ScoreEventRow:
    id: int
    session_id: int
    player_id: str
    round_id: Optional[int]
    points: int
    reason: str
    at: float


class Database:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #
    def get_active_session_id(self) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM sessions WHERE status = 'active' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row["id"] if row else None

    def create_session(self, content_version: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions (started_at, content_version, status) VALUES (?, ?, 'active')",
                (time.time(), content_version),
            )
            self._conn.commit()
            return cur.lastrowid

    # ------------------------------------------------------------------ #
    # Players
    # ------------------------------------------------------------------ #
    def load_players(self, session_id: int) -> list[PlayerRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, session_id, gid, display_name, state, last_seen_x, last_seen_y, last_seen_at "
                "FROM players WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            return [PlayerRow(**dict(row)) for row in rows]

    def upsert_player(self, row: PlayerRow) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO players
                    (id, session_id, gid, display_name, state, last_seen_x, last_seen_y, last_seen_at)
                VALUES
                    (:id, :session_id, :gid, :display_name, :state, :last_seen_x, :last_seen_y, :last_seen_at)
                ON CONFLICT(session_id, id) DO UPDATE SET
                    gid=excluded.gid,
                    display_name=excluded.display_name,
                    state=excluded.state,
                    last_seen_x=excluded.last_seen_x,
                    last_seen_y=excluded.last_seen_y,
                    last_seen_at=excluded.last_seen_at
                """,
                asdict(row),
            )
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # Binding events (audit trail)
    # ------------------------------------------------------------------ #
    def record_binding_event(
        self,
        session_id: int,
        player_id: str,
        old_gid: Optional[int],
        new_gid: Optional[int],
        reason: str,
        actor: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO binding_events (session_id, player_id, old_gid, new_gid, reason, actor, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, player_id, old_gid, new_gid, reason, actor, time.time()),
            )
            self._conn.commit()

    def load_binding_events(self, session_id: int) -> list[BindingEventRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, session_id, player_id, old_gid, new_gid, reason, actor, at "
                "FROM binding_events WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
            return [BindingEventRow(**dict(row)) for row in rows]

    # ------------------------------------------------------------------ #
    # Rounds
    # ------------------------------------------------------------------ #
    def create_round(self, session_id: int, idx: int, question_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO rounds (session_id, idx, question_id, state, opened_at, closed_at) "
                "VALUES (?, ?, ?, 'pending', NULL, NULL)",
                (session_id, idx, question_id),
            )
            self._conn.commit()
            return cur.lastrowid

    def update_round_state(
        self,
        round_id: int,
        state: str,
        opened_at: Optional[float],
        closed_at: Optional[float],
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE rounds SET state = ?, opened_at = ?, closed_at = ? WHERE id = ?",
                (state, opened_at, closed_at, round_id),
            )
            self._conn.commit()

    def load_rounds(self, session_id: int) -> list[RoundRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, session_id, idx, question_id, state, opened_at, closed_at "
                "FROM rounds WHERE session_id = ? ORDER BY idx",
                (session_id,),
            ).fetchall()
            return [RoundRow(**dict(row)) for row in rows]

    # ------------------------------------------------------------------ #
    # Answers
    # ------------------------------------------------------------------ #
    def record_answer(self, row: AnswerRow) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO answers
                    (round_id, session_id, player_id, zone_id, resolved, position_x, position_y, at)
                VALUES
                    (:round_id, :session_id, :player_id, :zone_id, :resolved, :position_x, :position_y, :at)
                ON CONFLICT(round_id, player_id) DO UPDATE SET
                    zone_id=excluded.zone_id,
                    resolved=excluded.resolved,
                    position_x=excluded.position_x,
                    position_y=excluded.position_y,
                    at=excluded.at
                """,
                asdict(row),
            )
            self._conn.commit()

    def load_answers(self, round_id: int) -> list[AnswerRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT round_id, session_id, player_id, zone_id, resolved, position_x, position_y, at "
                "FROM answers WHERE round_id = ?",
                (round_id,),
            ).fetchall()
            return [AnswerRow(**dict(row)) for row in rows]

    # ------------------------------------------------------------------ #
    # Score events (scores are always SUM(points), never a mutable counter)
    # ------------------------------------------------------------------ #
    def record_score_event(
        self, session_id: int, player_id: str, round_id: Optional[int], points: int, reason: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO score_events (session_id, player_id, round_id, points, reason, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, player_id, round_id, points, reason, time.time()),
            )
            self._conn.commit()

    def sum_scores(self, session_id: int) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT player_id, SUM(points) AS total FROM score_events "
                "WHERE session_id = ? GROUP BY player_id",
                (session_id,),
            ).fetchall()
            return {row["player_id"]: row["total"] for row in rows}
