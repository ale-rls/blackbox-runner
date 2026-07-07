#!/usr/bin/env python3
"""CLI for server/replay.py — post-show timeline and state reconstruction.

Usage:
    python scripts/replay.py --list-sessions
    python scripts/replay.py --timeline
    python scripts/replay.py --player seat-14
    python scripts/replay.py --at 1730000000

PocketBase connection settings come from Settings/.env (POCKETBASE_URL,
POCKETBASE_ADMIN_EMAIL, POCKETBASE_ADMIN_PASSWORD) unless overridden with
--pb-url/--pb-email/--pb-password. Safe to run against a live show — this
tool only reads.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import Settings  # noqa: E402
from server.pocketbase_client import PocketBaseClient  # noqa: E402
from server.replay import (  # noqa: E402
    binding_state_at,
    build_timeline,
    explain_player,
    scores_at,
)


def _fmt_time(at: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(at))


async def main() -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--pb-url", default=settings.pocketbase_url)
    parser.add_argument("--pb-email", default=settings.pocketbase_admin_email)
    parser.add_argument("--pb-password", default=settings.pocketbase_admin_password)
    parser.add_argument("--session", help="session id (default: the most recent one)")
    parser.add_argument("--list-sessions", action="store_true", help="list sessions and exit")
    parser.add_argument("--timeline", action="store_true", help="print the full chronological event log")
    parser.add_argument("--player", help="print one player's full history")
    parser.add_argument("--at", type=float, help="reconstruct binding/score state as of this unix timestamp")
    args = parser.parse_args()

    if not args.pb_email or not args.pb_password:
        print("POCKETBASE_ADMIN_EMAIL / POCKETBASE_ADMIN_PASSWORD not set", file=sys.stderr)
        return 2

    db = PocketBaseClient(args.pb_url, args.pb_email, args.pb_password)
    try:
        await db.connect()
        if args.list_sessions:
            for row in await db.list_sessions():
                print(f"session {row.id}: started {_fmt_time(row.started_at)}  status={row.status}")
            return 0

        session_id = args.session
        if session_id is None:
            sessions = await db.list_sessions()
            if not sessions:
                print("No sessions in this database.")
                return 1
            session_id = sessions[-1].id
            print(f"(using most recent session: {session_id})")

        if args.player:
            for e in await explain_player(db, session_id, args.player):
                print(f"{_fmt_time(e.at)}  {e.kind:8s}  {e.detail}")
            return 0

        if args.at is not None:
            print(f"--- state at {_fmt_time(args.at)} ---")
            bindings = await binding_state_at(db, session_id, args.at)
            for player_id, snap in sorted(bindings.items()):
                print(f"  {player_id:12s} gid={snap.gid}  state={snap.state}")
            print("--- scores ---")
            for player_id, points in sorted((await scores_at(db, session_id, args.at)).items()):
                print(f"  {player_id:12s} {points}")
            return 0

        # Default: full timeline.
        for e in await build_timeline(db, session_id):
            print(f"{_fmt_time(e.at)}  {e.kind:8s}  {e.player_id:12s} {e.detail}")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
