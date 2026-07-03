# Quickstart

Zero to a running game server with the admin dashboard in about five
minutes. This is the developer-machine path; if you're setting up for an
actual show night, follow [`runbook.md`](runbook.md) instead.

## Prerequisites

- Python 3.11+
- A [TrackingBox](https://github.com/ale-rls/TrackingBox) checkout at the
  pinned commit (see the [README](../README.md)), installed per its own
  instructions. The mock backend is enough for everything below — no
  camera or GPU needed.

## 1. Install

From this repo's root.

macOS/Linux:

```bash
make venv
```

Windows (PowerShell — the Makefile assumes POSIX paths, so run the steps
directly):

```powershell
py -3.11 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```

## 2. Start TrackingBox

From the TrackingBox checkout, pointing it at this repo's dev config —
that config runs the mock backend (24 simulated people, no camera) with
floor calibration and the `answer_a`/`answer_b`/`ritual` zones that
`content/show.yaml` expects:

```bash
audience-tracker serve --config <path-to-this-repo>/dev/trackingbox.config.json
```

Check it's alive:

```bash
curl http://localhost:8000/health
# {"status":"ok","pipeline":true,"pipeline_running":true}
```

Already running a real TrackingBox (live camera, venue config)? That works
too — the game server doesn't care which backend is behind `/ws`, but zone
IDs in TrackingBox's config must then match `content/show.yaml` or startup
validation will fail.

## 3. Start the game server

macOS/Linux:

```bash
make dev
```

Windows:

```powershell
.venv\Scripts\python -m uvicorn server.app:app --reload --port 8100
```

On startup it connects to `ws://localhost:8000/ws`, loads
`content/show.yaml`, and validates the content's zone IDs against
TrackingBox's `/api/zones` (a typo fails fast here). Check:

```bash
curl http://localhost:8100/health
# {"status":"ok","tracking_connected":true,"tracking_ws_url":"ws://localhost:8000/ws"}
```

`tracking_connected: false` means TrackingBox isn't reachable — the server
keeps retrying with backoff, so just start/fix TrackingBox and re-check.

## 4. Poke at it

- **Admin dashboard:** <http://localhost:8100/admin/> — binding board,
  floor map (mock people wander as unbound GID dots), round control,
  scoreboard.
- **Player page:** <http://localhost:8100/p/demo1> — any player ID works;
  players are created on first claim. Enter a GID you can see on the admin
  floor map and tap Claim.
- **Run a round:** in the admin dashboard, hit **Start next round**. Bound
  players answer by their GID being inside `answer_a`/`answer_b` when the
  round closes.
- **Raw state:** `curl http://localhost:8100/api/tracking/audience` dumps
  the mirrored TrackingBox view; useful when the map looks wrong.

Heads-up: the mock simulator churns GIDs aggressively (people vanish and
respawn sub-second), so a manual claim can drop to `lost` quickly. That's
expected — it's the stress test the auto-rebind logic exists for. See the
README's Development notes.

## 5. Run the tests

macOS/Linux: `make test` &nbsp;·&nbsp; Windows: `.venv\Scripts\python -m pytest -q`

No TrackingBox needed — the suite runs against built-in fakes.

## Common tweaks

All configuration is environment variables with sane defaults
(`server/config.py`):

| Variable | Default | What it does |
| --- | --- | --- |
| `TRACKING_WS_URL` | `ws://localhost:8000/ws` | TrackingBox WebSocket |
| `TRACKING_HTTP_URL` | `http://localhost:8000` | TrackingBox REST (zones) |
| `GAME_HOST` / `GAME_PORT` | `0.0.0.0` / `8100` | Game server bind |
| `GAME_DB_PATH` | `data/blackbox-runner.db` | SQLite state (crash recovery) |
| `GAME_CONTENT_PATH` | `content/show.yaml` | Show content |
| `RITUAL_ZONE_ID` | unset (disabled) | Enable the ritual rebind flow, e.g. `ritual` |

Example — enable ritual rebind for a dev session:

```bash
RITUAL_ZONE_ID=ritual make dev
```

```powershell
$env:RITUAL_ZONE_ID = 'ritual'
.venv\Scripts\python -m uvicorn server.app:app --reload --port 8100
```

## Where next

- [`architecture.md`](architecture.md) — full design: binding state
  machine, rounds, persistence, the pinned TrackingBox WS contract.
- [`touchdesigner.md`](touchdesigner.md) — hooking up TD visuals to `/ws/td`.
- [`runbook.md`](runbook.md) — show-night ops: start order, failure modes,
  crash recovery, content freeze.
