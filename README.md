# theater-game

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
(phases, data model, binding/rebind logic, testing strategy).

## Repo layout

```
server/
  app.py               # FastAPI entry: player WS, TD WS, admin API
  tracking_client.py   # WS client to TrackingBox: reconnect, snapshot resync
  bindings.py          # player<->GID state machine
  engine.py            # round state machine, timers, zone evaluation, scoring
  persistence.py       # SQLite (WAL), write-through, crash recovery
  models.py            # pydantic models incl. copied TrackingBox message shapes
  content.py           # loads/validates rounds & questions from content/
web/
  player/              # phone page (one per player ID)
  admin/               # operator dashboard
content/
  show.yaml            # rounds, questions, zone->answer mapping, timing
tests/
  scenarios/           # scripted GID-churn scenarios against the mock backend
```

## Development

Requires Python 3.11+ and a running TrackingBox instance (mock backend is
fine for all development and load testing):

```bash
# in a TrackingBox checkout at the pinned commit
audience-tracker serve --backend mock --port 8000

# in this repo
make dev
```

`make dev` boots the game server against `ws://localhost:8000/ws`.

Run tests with `make test`.
