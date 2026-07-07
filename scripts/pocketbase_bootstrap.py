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
* Rules: null = superuser only. ``rounds`` and ``score_events`` get public
  read ("" list/view) so the Svelte player frontend (issue #17) can
  subscribe to realtime without credentials; everything else — including
  ``answers``, which would expose each player's individual choices — stays
  superuser-only.
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
            ],
            "indexes": [
                "CREATE UNIQUE INDEX idx_content_rounds_rid ON content_rounds (round_id)",
                "CREATE UNIQUE INDEX idx_content_rounds_ord ON content_rounds (ord)",
            ],
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

    async with httpx.AsyncClient(base_url=args.url.rstrip("/"), timeout=15.0) as http:
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

    print("Done. rounds + score_events are public-read; everything else superuser-only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
