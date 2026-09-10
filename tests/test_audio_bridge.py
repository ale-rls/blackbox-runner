from __future__ import annotations

import httpx
import pytest

from server.audio_bridge import AudioBridgeClient


@pytest.mark.asyncio
async def test_register_play_and_status_use_bridge_contract():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/status":
            return httpx.Response(200, json={"players": [], "flagged": []})
        return httpx.Response(200, json={"ok": True})

    http = httpx.AsyncClient(
        base_url="http://bridge",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer secret"},
    )
    audio = AudioBridgeClient("http://bridge", "secret", client=http)
    await audio.register("seat-a")
    await audio.set_active("seat-a", True)
    await audio.play("seat-a", "intro.mp3", "queue")
    assert await audio.status() == {"players": [], "flagged": []}
    assert [r.url.path for r in requests] == [
        "/players/seat-a/register",
        "/players/seat-a/active",
        "/players/seat-a/play",
        "/status",
    ]
    assert all(r.headers["Authorization"] == "Bearer secret" for r in requests)
    await http.aclose()


@pytest.mark.asyncio
async def test_play_many_uploads_once_then_degrades_per_player(tmp_path):
    (tmp_path / "intro.mp3").write_bytes(b"ID3audio")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(502 if request.url.path.endswith("/bad/play") else 200, json={})

    http = httpx.AsyncClient(base_url="http://bridge", transport=httpx.MockTransport(handler))
    audio = AudioBridgeClient("http://bridge", audio_dir=tmp_path, client=http)
    result = await audio.play_many(["good", "bad"], "intro.mp3")
    assert result["good"] is None
    assert result["bad"] is not None
    assert [request.url.path for request in requests].count("/audio/intro.mp3") == 1
    upload = next(request for request in requests if request.url.path == "/audio/intro.mp3")
    assert upload.method == "PUT" and upload.content == b"ID3audio"
    await http.aclose()


@pytest.mark.asyncio
async def test_missing_upload_degrades_every_target(tmp_path):
    http = httpx.AsyncClient(
        base_url="http://bridge",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    audio = AudioBridgeClient("http://bridge", audio_dir=tmp_path, client=http)
    result = await audio.play_many(["a", "b"], "missing.mp3")
    assert result["a"] and result["b"]
    await http.aclose()
