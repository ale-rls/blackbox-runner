# blackbox-runner

Interactive theater game server for 40+ simultaneous players. Positions come
from [TrackingBox](https://github.com/ale-rls/TrackingBox) (anonymous GIDs
over REST + WebSocket); players answer questions by physically moving into
floor zones.

TrackingBox is treated as a versioned, read-only sensor: this repo consumes
its `/ws` and `/api/zones` endpoints and never modifies it for game features.

**Pinned TrackingBox commit:** `95d092864ff8e24a2af9c33e662e547262692b04`
(update this line, and re-validate the show, whenever the pin moves).

## Architecture

```
                    ┌──────────────────────┐
   camera ─────────▶│  TrackingBox (as-is) │───── /ws positions ────┐
                    │  GIDs, zones, /ws    │                        │
                    └──────────┬───────────┘                        │
                               │ raw positions (direct, low-latency)│
                               ▼                                    ▼
                    ┌──────────────────┐  round/cue WS   ┌────────────────────┐
                    │  TouchDesigner   │◀────────────────│  Game server (new) │
                    │  screen visuals  │                 │  bindings, rounds, │
                    └──────────────────┘                 │  scoring, SQLite   │
                                                         └───┬──────────┬─────┘
                                                             │          │
                                                     player WS      admin WS/REST
                                                             │          │
                                                     ┌───────▼───┐ ┌────▼──────┐
                                                     │ 40+ phone │ │ operator  │
                                                     │ web pages │ │ dashboard │
                                                     └───────────┘ └───────────┘
```

See [`docs/architecture.md`](docs/architecture.md) for the full design plan
(phases, data model, binding/rebind logic, testing strategy),
[`docs/touchdesigner.md`](docs/touchdesigner.md) for the TD cue vocabulary,
**[`docs/runbook.md`](docs/runbook.md) if you're running the actual
show** — start order, health checks, what to do when something dies, and
the content freeze process — and
[`docs/deployment.md`](docs/deployment.md) for running this as a container
on Coolify while TrackingBox stays on the venue machine.

## Repo layout

```
server/
  app.py               # FastAPI entry: player WS, TD WS, admin API
  tracking_client.py   # WS client to TrackingBox: reconnect, snapshot resync
  bindings.py          # player<->GID state machine
  engine.py            # round state machine, timers, zone evaluation, scoring
  persistence.py       # SQLite (WAL), write-through, crash recovery
  replay.py            # post-show timeline / state-at-a-moment reconstruction
  models.py            # pydantic models incl. copied TrackingBox message shapes
  content.py           # validates rounds & questions (pydantic models)
  content_db.py        # loads the show from the DB (runtime source of truth)
web/
  player/              # phone page (one per player ID)
  admin/               # operator dashboard: round control, binding board,
                        # floor map, TD cue log, scoreboard
content/
  show.yaml            # AUTHORING source: narration texts, questions, forms,
                       # zone mapping. Edit here, then run
                       # scripts/import_content.py to apply — the server
                       # plays from the DB, not this file.
  audio/               # voice-over mp3s, served at /audio/<name>.mp3
                       # (override the folder with GAME_AUDIO_DIR; generate
                       # via ElevenLabs from the admin console — see runbook)
scripts/
  validate_content.py  # check show.yaml against a running TrackingBox's zones
  import_content.py    # push show.yaml into the game DB (the live content)
  replay.py            # CLI for server/replay.py
td_scripts/
  td_receive_cues.py   # TouchDesigner WebSocket DAT callback for /ws/td
tests/
  scenarios/           # scripted GID-churn scenarios against the mock backend
docs/
  architecture.md      # the full design plan
  touchdesigner.md     # TD cue vocabulary + setup
  runbook.md           # ops runbook: start order, failure modes, replay
  deployment.md        # Coolify + Tailscale deployment (TrackingBox on-prem)
Dockerfile             # blackbox-runner image (not TrackingBox -- see deployment.md)
docker-entrypoint.sh   # import_content.py, then serve; see deployment.md
```

## Development

Requires Python 3.11+ and a running TrackingBox instance (mock backend is
fine for all development and load testing):

```bash
# in a TrackingBox checkout at the pinned commit — bare mock, no zones/floor
audience-tracker serve --backend mock --port 8000

# or, to exercise zone-based gameplay (answers require floor projection):
audience-tracker serve --config /path/to/this/repo/dev/trackingbox.config.json --port 8000

# in this repo — import the show content once, then boot
make import-content
make dev
```

`dev/trackingbox.config.json` enables calibration (an identity mapping, since
the mock backend has no real camera) and defines the floor-marking zones
(`scale_*`, `cross_*`, `ring_*`, plus the `ritual` corner) matching
`content/show.yaml`. Without it, `floor_valid` stays false and no round can
ever be answered — only useful for Phase 0/1-style connectivity checks.

To enable ElevenLabs voice generation from the admin console, set
`ELEVENLABS_API_KEY` (and usually `ELEVENLABS_VOICE_ID`; model defaults to
`eleven_multilingual_v2`) — see `docs/runbook.md` for the workflow.

`make dev` boots the game server against `ws://localhost:8000/ws`, loading
the show from the game DB (validated against TrackingBox's `/api/zones` at
startup — a content zone typo fails fast here rather than silently going
unanswerable mid-show) and the admin dashboard at `/admin/`. The DB is
seeded from the authoring copy with `make import-content` (or
`python scripts/import_content.py`); re-run it after every `show.yaml`
edit you want the server to pick up.

Run tests with `make test`.

Note: TrackingBox's default mock simulator churns GIDs quite aggressively
(people can disappear and respawn sub-second). That's a good stress test —
it's exactly what Phase 4's auto-rebind exists for — but makes manual
single-player demos flaky if you're just claiming once and watching; favor
scripted checks over live clicking when verifying binding behavior by hand.

### Ritual rebind (Phase 4)

Set `RITUAL_ZONE_ID` to enable the ritual rebind flow (an orphaned player —
lost with no confident auto-rebind after `ORPHAN_AFTER_S` seconds, default
3 — gets prompted to a lit corner; whichever unbound GID enters that zone
resolves them). `dev/trackingbox.config.json` defines a `ritual` zone in
the top-left corner for local testing:

```bash
RITUAL_ZONE_ID=ritual make dev
```

Other tunables (see `server/config.py`): `REBIND_MAX_DISTANCE` (normalized
floor units, default 0.15), `REBIND_MAX_GAP_S` (default 8.0). These are
exactly the thresholds §4.2/§7 of the design doc expect to be retuned from
rehearsal telemetry — there's no "correct" value without a real venue.
