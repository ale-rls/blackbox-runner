"""ElevenLabs text-to-speech for narration/question audio.

The admin console calls POST /api/admin/content/rounds/{id}/tts, which renders
a step's ``text`` through this module and drops the mp3 into the audio dir.
Kept as a thin HTTP client (no elevenlabs SDK dependency): one endpoint, one
response shape, and the show machine must not grow ML-sized installs.
"""

from __future__ import annotations

from typing import Optional

import httpx

_API_BASE = "https://api.elevenlabs.io/v1"
# Long monologues (k2_intro is ~3k chars) take a while to render server-side.
_TIMEOUT = httpx.Timeout(180.0, connect=10.0)


class TTSError(RuntimeError):
    """ElevenLabs rejected the request or was unreachable."""


async def synthesize(
    text: str,
    *,
    api_key: str,
    voice_id: str,
    model_id: str = "eleven_multilingual_v2",
    client: Optional[httpx.AsyncClient] = None,
) -> bytes:
    """Render ``text`` to mp3 bytes. ``client`` is injectable for tests."""
    url = f"{_API_BASE}/text-to-speech/{voice_id}"
    params = {"output_format": "mp3_44100_128"}
    headers = {"xi-api-key": api_key}
    body = {"text": text, "model_id": model_id}

    own_client = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        try:
            resp = await client.post(url, params=params, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise TTSError(f"ElevenLabs unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise TTSError(f"ElevenLabs error {resp.status_code}: {_error_detail(resp)}")
        return resp.content
    finally:
        if own_client:
            await client.aclose()


def _error_detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except Exception:
        return resp.text[:300]
    # ElevenLabs nests errors as {"detail": {"status": ..., "message": ...}}.
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    return str(detail) if detail else resp.text[:300]
