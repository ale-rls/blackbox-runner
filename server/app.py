"""FastAPI entry point for the theater game server.

Phase 0 scope: connect to TrackingBox, mirror its audience state, and expose
enough surface to prove it end-to-end. Player/admin/TD WebSockets and game
logic land in later phases.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import FastAPI

from .config import Settings
from .models import ZoneMap
from .tracking_client import TrackingClient, fetch_zones

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

log = logging.getLogger("theater_game.app")

_POSITION_LOG_INTERVAL_S = 5.0


async def _log_positions_periodically(client: TrackingClient) -> None:
    """Exit criterion for Phase 0: prove live positions are flowing."""
    while True:
        await asyncio.sleep(_POSITION_LOG_INTERVAL_S)
        people = client.get_all()
        visible = sum(1 for p in people.values() if p.visible)
        log.info("tracking: %d active GID(s)%s", visible, _sample(people))


def _sample(people: dict) -> str:
    if not people:
        return ""
    gid, state = next(iter(people.items()))
    return f" (e.g. gid={gid} floor={state.floor} zone={state.zone})"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    tracking = TrackingClient(
        settings.tracking_ws_url,
        reconnect_initial_s=settings.reconnect_initial_s,
        reconnect_max_s=settings.reconnect_max_s,
        history_seconds=settings.position_history_seconds,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        tracking_task = asyncio.create_task(tracking.run())
        log_task: asyncio.Task | None = None
        try:
            await tracking.wait_connected(timeout=10)
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("TrackingBox not reachable at startup; will keep retrying in background")
        try:
            app.state.zones = await fetch_zones(settings.tracking_http_url)
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning("Could not fetch zones from TrackingBox: %s", exc)
            app.state.zones = ZoneMap(enabled=False, default_zone=None, zones=[])
        log_task = asyncio.create_task(_log_positions_periodically(tracking))
        try:
            yield
        finally:
            tracking.stop()
            for task in (tracking_task, log_task):
                if task is None:
                    continue
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="Theater Game Server", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.tracking = tracking
    app.state.zones = ZoneMap(enabled=False, default_zone=None, zones=[])

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "tracking_connected": tracking.connected,
            "tracking_ws_url": settings.tracking_ws_url,
        }

    @app.get("/api/tracking/audience")
    async def debug_audience() -> list[dict]:
        """Debug passthrough of the mirrored TrackingBox state."""
        return [s.model_dump() for s in tracking.get_all().values()]

    @app.get("/api/tracking/zones")
    async def debug_zones() -> dict:
        return app.state.zones.model_dump()

    return app


app = create_app()
