---
name: verify
description: Run and drive blackbox-runner locally (fake TrackingBox + uvicorn + Playwright) to verify changes end-to-end.
---

# Verifying blackbox-runner

The venv must be Python 3.11+ (`/opt/homebrew/bin/python3.13 -m venv .venv`,
then `.venv/bin/pip install -e ".[dev]"`). `make test` rebuilds the venv with
`python3`, which may be too old — prefer `.venv/bin/python -m pytest -q`.

## Launch

The server needs a TrackingBox (WS + `GET /api/zones`) at startup or the show
loads empty ("no more rounds"). No real TrackingBox needed — run a fake:

1. Fake TrackingBox: serve the zone ids from `dev/trackingbox.config.json` on
   `http://:8000/api/zones` and a WS on `:8001` that sends one snapshot with
   people carrying `floor: [x, y]` + `floor_valid: true` (see
   tests/conftest.py for message shapes). Question rounds resolve answers
   from floor positions through the round's `zone_layout` (server/zones.py) —
   the person's `zone` field only matters for the ritual corner and
   "choice"-form rounds.
2. Server (start it *after* the fake is listening, or restart it):
   `TRACKING_WS_URL=ws://localhost:8001/ws TRACKING_HTTP_URL=http://localhost:8000 GAME_DB_PATH=/tmp/verify.db .venv/bin/uvicorn server.app:app --port 8100`

## Drive

- Claim: `POST /api/players/seat-1/claim {"gid": 1}` (gid must be in the fake's
  snapshot). Player events: `ws://localhost:8100/ws/player/seat-1`.
- Rounds: `POST /api/admin/rounds/{start,close,reveal}`. Narration steps
  (`duration_s: 0`) are finished with `reveal` directly.
- Player page: Playwright chromium (installed in the venv), viewport ~390×780.
  The claim tap unlocks audio; `page.reload()` mid-step must show the
  `#audio-unlock` overlay. Check the `#narration` element's
  `currentSrc`/`paused` to prove playback.
- Answers: `sqlite3 $GAME_DB_PATH 'select * from answers'` — question rounds
  record zone + position, narration rounds record nothing.

## Gotchas

- WS payloads merge as `{"type": event.type, **payload}` — payload keys must
  never be named `type` (the round payload uses `round_type`).
- Player/gid bindings persist in the DB — delete `$GAME_DB_PATH*` between
  browser runs or claims will already be bound.
- uvicorn doesn't hot-reload; restart after server code edits (static
  web/player, web/admin files are re-read per request).
