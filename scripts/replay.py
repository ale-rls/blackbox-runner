#!/usr/bin/env python3
"""CLI for server/replay.py — post-show timeline and state reconstruction.

Usage:
    python scripts/replay.py --db data/theater-game.db --list-sessions
    python scripts/replay.py --db data/theater-game.db --timeline
    python scripts/replay.py --db data/theater-game.db --player seat-14
    python scripts/replay.py --db data/theater-game.db --at 1730000000
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.persistence import Database  # noqa: E402
from server.replay import (  # noqa: E402
    binding_state_at,
    build_timeline,
    explain_player,
    scores_at,
)


def _fmt_time(at: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(at))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--db", default="data/theater-game.db", help="path to the SQLite DB file")
    parser.add_argument("--session", type=int, help="session id (default: the most recent one)")
    parser.add_argument("--list-sessions", action="store_true", help="list sessions and exit")
    parser.add_argument("--timeline", action="store_true", help="print the full chronological event log")
    parser.add_argument("--player", help="print one player's full history")
    parser.add_argument("--at", type=float, help="reconstruct binding/score state as of this unix timestamp")
    args = parser.parse_args()

    db = Database(args.db)
    try:
        if args.list_sessions:
            for row in db.list_sessions():
                print(f"session {row['id']}: started {_fmt_time(row['started_at'])}  status={row['status']}")
            return 0

        session_id = args.session
        if session_id is None:
            sessions = db.list_sessions()
            if not sessions:
                print("No sessions in this database.")
                return 1
            session_id = sessions[-1]["id"]
            print(f"(using most recent session: {session_id})")

        if args.player:
            for e in explain_player(db, session_id, args.player):
                print(f"{_fmt_time(e.at)}  {e.kind:8s}  {e.detail}")
            return 0

        if args.at is not None:
            print(f"--- state at {_fmt_time(args.at)} ---")
            bindings = binding_state_at(db, session_id, args.at)
            for player_id, snap in sorted(bindings.items()):
                print(f"  {player_id:12s} gid={snap.gid}  state={snap.state}")
            print("--- scores ---")
            for player_id, points in sorted(scores_at(db, session_id, args.at).items()):
                print(f"  {player_id:12s} {points}")
            return 0

        # Default: full timeline.
        for e in build_timeline(db, session_id):
            print(f"{_fmt_time(e.at)}  {e.kind:8s}  {e.player_id:12s} {e.detail}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
