"""Runtime configuration, read from environment variables.

Kept intentionally tiny: the show runs on one venue machine from a shell
script, not a deployment pipeline, so env vars with sane defaults are enough.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Settings:
    tracking_ws_url: str = "ws://localhost:8000/ws"
    tracking_http_url: str = "http://localhost:8000"
    host: str = "0.0.0.0"
    port: int = 8100
    reconnect_initial_s: float = 0.5
    reconnect_max_s: float = 30.0
    position_history_seconds: float = 5.0
    db_path: str = "data/blackbox-runner.db"
    content_path: str = "content/show.yaml"
    audio_dir: str = "content/audio"
    rebind_max_distance: float = 0.15
    rebind_max_gap_s: float = 8.0
    orphan_after_s: float = 3.0
    ritual_zone_id: Optional[str] = None
    # ElevenLabs TTS for narration/question audio. Generation stays disabled
    # (503 from the admin endpoint) until an API key is configured.
    elevenlabs_api_key: Optional[str] = None
    elevenlabs_voice_id: Optional[str] = None
    elevenlabs_model_id: str = "eleven_multilingual_v2"

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            tracking_ws_url=os.environ.get("TRACKING_WS_URL", cls.tracking_ws_url),
            tracking_http_url=os.environ.get("TRACKING_HTTP_URL", cls.tracking_http_url),
            host=os.environ.get("GAME_HOST", cls.host),
            port=int(os.environ.get("GAME_PORT", cls.port)),
            reconnect_initial_s=float(
                os.environ.get("TRACKING_RECONNECT_INITIAL_S", cls.reconnect_initial_s)
            ),
            reconnect_max_s=float(
                os.environ.get("TRACKING_RECONNECT_MAX_S", cls.reconnect_max_s)
            ),
            position_history_seconds=float(
                os.environ.get("POSITION_HISTORY_SECONDS", cls.position_history_seconds)
            ),
            db_path=os.environ.get("GAME_DB_PATH", cls.db_path),
            content_path=os.environ.get("GAME_CONTENT_PATH", cls.content_path),
            audio_dir=os.environ.get("GAME_AUDIO_DIR", cls.audio_dir),
            rebind_max_distance=float(
                os.environ.get("REBIND_MAX_DISTANCE", cls.rebind_max_distance)
            ),
            rebind_max_gap_s=float(os.environ.get("REBIND_MAX_GAP_S", cls.rebind_max_gap_s)),
            orphan_after_s=float(os.environ.get("ORPHAN_AFTER_S", cls.orphan_after_s)),
            ritual_zone_id=os.environ.get("RITUAL_ZONE_ID", cls.ritual_zone_id),
            elevenlabs_api_key=os.environ.get("ELEVENLABS_API_KEY", cls.elevenlabs_api_key),
            elevenlabs_voice_id=os.environ.get("ELEVENLABS_VOICE_ID", cls.elevenlabs_voice_id),
            elevenlabs_model_id=os.environ.get("ELEVENLABS_MODEL_ID", cls.elevenlabs_model_id),
        )
