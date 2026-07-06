from __future__ import annotations

import asyncio

import httpx
import pytest
import yaml
from asgi_lifespan import LifespanManager

from server import content_db
from server.app import create_app
from server.config import Settings
from server.persistence import Database


# A minimal scoreable show against conftest's answer_a/answer_b zones, so app
# tests don't depend on the production script in content/show.yaml (whose
# first step is an unscored narration monologue).
_TEST_SHOW = (
    "version: '1'\n"
    "rounds:\n"
    "  - id: r1\n"
    "    question: Coffee or tea?\n"
    "    options:\n"
    "      - {zone: answer_a, label: Coffee}\n"
    "      - {zone: answer_b, label: Tea}\n"
)


def _import_show(db_path: str, yaml_text: str) -> None:
    """Push a YAML show into the game DB, as scripts/import_content.py does."""
    raw = yaml.safe_load(yaml_text)
    db = Database(db_path)
    try:
        db.save_content(str(raw.get("version", "1")), content_db.rows_from_raw(raw["rounds"]))
    finally:
        db.close()


def _settings(fake_backend, fake_zones_http, tmp_path) -> Settings:
    db_path = str(tmp_path / "game.db")
    _import_show(db_path, _TEST_SHOW)
    return Settings(
        tracking_ws_url=fake_backend.ws_url,
        tracking_http_url=fake_zones_http,
        db_path=db_path,
        audio_dir=str(tmp_path / "audio"),
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
async def test_content_reload_endpoint(fake_backend, fake_zones_http, tmp_path):
    db_path = str(tmp_path / "game.db")
    _import_show(
        db_path,
        "version: '1'\n"
        "rounds:\n"
        "  - id: r1\n"
        "    question: Original\n"
        "    options:\n"
        "      - {zone: answer_a, label: A}\n"
        "      - {zone: answer_b, label: B}\n",
    )
    settings = Settings(
        tracking_ws_url=fake_backend.ws_url,
        tracking_http_url=fake_zones_http,
        db_path=db_path,
    )
    app = create_app(settings)

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert app.state.show.rounds[0].question == "Original"

            # Re-import against the live server's DB (separate connection,
            # as scripts/import_content.py would), then hot-reload.
            _import_show(
                db_path,
                "version: '2'\n"
                "rounds:\n"
                "  - id: r1\n"
                "    question: Updated\n"
                "    options:\n"
                "      - {zone: answer_a, label: A}\n"
                "      - {zone: answer_b, label: B}\n",
            )
            resp = await client.post("/api/admin/content/reload")
            assert resp.status_code == 200
            assert resp.json() == {"ok": True, "rounds": 1}
            assert app.state.show.rounds[0].question == "Updated"

            # Refused once a round is in flight.
            await client.post("/api/admin/rounds/start")
            _import_show(
                db_path,
                "version: '3'\n"
                "rounds:\n"
                "  - id: r1\n"
                "    question: Should not apply\n"
                "    options:\n"
                "      - {zone: answer_a, label: A}\n"
                "      - {zone: answer_b, label: B}\n",
            )
            resp = await client.post("/api/admin/content/reload")
            assert resp.status_code == 409
            assert app.state.engine.show.rounds[0].question == "Updated"


@pytest.mark.asyncio
async def test_show_editor_endpoints(fake_backend, fake_zones_http, tmp_path):
    settings = _settings(fake_backend, fake_zones_http, tmp_path)
    app = create_app(settings)

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/admin/content")
            assert resp.status_code == 200
            content = resp.json()
            assert content["tts"]["configured"] is False
            assert [r["id"] for r in content["rounds"]] == ["r1"]
            assert content["rounds"][0]["audio_exists"] is False

            # Edit between rounds: stored rows + running engine both update.
            resp = await client.put(
                "/api/admin/content/rounds/r1", json={"question": "Beer or wine?"}
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["reloaded"] is True
            assert body["round"]["question"] == "Beer or wine?"
            _, rows = app.state.db.load_content()
            assert rows[0].question == "Beer or wine?"
            assert app.state.show.rounds[0].question == "Beer or wine?"

            # Invalid edits are rejected and never reach the DB.
            before = app.state.db.load_content()
            resp = await client.put("/api/admin/content/rounds/r1", json={"id": "r2"})
            assert resp.status_code == 400
            resp = await client.put(
                "/api/admin/content/rounds/r1",
                json={"options": [{"zone": "bogus_zone", "label": "X"}]},
            )
            assert resp.status_code == 400
            assert app.state.db.load_content() == before

            resp = await client.put("/api/admin/content/rounds/ghost", json={"question": "?"})
            assert resp.status_code == 400

            # Mid-round: the edit is saved to the DB but not hot-applied.
            assert (await client.post("/api/admin/rounds/start")).status_code == 200
            resp = await client.put(
                "/api/admin/content/rounds/r1", json={"question": "Mid-round edit"}
            )
            assert resp.status_code == 200
            assert resp.json()["reloaded"] is False
            _, rows = app.state.db.load_content()
            assert rows[0].question == "Mid-round edit"
            assert app.state.show.rounds[0].question == "Beer or wine?"


@pytest.mark.asyncio
async def test_tts_endpoint_requires_configuration(fake_backend, fake_zones_http, tmp_path):
    app = create_app(_settings(fake_backend, fake_zones_http, tmp_path))

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/admin/content/rounds/r1/tts")
            assert resp.status_code == 503
            assert "ELEVENLABS_API_KEY" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_tts_endpoint_generates_and_wires_audio(
    fake_backend, fake_zones_http, tmp_path, monkeypatch
):
    import dataclasses

    import server.tts

    calls = {}

    async def fake_synthesize(text, *, api_key, voice_id, model_id, client=None):
        calls.update(text=text, api_key=api_key, voice_id=voice_id, model_id=model_id)
        return b"ID3fake-mp3"

    monkeypatch.setattr(server.tts, "synthesize", fake_synthesize)

    settings = dataclasses.replace(
        _settings(fake_backend, fake_zones_http, tmp_path),
        elevenlabs_api_key="k-123",
    )
    app = create_app(settings)

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Key set but no voice anywhere → the operator must pick one.
            resp = await client.post("/api/admin/content/rounds/r1/tts")
            assert resp.status_code == 400

            resp = await client.post(
                "/api/admin/content/rounds/r1/tts", json={"voice_id": "voice-9"}
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["audio"] == "r1.mp3"
            assert body["reloaded"] is True
            assert calls["voice_id"] == "voice-9"
            assert calls["text"] == "Coffee or tea?"  # no text: falls back to question

            # mp3 landed in the audio dir and is served; the stored round points at it.
            assert (tmp_path / "audio" / "r1.mp3").read_bytes() == b"ID3fake-mp3"
            assert (await client.get("/audio/r1.mp3")).content == b"ID3fake-mp3"
            assert app.state.show.rounds[0].audio == "r1.mp3"
            content = (await client.get("/api/admin/content")).json()
            assert content["rounds"][0]["audio_exists"] is True

            resp = await client.post("/api/admin/content/rounds/ghost/tts")
            assert resp.status_code == 404


@pytest.mark.asyncio
async def test_player_page_served(fake_backend, fake_zones_http, tmp_path):
    app = create_app(_settings(fake_backend, fake_zones_http, tmp_path))

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/p/seat-1")
            assert resp.status_code == 200
            assert "KI-Blackbox".encode() in resp.content


@pytest.mark.asyncio
async def test_audio_files_served_from_audio_dir(fake_backend, fake_zones_http, tmp_path):
    settings = _settings(fake_backend, fake_zones_http, tmp_path)
    app = create_app(settings)
    # create_app made the dir; drop a narration mp3 in as the operator would.
    (tmp_path / "audio" / "k2_intro.mp3").write_bytes(b"ID3fake-mp3-bytes")

    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/audio/k2_intro.mp3")
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "audio/mpeg"
            assert resp.content == b"ID3fake-mp3-bytes"

            assert (await client.get("/audio/missing.mp3")).status_code == 404
