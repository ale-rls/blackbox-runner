"""Opt-in integration test against a real PocketBase instance.

The fast suite runs against tests/fake_pocketbase.py; this one catches
anything the fake gets subtly wrong about real PocketBase semantics
(filter parsing, json field round-trips, unique-index enforcement).

Skipped unless PB_INTEGRATION=1 is set, with connection details from the
usual POCKETBASE_* env vars / .env:

    PB_INTEGRATION=1 python -m pytest tests/test_pocketbase_integration.py

It creates one session's worth of records and deletes them afterwards.
"""

from __future__ import annotations

import os
import time

import pytest

from server.config import Settings
from server.pocketbase_client import AnswerRow, PlayerRow, PocketBaseClient

pytestmark = pytest.mark.skipif(
    os.environ.get("PB_INTEGRATION") != "1",
    reason="set PB_INTEGRATION=1 to run against the real PocketBase instance",
)


@pytest.mark.asyncio
async def test_full_surface_against_real_pocketbase():
    settings = Settings.load()
    assert settings.pocketbase_admin_email and settings.pocketbase_admin_password, (
        "POCKETBASE_ADMIN_EMAIL / POCKETBASE_ADMIN_PASSWORD must be set"
    )
    client = PocketBaseClient(
        settings.pocketbase_url,
        settings.pocketbase_admin_email,
        settings.pocketbase_admin_password,
    )
    await client.connect()
    sid = await client.create_session("integration-test")
    try:
        # players: json-null round trip (gid None vs 0) + composite upsert
        row = PlayerRow(
            id="itest-seat", session_id=sid, gid=0, display_name=None,
            state="bound", last_seen_x=0.0, last_seen_y=1.0, last_seen_at=time.time(),
        )
        await client.upsert_player(row)
        row.gid = None
        await client.upsert_player(row)
        players = await client.load_players(sid)
        assert len(players) == 1 and players[0].gid is None
        assert players[0].last_seen_x == 0.0

        # rounds + answers upsert + score sum
        rid = await client.create_round(sid, 0, "itest-q")
        await client.update_round_state(rid, "closing", time.time() - 1, time.time())
        answer = AnswerRow(
            round_id=rid, session_id=sid, player_id="itest-seat", zone_id="a",
            resolved="answered", position_x=0.5, position_y=0.5, at=time.time(),
        )
        await client.record_answer(answer)
        answer.resolved = "late_grace"
        await client.record_answer(answer)
        answers = await client.load_answers(rid)
        assert len(answers) == 1 and answers[0].resolved == "late_grace"

        await client.record_score_event(sid, "itest-seat", rid, 10, "majority")
        assert await client.sum_scores(sid) == {"itest-seat": 10}
    finally:
        for coll, filt in [
            ("score_events", f"session='{sid}'"),
            ("answers", f"session='{sid}'"),
            ("rounds", f"session='{sid}'"),
            ("binding_events", f"session='{sid}'"),
            ("players", f"session='{sid}'"),
        ]:
            for rec in await client._list_all(coll, filter_=filt):
                await client._delete(coll, rec["id"])
        await client._delete("sessions", sid)
        await client.close()
