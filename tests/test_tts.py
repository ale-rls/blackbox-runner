"""tts: request shaping and error mapping for the ElevenLabs client."""

from __future__ import annotations

import httpx
import pytest

from server.tts import TTSError, synthesize


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_synthesize_posts_text_to_voice_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["api_key"] = request.headers.get("xi-api-key")
        seen["body"] = request.read()
        return httpx.Response(200, content=b"ID3fake-mp3")

    async with _client(handler) as client:
        audio = await synthesize(
            "Hallo Welt",
            api_key="k-123",
            voice_id="voice-9",
            model_id="eleven_multilingual_v2",
            client=client,
        )

    assert audio == b"ID3fake-mp3"
    assert "/v1/text-to-speech/voice-9" in seen["url"]
    assert "output_format=mp3_44100_128" in seen["url"]
    assert seen["api_key"] == "k-123"
    assert b"Hallo Welt" in seen["body"]
    assert b"eleven_multilingual_v2" in seen["body"]


@pytest.mark.asyncio
async def test_synthesize_maps_api_errors_to_ttserror():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"detail": {"status": "invalid_api_key", "message": "bad key"}}
        )

    async with _client(handler) as client:
        with pytest.raises(TTSError, match="401.*bad key"):
            await synthesize("x", api_key="nope", voice_id="v", client=client)


@pytest.mark.asyncio
async def test_synthesize_maps_transport_errors_to_ttserror():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with _client(handler) as client:
        with pytest.raises(TTSError, match="unreachable"):
            await synthesize("x", api_key="k", voice_id="v", client=client)
