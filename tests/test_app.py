from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from server.app import create_app
from server.config import Settings


@pytest.mark.asyncio
async def test_health_and_zones_after_startup(fake_backend, fake_zones_http):
    settings = Settings(
        tracking_ws_url=fake_backend.ws_url,
        tracking_http_url=fake_zones_http,
    )
    app = create_app(settings)

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
