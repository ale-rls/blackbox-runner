#!/usr/bin/env python3
"""Create the game's PocketBase collections (issue #16).

Idempotent: collections that already exist are left untouched (pass
--force to delete and recreate them — destroys their records!). Run once
against a fresh PocketBase instance before first boot:

    python scripts/pocketbase_bootstrap.py

Connection settings come from Settings/.env (POCKETBASE_URL,
POCKETBASE_ADMIN_EMAIL, POCKETBASE_ADMIN_PASSWORD) unless overridden with
flags.

Design notes (mirrors server/pocketbase_client.py's expectations):
* Nullable numeric columns (gid, floor coords, opened_at/closed_at) are
  ``json`` fields — PocketBase returns zero-values, not null, for empty
  number fields, and 0 is a legitimate gid/coordinate.
* All fields are required=false: PocketBase's "required" means "non-empty",
  and 0/"" are legitimate values for several columns. Validation lives in
  the Python layer (content.py / enums), as it always has.
* Rules: null = superuser only. The deployed player frontend (issue #16)
  has no route to the venue game server, so everything it reads or writes
  goes through PocketBase directly: ``rounds``, ``score_events``,
  ``players``, ``game_state`` (available GIDs) and ``player_reveals`` (each
  player's own answer) get public read; ``claim_requests`` additionally
  gets public *create* so a phone can submit a claim. The game server
  consumes ``claim_requests`` over the realtime stream and performs the
  actual binding. ``answers`` — which would expose every player's
  individual choice — stays superuser-only; the public ``player_reveals``
  projection only ever holds a player's *own* result.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import Settings  # noqa: E402

PUBLIC_READ = {"listRule": "", "viewRule": ""}
# claim_requests also needs public create: the phone POSTs its claim with
# no credentials. update/delete stay superuser-only (the game server writes
# the result back), so a phone can submit but not forge a decision.
PUBLIC_CREATE = {"createRule": ""}


def _collections(session_id: str, rounds_id: str) -> list[dict]:
    """Collection definitions. ``session_id``/``rounds_id`` are the created
    PocketBase collection ids that relation fields must reference."""

    def rel(name: str, target: str) -> dict:
        return {
            "name": name,
            "type": "relation",
            "collectionId": target,
            "maxSelect": 1,
            "cascadeDelete": False,
        }

    def f(name: str, type_: str) -> dict:
        return {"name": name, "type": type_}

    return [
        {
            "name": "players",
            "type": "base",
            "fields": [
                rel("session", session_id),
                f("player_key", "text"),
                f("gid", "json"),
                f("display_name", "text"),
                f("state", "text"),
                f("last_seen_x", "json"),
                f("last_seen_y", "json"),
                f("last_seen_at", "json"),
            ],
            "indexes": [
                "CREATE UNIQUE INDEX idx_players_session_key ON players (session, player_key)"
            ],
            # Public read so the deployed phone can subscribe to its own
            # player record (bound/lost/orphaned state) with no credentials.
            **PUBLIC_READ,
        },
        {
            "name": "binding_events",
            "type": "base",
            "fields": [
                rel("session", session_id),
                f("player_key", "text"),
                f("old_gid", "json"),
                f("new_gid", "json"),
                f("reason", "text"),
                f("actor", "text"),
                f("at", "number"),
            ],
        },
        {
            "name": "answers",
            "type": "base",
            "fields": [
                rel("round", rounds_id),
                rel("session", session_id),
                f("player_key", "text"),
                f("zone_id", "text"),
                f("resolved", "text"),
                f("position_x", "json"),
                f("position_y", "json"),
                f("at", "number"),
            ],
            "indexes": [
                "CREATE UNIQUE INDEX idx_answers_round_player ON answers (round, player_key)"
            ],
        },
        {
            "name": "score_events",
            "type": "base",
            "fields": [
                rel("session", session_id),
                f("player_key", "text"),
                rel("round", rounds_id),
                f("points", "number"),
                f("reason", "text"),
                f("at", "number"),
            ],
            **PUBLIC_READ,
        },
        {
            "name": "content_meta",
            "type": "base",
            "fields": [f("version", "text")],
        },
        {
            "name": "content_rounds",
            "type": "base",
            "fields": [
                f("round_id", "text"),
                # ord deliberately has no unique index: save_content diffs
                # by round_id and PATCHes records in place, so a reorder
                # swaps ords sequentially — transiently duplicated, finally
                # consistent (the writer always emits 0..n-1).
                f("ord", "number"),
                f("question", "text"),
                f("type", "text"),
                f("duration_s", "number"),
                f("grace_s", "number"),
                f("points", "number"),
                f("text", "text"),
                f("audio", "text"),
                f("form", "text"),
                f("zone_layout", "text"),
                f("form_labels", "json"),
                f("options", "json"),
                # Narration mp3, uploaded by save_content; the player
                # frontend builds its URL with pb.files.getURL(). File URLs
                # are public-by-default in PocketBase even though the
                # records themselves stay superuser-only.
                {
                    "name": "audio_file",
                    "type": "file",
                    "maxSelect": 1,
                    "maxSize": 52428800,
                },
            ],
            "indexes": [
                "CREATE UNIQUE INDEX idx_content_rounds_rid ON content_rounds (round_id)",
            ],
        },
        {
            # Singleton (one row) holding the live list of GIDs that are
            # currently tracked but unbound — what a phone is allowed to
            # claim. The game server rewrites it whenever the set changes;
            # the phone subscribes for realtime updates.
            "name": "game_state",
            "type": "base",
            "fields": [
                # The active session id, so the phone filters its player and
                # reveal records to the current show (a returning player_key
                # may still have stale rows from a previous session).
                f("session_id", "text"),
                f("available_gids", "json"),
                f("updated_at", "number"),
            ],
            **PUBLIC_READ,
        },
        {
            # A phone's claim submission. The phone creates a row (public
            # create); the game server, subscribed over realtime, runs the
            # real binding and PATCHes status/detail back. Public read so the
            # phone can watch its own request resolve (or fail).
            "name": "claim_requests",
            "type": "base",
            "fields": [
                f("player_key", "text"),
                f("gid", "json"),
                f("display_name", "text"),
                # status: pending -> done | error. detail carries the error
                # message (e.g. "gid 12 is not currently active").
                f("status", "text"),
                f("detail", "text"),
                f("at", "number"),
            ],
            **PUBLIC_READ,
            **PUBLIC_CREATE,
        },
        {
            # Per-player reveal projection: each player's *own* answer for a
            # revealed round, so the phone can show "you were here" without
            # the private answers table being readable. One row per player
            # per round.
            "name": "player_reveals",
            "type": "base",
            "fields": [
                rel("session", session_id),
                rel("round", rounds_id),
                f("player_key", "text"),
                f("zone", "text"),
                f("resolved", "text"),
                f("at", "number"),
            ],
            "indexes": [
                "CREATE UNIQUE INDEX idx_player_reveals_round_player "
                "ON player_reveals (round, player_key)"
            ],
            **PUBLIC_READ,
        },
        {
            # Singleton (one row) mirroring the live per-zone headcount the
            # engine streams to /ws/td while a round is active. Only the
            # /listen page subscribes to it — a separate collection (not a
            # game_state field) so its ~1s update cadence never wakes the
            # phones' game_state subscriptions.
            "name": "live_stats",
            "type": "base",
            "fields": [
                f("session_id", "text"),
                f("round_id", "text"),
                f("zone_counts", "json"),
                f("updated_at", "number"),
            ],
            **PUBLIC_READ,
        },
    ]


SESSIONS_DEF = {
    "name": "sessions",
    "type": "base",
    "fields": [
        {"name": "started_at", "type": "number"},
        {"name": "content_version", "type": "text"},
        {"name": "status", "type": "text"},
    ],
}

ROUNDS_DEF = {
    # session relation is appended once the sessions collection id is known
    "name": "rounds",
    "type": "base",
    "fields": [
        {"name": "idx", "type": "number"},
        {"name": "question_id", "type": "text"},
        {"name": "state", "type": "text"},
        {"name": "opened_at", "type": "json"},
        {"name": "closed_at", "type": "json"},
        # The full round payload the player frontend renders (question,
        # options, form, audio ref, and at reveal tally/winning_zones),
        # denormalized here so the phone gets everything from one public
        # ``rounds`` realtime event — no join against superuser-only content.
        {"name": "payload", "type": "json"},
    ],
    **PUBLIC_READ,
}


async def main() -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=settings.pocketbase_url)
    parser.add_argument("--email", default=settings.pocketbase_admin_email)
    parser.add_argument("--password", default=settings.pocketbase_admin_password)
    parser.add_argument(
        "--force",
        action="store_true",
        help="delete and recreate existing game collections (DESTROYS their records)",
    )
    args = parser.parse_args()
    if not args.email or not args.password:
        print("POCKETBASE_ADMIN_EMAIL / POCKETBASE_ADMIN_PASSWORD not set", file=sys.stderr)
        return 2

    async with httpx.AsyncClient(
        base_url=args.url.rstrip("/"), timeout=15.0, follow_redirects=True
    ) as http:
        resp = await http.post(
            "/api/collections/_superusers/auth-with-password",
            json={"identity": args.email, "password": args.password},
        )
        if resp.status_code != 200:
            print(f"auth failed ({resp.status_code}): {resp.text}", file=sys.stderr)
            return 2
        headers = {"Authorization": resp.json()["token"]}

        resp = await http.get("/api/collections", params={"perPage": 200}, headers=headers)
        resp.raise_for_status()
        existing = {c["name"]: c for c in resp.json()["items"]}

        async def ensure(defn: dict) -> dict:
            name = defn["name"]
            if name in existing:
                if not args.force:
                    print(f"  = {name} (exists, skipped)")
                    return existing[name]
                resp = await http.delete(
                    f"/api/collections/{existing[name]['id']}", headers=headers
                )
                if resp.status_code >= 400:
                    print(f"delete {name} failed: {resp.text}", file=sys.stderr)
                    raise SystemExit(1)
                print(f"  - {name} (deleted)")
            resp = await http.post("/api/collections", json=defn, headers=headers)
            if resp.status_code >= 400:
                print(f"create {name} failed ({resp.status_code}): {resp.text}", file=sys.stderr)
                raise SystemExit(1)
            print(f"  + {name}")
            return resp.json()

        print(f"Bootstrapping collections on {args.url} ...")
        sessions = await ensure(SESSIONS_DEF)

        rounds_def = dict(ROUNDS_DEF)
        rounds_def["fields"] = [
            {
                "name": "session",
                "type": "relation",
                "collectionId": sessions["id"],
                "maxSelect": 1,
                "cascadeDelete": False,
            },
            *ROUNDS_DEF["fields"],
        ]
        rounds = await ensure(rounds_def)

        for defn in _collections(sessions["id"], rounds["id"]):
            await ensure(defn)

    print(
        "Done. Public-read: rounds, score_events, players, game_state, "
        "player_reveals; claim_requests is public read+create; "
        "answers and everything else stay superuser-only."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
