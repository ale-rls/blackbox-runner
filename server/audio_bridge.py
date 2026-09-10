"""Client for the personal Icecast/Liquidsoap delivery bridge.

The game remains playable when this optional service is down: ``play_many``
turns upload/control failures into per-player errors instead of raising into
the round engine, and the phone keeps its existing cue-file fallback path.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Iterable, Literal, Optional
from urllib.parse import quote

import httpx

log = logging.getLogger("blackbox_runner.audio_bridge")


class AudioBridgeClient:
    def __init__(
        self,
        url: str,
        token: str = "",
        audio_dir: str | Path = "content/audio",
        *,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._own_client = client is None
        self._audio_dir = Path(audio_dir)
        self._http = client or httpx.AsyncClient(
            base_url=url.rstrip("/"), timeout=10.0, headers=headers
        )

    async def close(self) -> None:
        if self._own_client:
            await self._http.aclose()

    async def health(self) -> dict:
        try:
            response = await self._http.get("/health")
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    async def status(self) -> dict:
        response = await self._http.get("/status")
        response.raise_for_status()
        return response.json()

    async def upload(self, file: str) -> dict:
        if not file or Path(file).name != file or Path(file).suffix.lower() != ".mp3":
            raise ValueError("audio file must be a bare .mp3 filename")
        path = self._audio_dir / file
        try:
            payload = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise FileNotFoundError(f"personal audio source is unavailable: {path}") from exc
        response = await self._http.put(
            f"/audio/{quote(file, safe='')}",
            content=payload,
            headers={"Content-Type": "audio/mpeg"},
        )
        response.raise_for_status()
        return response.json()

    async def register(self, player_id: str) -> dict:
        player = quote(player_id, safe="")
        response = await self._http.post(f"/players/{player}/register")
        response.raise_for_status()
        return response.json()

    async def set_active(self, player_id: str, active: bool) -> dict:
        player = quote(player_id, safe="")
        response = await self._http.put(
            f"/players/{player}/active", json={"active": active}
        )
        response.raise_for_status()
        return response.json()

    async def play(
        self,
        player_id: str,
        file: str,
        mode: Literal["interrupt", "queue"] = "interrupt",
    ) -> dict:
        player = quote(player_id, safe="")
        response = await self._http.post(
            f"/players/{player}/play", json={"file": file, "mode": mode}
        )
        response.raise_for_status()
        return response.json()

    async def play_many(
        self,
        player_ids: Iterable[str],
        file: str,
        mode: Literal["interrupt", "queue"] = "interrupt",
    ) -> dict[str, Optional[str]]:
        """Push one file concurrently; values are ``None`` or error text."""
        ids = list(dict.fromkeys(player_ids))

        if not ids:
            return {}
        try:
            await self.upload(file)
        except Exception as exc:
            log.warning("Personal audio upload failed for %s: %s", file, exc)
            return {player_id: str(exc) for player_id in ids}

        async def one(player_id: str) -> tuple[str, Optional[str]]:
            try:
                await self.play(player_id, file, mode)
                return player_id, None
            except httpx.HTTPError as exc:
                log.warning("Personal audio push failed for %s: %s", player_id, exc)
                return player_id, str(exc)

        return dict(await asyncio.gather(*(one(player_id) for player_id in ids)))
