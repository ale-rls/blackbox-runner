from __future__ import annotations

import asyncio

import httpx
import pytest
from asgi_lifespan import LifespanManager

from server.app import create_app
from server.config import Settings


def _settings(fake_backend, fake_zones_http, tmp_path) -> Settings:
    return Settings(
        tracking_ws_url=fake_backend.ws_url,
        tracking_http_url=fake_zones_http,
        db_path=str(tmp_path / "game.db"),
    )


@pytest.mark.asyncio
async def test_health_and_zones_after_startup(fake_backend, fake_zones_http, tmp_path):
    app = create_app(_settings(fake_backend, fake_zones_http, tmp_path))

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = (await client.get("/health")).json()
            assert health["status"] == "ok"
            assert health["tracking_connected"] is True

            zones = (await client.get("/api/tracking/zones")).json()
            assert zones["enabled"] is True
            assert {z["id"] for z in zones["zones"]} == {"answer_a", "answer_b"}

            audience = (await client.get("/api/tracking/audience")).json()
            gids = {p["gid"] for p in audience}
            assert 1 in gids


@pytest.mark.asyncio
async def test_claim_flow_and_admin_rebind(fake_backend, fake_zones_http, tmp_path):
    app = create_app(_settings(fake_backend, fake_zones_http, tmp_path))

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # fake_backend's canned snapshot has gid 1 and gid 2 active.
            resp = await client.post("/api/players/seat-1/claim", json={"gid": 1, "display_name": "Alex"})
            assert resp.status_code == 200
            player = resp.json()
            assert player["state"] == "bound"
            assert player["gid"] == 1

            # Claiming an already-bound gid from another player is rejected.
            resp = await client.post("/api/players/seat-2/claim", json={"gid": 1})
            assert resp.status_code == 409

            # Claiming a gid that TrackingBox has never seen is rejected.
            resp = await client.post("/api/players/seat-3/claim", json={"gid": 12345})
            assert resp.status_code == 409

            resp = await client.get("/api/players")
            assert len(resp.json()) == 1

            # Operator rebind can onboard a brand-new player id directly (an
            # usher fixing a failed phone claim) and, unlike self-service
            # claim, is allowed to steal an already-bound gid.
            resp = await client.post(
                "/api/admin/players/usher-seat/rebind", json={"gid": 1, "actor": "usher-1"}
            )
            assert resp.status_code == 200
            assert resp.json()["gid"] == 1

            resp = await client.get("/api/players/seat-1")
            assert resp.json()["state"] == "lost"

            resp = await client.get("/api/players")
            assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_round_lifecycle_via_admin_endpoints(fake_backend, fake_zones_http, tmp_path):
    app = create_app(_settings(fake_backend, fake_zones_http, tmp_path))

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # gid 1 -> answer_a per the fake backend's canned snapshot. (gid 2 is
            # scripted to go invisible shortly after connect, so it's not used
            # here — majority/minority scoring math is covered by test_engine.py;
            # this test is only about the admin REST wiring.)
            assert (await client.post("/api/players/seat-1/claim", json={"gid": 1})).status_code == 200

            resp = await client.get("/api/rounds/current")
            assert resp.json() is None

            resp = await client.post("/api/admin/rounds/start")
            assert resp.status_code == 200
            round_payload = resp.json()
            assert round_payload["state"] == "active"
            assert round_payload["index"] == 0

            resp = await client.get("/api/rounds/current")
            assert resp.json()["state"] == "active"

            resp = await client.post("/api/admin/rounds/close")
            assert resp.status_code == 200
            assert resp.json()["state"] == "closing"

            resp = await client.post("/api/admin/rounds/reveal")
            assert resp.status_code == 200
            # RoundRuntime.state is "revealed" only transiently in the "reveal"
            # engine event; by the time reveal_round() returns it's "done".
            assert resp.json()["state"] == "done"

            resp = await client.get("/api/scores")
            scores = resp.json()
            assert scores  # someone scored (r1's answer_a/answer_b split by seat-1/seat-2)

            # Closing again with nothing active is rejected.
            resp = await client.post("/api/admin/rounds/close")
            assert resp.status_code == 409


@pytest.mark.asyncio
async def test_admin_fire_cue_reaches_td_subscribers(fake_backend, fake_zones_http, tmp_path):
    app = create_app(_settings(fake_backend, fake_zones_http, tmp_path))

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            engine = app.state.engine
            queue = engine.subscribe()

            resp = await client.post(
                "/api/admin/cues/ritual_prompt",
                json={"payload": {"player_id": "seat-1", "corner_zone": "answer_a"}},
            )
            assert resp.status_code == 200
            assert resp.json() == {
                "ok": True,
                "cue": "ritual_prompt",
                "payload": {"player_id": "seat-1", "corner_zone": "answer_a"},
            }

            event = await asyncio.wait_for(queue.get(), timeout=1)
            assert event.type == "ritual_prompt"
            assert event.payload["player_id"] == "seat-1"

            # No body at all is also valid — defaults to an empty payload.
            resp = await client.post("/api/admin/cues/blackout")
            assert resp.status_code == 200
            event = await asyncio.wait_for(queue.get(), timeout=1)
            assert event.type == "blackout"
            assert event.payload == {}


@pytest.mark.asyncio
async def test_player_page_served(fake_backend, fake_zones_http, tmp_path):
    app = create_app(_settings(fake_backend, fake_zones_http, tmp_path))

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/p/seat-1")
            assert resp.status_code == 200
            assert b"Claim" in resp.content
