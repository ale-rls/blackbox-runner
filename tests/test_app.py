from __future__ import annotations

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
async def test_player_page_served(fake_backend, fake_zones_http, tmp_path):
    app = create_app(_settings(fake_backend, fake_zones_http, tmp_path))

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/p/seat-1")
            assert resp.status_code == 200
            assert b"Claim" in resp.content
