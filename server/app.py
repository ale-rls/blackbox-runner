"""FastAPI entry point for the theater game server.

Phase 0 connected to TrackingBox and mirrored its audience state. Phase 1
added the player<->GID binding layer: claim flow, persisted state with crash
recovery, and a minimal admin binding board. Phase 2 added the round/scoring
engine and the player WebSocket. Phase 3 added the TouchDesigner round/cue
WS. Phase 4 adds auto-rebind, the ritual rebind flow, and round-state crash
recovery on top of Phase 1's binding-state recovery.
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

from .bindings import BindingError, BindingManager, PlayerState
from .config import Settings
from .content import ContentError, ShowContent, load_show
from .engine import EngineError, GameEngine
from .models import ZoneMap
from .persistence import Database
from .tracking_client import TrackingClient, fetch_zones

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

log = logging.getLogger("blackbox_runner.app")

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


async def _watch_for_ritual_prompts(
    bindings: BindingManager, engine: GameEngine, ritual_zone_id: Optional[str]
) -> None:
    """Bridges bindings.py's orphan transitions to the TD/player cue channel.

    Kept out of bindings.py to avoid coupling the binding state machine to
    the round engine's pub/sub — this is the one place both are in scope.
    """
    if ritual_zone_id is None:
        return
    queue = bindings.subscribe()
    try:
        while True:
            player = await queue.get()
            if player.state == PlayerState.ORPHANED:
                engine.publish_cue(
                    "ritual_prompt", {"player_id": player.id, "corner_zone": ritual_zone_id}
                )
    finally:
        bindings.unsubscribe(queue)


class ClaimRequest(BaseModel):
    gid: int
    display_name: Optional[str] = None


class RebindRequest(BaseModel):
    gid: int
    actor: str = "operator"


class CueRequest(BaseModel):
    payload: dict = {}


_EMPTY_SHOW = ShowContent(rounds=[])


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
        bindings = await BindingManager.load(
            db,
            session_id,
            tracking,
            rebind_max_distance=settings.rebind_max_distance,
            rebind_max_gap_s=settings.rebind_max_gap_s,
            orphan_after_s=settings.orphan_after_s,
            ritual_zone_id=settings.ritual_zone_id,
        )
        app.state.bindings = bindings
        app.state.session_id = session_id

        try:
            show = await asyncio.to_thread(
                load_show, settings.content_path, valid_zone_ids=app.state.zones.zone_ids()
            )
            log.info("Loaded show content: %d round(s) from %s", len(show.rounds), settings.content_path)
        except (ContentError, FileNotFoundError) as exc:
            log.warning("Could not load show content (%s); round control disabled", exc)
            show = _EMPTY_SHOW
        app.state.show = show
        app.state.engine = await GameEngine.load(db, session_id, show, bindings, tracking)

        bindings_task = asyncio.create_task(bindings.run())
        log_task = asyncio.create_task(_log_positions_periodically(tracking))
        ritual_task = asyncio.create_task(
            _watch_for_ritual_prompts(bindings, app.state.engine, settings.ritual_zone_id)
        )
        try:
            yield
        finally:
            tracking.stop()
            app.state.engine.shutdown()
            bindings.shutdown()
            for task in (tracking_task, bindings_task, log_task, ritual_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await asyncio.to_thread(db.close)

    app = FastAPI(title="Blackbox Runner", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.tracking = tracking
    app.state.db = db
    app.state.zones = ZoneMap(enabled=False, default_zone=None, zones=[])
    app.state.bindings = None
    app.state.session_id = None
    app.state.show = _EMPTY_SHOW
    app.state.engine = None

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
    # Rounds & scoring
    # -------------------------------------------------------------- #
    @app.get("/api/rounds/current")
    async def current_round() -> Optional[dict]:
        engine: GameEngine = app.state.engine
        rt = engine.current if engine else None
        return engine.round_payload(rt) if rt else None

    @app.get("/api/scores")
    async def scores() -> dict:
        engine: GameEngine = app.state.engine
        return await engine.scores() if engine else {}

    @app.post("/api/admin/content/reload")
    async def reload_content() -> dict:
        """Hot-reload content/show.yaml between rounds (docs/runbook.md's
        content freeze process covers when this is and isn't safe to use).
        """
        try:
            show = await asyncio.to_thread(
                load_show, settings.content_path, valid_zone_ids=app.state.zones.zone_ids()
            )
        except ContentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            app.state.engine.reload_show(show)
        except EngineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        app.state.show = show
        return {"ok": True, "rounds": len(show.rounds)}

    @app.post("/api/admin/rounds/start")
    async def start_round() -> dict:
        try:
            rt = await app.state.engine.start_next_round()
        except EngineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return app.state.engine.round_payload(rt)

    @app.post("/api/admin/rounds/close")
    async def close_round() -> dict:
        try:
            rt = await app.state.engine.close_round()
        except EngineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return app.state.engine.round_payload(rt)

    @app.post("/api/admin/rounds/reveal")
    async def reveal_round() -> dict:
        try:
            rt = await app.state.engine.reveal_round()
        except EngineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return app.state.engine.round_payload(rt)

    @app.websocket("/ws/player/{player_id}")
    async def ws_player(websocket: WebSocket, player_id: str) -> None:
        await websocket.accept()
        engine: GameEngine = app.state.engine
        queue = engine.subscribe()
        try:
            rt = engine.current
            await websocket.send_json(
                {
                    "type": "hello",
                    "round": engine.round_payload(rt) if rt else None,
                    "scores": await engine.scores(),
                }
            )
            while True:
                event = await queue.get()
                payload = dict(event.payload)
                if event.type == "reveal":
                    answer = engine.player_answer(player_id)
                    payload["your_answer"] = {"zone": answer[0], "resolved": answer[1]} if answer else None
                elif event.type == "scores_updated":
                    payload["your_score"] = payload["scores"].get(player_id, 0)
                await websocket.send_json({"type": event.type, **payload})
        except WebSocketDisconnect:
            pass
        finally:
            engine.unsubscribe(queue)

    # -------------------------------------------------------------- #
    # TouchDesigner — round/cue WS (docs/touchdesigner.md)
    # -------------------------------------------------------------- #
    @app.post("/api/admin/cues/{cue_type}")
    async def fire_cue(cue_type: str, body: Optional[CueRequest] = None) -> dict:
        """Manually fire a named cue to every /ws/td (and /ws/player) listener.
        Used for tech rehearsal and for cues that don't yet have an automatic
        trigger — e.g. 'ritual_prompt' ahead of Phase 4's real ritual flow.
        """
        payload = body.payload if body else {}
        app.state.engine.publish_cue(cue_type, payload)
        return {"ok": True, "cue": cue_type, "payload": payload}

    @app.websocket("/ws/td")
    async def ws_td(websocket: WebSocket) -> None:
        await websocket.accept()
        engine: GameEngine = app.state.engine
        queue = engine.subscribe()
        try:
            rt = engine.current
            await websocket.send_json(
                {
                    "type": "hello",
                    "round": engine.round_payload(rt) if rt else None,
                    "zone_counts": engine.current_zone_counts(),
                    "zones": app.state.zones.model_dump(),
                }
            )
            while True:
                event = await queue.get()
                await websocket.send_json({"type": event.type, **event.payload})
        except WebSocketDisconnect:
            pass
        finally:
            engine.unsubscribe(queue)

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
