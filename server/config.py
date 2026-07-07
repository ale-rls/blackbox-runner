"""Runtime configuration, read from environment variables.

Kept intentionally tiny: the show runs on one venue machine from a shell
script, not a deployment pipeline, so env vars with sane defaults are enough.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _read_dotenv(path: str) -> dict[str, str]:
    """Parse simple ``KEY=VALUE`` lines from a dotenv file. Missing file is fine."""
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    return values


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
    # PocketBase persistence backend (issue #16). Superuser credentials are
    # server-side only — never sent to browsers. Startup hard-fails if the
    # instance is unreachable (there is no degraded mode without persistence).
    pocketbase_url: str = "http://localhost:8090"
    pocketbase_admin_email: Optional[str] = None
    pocketbase_admin_password: Optional[str] = None
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
    def load(cls, env_file: str | None = ".env") -> "Settings":
        """Build Settings from defaults, then env vars.

        ``env_file`` (default ``.env`` in the current directory) holds local
        overrides that shouldn't be retyped into the shell every launch. Real
        process environment variables still win over anything in the file.
        """
        environ = dict(os.environ)
        if env_file:
            for key, value in _read_dotenv(env_file).items():
                environ.setdefault(key, value)
        return cls(
            tracking_ws_url=environ.get("TRACKING_WS_URL", cls.tracking_ws_url),
            tracking_http_url=environ.get("TRACKING_HTTP_URL", cls.tracking_http_url),
            host=environ.get("GAME_HOST", cls.host),
            port=int(environ.get("GAME_PORT", cls.port)),
            reconnect_initial_s=float(
                environ.get("TRACKING_RECONNECT_INITIAL_S", cls.reconnect_initial_s)
            ),
            reconnect_max_s=float(
                environ.get("TRACKING_RECONNECT_MAX_S", cls.reconnect_max_s)
            ),
            position_history_seconds=float(
                environ.get("POSITION_HISTORY_SECONDS", cls.position_history_seconds)
            ),
            db_path=environ.get("GAME_DB_PATH", cls.db_path),
            pocketbase_url=environ.get("POCKETBASE_URL", cls.pocketbase_url),
            pocketbase_admin_email=environ.get(
                "POCKETBASE_ADMIN_EMAIL", cls.pocketbase_admin_email
            ),
            pocketbase_admin_password=environ.get(
                "POCKETBASE_ADMIN_PASSWORD", cls.pocketbase_admin_password
            ),
            content_path=environ.get("GAME_CONTENT_PATH", cls.content_path),
            audio_dir=environ.get("GAME_AUDIO_DIR", cls.audio_dir),
            rebind_max_distance=float(
                environ.get("REBIND_MAX_DISTANCE", cls.rebind_max_distance)
            ),
            rebind_max_gap_s=float(environ.get("REBIND_MAX_GAP_S", cls.rebind_max_gap_s)),
            orphan_after_s=float(environ.get("ORPHAN_AFTER_S", cls.orphan_after_s)),
            ritual_zone_id=environ.get("RITUAL_ZONE_ID", cls.ritual_zone_id),
            elevenlabs_api_key=environ.get("ELEVENLABS_API_KEY", cls.elevenlabs_api_key),
            elevenlabs_voice_id=environ.get("ELEVENLABS_VOICE_ID", cls.elevenlabs_voice_id),
            elevenlabs_model_id=environ.get("ELEVENLABS_MODEL_ID", cls.elevenlabs_model_id),
        )
