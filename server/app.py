"""FastAPI entry point for the theater game server.

Phase 0 connected to TrackingBox and mirrored its audience state. Phase 1
adds the player<->GID binding layer: claim flow, persisted state with crash
recovery, and a minimal admin binding board. Round/scoring logic and the
player/TD WebSockets described in the full design land in later phases.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .bindings import BindingError, BindingManager
from .config import Settings
from .models import ZoneMap
from .persistence import Database
from .tracking_client import TrackingClient, fetch_zones

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

log = logging.getLogger("theater_game.app")

_POSITION_LOG_INTERVAL_S = 5.0
_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


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


class ClaimRequest(BaseModel):
    gid: int
    display_name: Optional[str] = None


class RebindRequest(BaseModel):
    gid: int
    actor: str = "operator"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    tracking = TrackingClient(
        settings.tracking_ws_url,
        reconnect_initial_s=settings.reconnect_initial_s,
        reconnect_max_s=settings.reconnect_max_s,
        history_seconds=settings.position_history_seconds,
    )
    db = Database(settings.db_path)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        tracking_task = asyncio.create_task(tracking.run())
        try:
            await tracking.wait_connected(timeout=10)
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("TrackingBox not reachable at startup; will keep retrying in background")
        try:
            app.state.zones = await fetch_zones(settings.tracking_http_url)
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning("Could not fetch zones from TrackingBox: %s", exc)
            app.state.zones = ZoneMap(enabled=False, default_zone=None, zones=[])

        session_id = await asyncio.to_thread(db.get_active_session_id)
        if session_id is None:
            session_id = await asyncio.to_thread(db.create_session)
            log.info("Started new session %d", session_id)
        else:
            log.info("Resuming session %d (crash recovery)", session_id)
        bindings = await BindingManager.load(db, session_id, tracking)
        app.state.bindings = bindings
        app.state.session_id = session_id

        bindings_task = asyncio.create_task(bindings.run())
        log_task = asyncio.create_task(_log_positions_periodically(tracking))
        try:
            yield
        finally:
            tracking.stop()
            for task in (tracking_task, bindings_task, log_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await asyncio.to_thread(db.close)

    app = FastAPI(title="Theater Game Server", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.tracking = tracking
    app.state.db = db
    app.state.zones = ZoneMap(enabled=False, default_zone=None, zones=[])
    app.state.bindings = None
    app.state.session_id = None

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

    # -------------------------------------------------------------- #
    # Players — claim flow
    # -------------------------------------------------------------- #
    @app.get("/api/players")
    async def list_players() -> list[dict]:
        return [p.to_dict() for p in app.state.bindings.all_players()]

    @app.get("/api/players/{player_id}")
    async def get_player(player_id: str) -> dict:
        player = app.state.bindings.get(player_id)
        if player is None:
            raise HTTPException(status_code=404, detail=f"player {player_id!r} not found")
        return player.to_dict()

    @app.post("/api/players/{player_id}/claim")
    async def claim_player(player_id: str, body: ClaimRequest) -> dict:
        try:
            player = await app.state.bindings.claim(player_id, body.gid, body.display_name)
        except BindingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return player.to_dict()

    # -------------------------------------------------------------- #
    # Admin — binding board
    # -------------------------------------------------------------- #
    @app.post("/api/admin/players/{player_id}/rebind")
    async def rebind_player(player_id: str, body: RebindRequest) -> dict:
        try:
            player = await app.state.bindings.operator_rebind(player_id, body.gid, actor=body.actor)
        except BindingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return player.to_dict()

    @app.websocket("/ws/admin")
    async def ws_admin(websocket: WebSocket) -> None:
        await websocket.accept()
        bindings: BindingManager = app.state.bindings
        queue = bindings.subscribe()
        try:
            await websocket.send_json(
                {"type": "roster", "players": [p.to_dict() for p in bindings.all_players()]}
            )
            while True:
                player = await queue.get()
                await websocket.send_json({"type": "player_update", "player": player.to_dict()})
        except WebSocketDisconnect:
            pass
        finally:
            bindings.unsubscribe(queue)

    # -------------------------------------------------------------- #
    # Web: player claim page + admin dashboard
    # -------------------------------------------------------------- #
    @app.get("/p/{player_id}")
    async def player_page(player_id: str) -> FileResponse:
        return FileResponse(_WEB_DIR / "player" / "index.html")

    if (_WEB_DIR / "admin").is_dir():
        app.mount("/admin", StaticFiles(directory=_WEB_DIR / "admin", html=True), name="admin")

    return app


app = create_app()
