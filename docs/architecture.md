# Blackbox Runner — Design Plan

Interactive theater piece for 40+ simultaneous players. Positions come from
the existing TrackingBox server (anonymous GIDs over REST + WebSocket).
Players answer questions by physically moving into floor zones. Competitive
scoring. Everything runs on one venue machine. TouchDesigner renders visuals;
each player has a phone open on a personal web page.

**Decision already made.** The game lives in a separate repo, as a separate
FastAPI service that consumes TrackingBox over its WebSocket. TrackingBox is
treated as a versioned, read-only sensor — we pin the commit we run against
and never modify it for game features.

## 1. Architecture

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

* TouchDesigner keeps reading raw per-frame positions directly from
  TrackingBox (`/ws`) for smooth visuals, and gets game state (round, cues,
  reveal moments) from the game server.
* Phones and the admin dashboard talk only to the game server.
* The game server subscribes to TrackingBox `/ws` (snapshot on connect, then
  change events + heartbeat snapshots) and to `/api/zones` for the zone map.

### TrackingBox WS contract (as pinned)

The `/ws` endpoint sends one of two shapes, undiscriminated except by the
presence of `type`:

* `{"type": "snapshot", "data": {...}}` — full snapshot, sent once on
  connect and again on every heartbeat (`ws_heartbeat_interval_s`, default
  10s) if no change events arrived in that window.
* A bare per-GID change event with no `type` key:
  `{"gid": int, "visible": bool, "center": [x,y]|null, "bbox": [...]|null,
  "floor": [x,y]|null, "floor_valid": bool, "zone": str|null}`. A GID
  dropping out of the snapshot entirely is emitted as `visible: false`.

`floor` is calibrated floor-space `[x, y]`, used for zone lookups. `zone` is
already resolved server-side by TrackingBox's `ZoneMap` (first enabled zone
containing the point wins; falls back to `default_zone` if configured). The
game server still needs `/api/zones` for the zone map so answer options in
`content/show.yaml` can validate zone IDs and the admin dashboard can render
the floor.

## 2. Repo layout

```
server/
  app.py               # FastAPI entry: player WS, TD WS, admin API
  tracking_client.py   # WS client to TrackingBox: reconnect, snapshot resync
  bindings.py          # player<->GID state machine (core of the show's reliability)
  engine.py            # round state machine, timers, zone evaluation, scoring
  persistence.py       # SQLite (WAL), write-through, crash recovery
  models.py            # pydantic models incl. copied TrackingBox message shapes
  content.py           # loads/validates rounds & questions from content/
web/
  player/              # phone page (one per player ID)
  admin/                # operator dashboard
content/
  show.yaml            # rounds, questions, zone->answer mapping, timing
tests/
  scenarios/           # scripted GID-churn scenarios against the mock backend
Makefile               # `make dev` boots TrackingBox (mock) + game server + web
```

Contract pinning: `models.py` mirrors TrackingBox's WS/REST payloads
(`snapshot`, member fields, zone dict). The README records the TrackingBox
commit hash the show is validated against.

## 3. Data model (SQLite, WAL mode)

Live game state is held in memory (same pattern as TrackingBox's
`InMemoryStateStore`) and written through to SQLite on every meaningful
transition, so a crashed game server reloads mid-show exactly where it died.

* `sessions(id, started_at, content_version, status)` — one row per show run.
* `players(id, session_id, gid, display_name, state, last_seen_x, last_seen_y, last_seen_at)`
  — `gid` is mutable; `state ∈ {unclaimed, bound, lost, orphaned, left}`.
* `binding_events(id, player_id, old_gid, new_gid, reason, actor, at)` — full
  audit trail: `reason ∈ {claim, auto_rebind, ritual, operator, gc}`. With
  competitive scoring, every rebind must be explainable after the show.
* `rounds(id, session_id, index, question_id, state, opened_at, closed_at)`
  — `state ∈ {pending, active, closing, revealed, done}`.
* `answers(round_id, player_id, zone_id, resolved, position_x, position_y, at)`
  — `resolved ∈ {answered, absent, late_grace}`. Position snapshot stored for
  audit.
* `score_events(id, session_id, player_id, round_id, points, reason)` —
  scores are derived (SUM), never stored as a mutable counter; operator
  corrections are just more events.

Questions/rounds live in `content/show.yaml` (editable by non-programmers,
hot-reloaded between rounds), validated on load: every answer option must
reference a zone ID that exists in TrackingBox's `/api/zones`.

## 4. Core components

### 4.1 Tracking client

Async WS client with exponential-backoff reconnect. On (re)connect it
receives the full snapshot and resyncs rather than assuming continuity —
this is also the recovery path for a TrackingBox restart (all GIDs reset;
every bound player transitions to `lost` and the rebind machinery takes
over). Maintains a rolling ~5s position history per active GID for the
rebind scorer.

### 4.2 Binding manager (the reliability core)

Per-player state machine: `unclaimed -> bound -> lost -> (bound | orphaned) -> bound`.

* **Claim (onboarding):** overlay screen / TouchDesigner shows each person's
  GID; player enters it on their phone (`/p/<number>`). Game server verifies
  the GID is active and unclaimed. For 40+ people, entry is staggered.
* **Lost:** the bound GID disappears from snapshots past TrackingBox's own
  lost-buffer (i.e. the GID is truly retired or long-gone). Start a timer.
* **Auto-rebind:** candidates are newly created GIDs only (never GIDs bound
  to others). Score = spatial proximity to the player's last position ×
  time gap × uniqueness (no other lost player plausibly matches). Single
  confident candidate -> silent rebind + `binding_events` row.
* **Ambiguous -> orphaned:** never guess between two candidates. Resolution
  paths:
  1. **Ritual rebind:** push a prompt to the orphaned phone ("walk to the
     glowing corner" — TouchDesigner lights it); whichever unbound GID
     enters the ritual zone gets bound. Diegetic, invisible as an error
     state.
  2. **Operator rebind:** one tap on the dashboard floor map.
* **Density note (40+ people):** ReID mis-matches under clustering are
  expected; the rebind-veto in TrackingBox helps upstream, but the game
  layer treats every binding as provisional and auditable rather than
  assuming GID stability.

### 4.3 Game engine

* Round state machine driven by `content/show.yaml` + operator commands
  (auto-advance timers with manual override always available).
* Answer evaluation: at `closing`, take one authoritative position
  snapshot; map each bound player's floor point through the zone map (same
  first-match semantics as TrackingBox's `ZoneMap`). Players in
  `lost/orphaned` state at close are recorded `absent`, never wrong — with
  an optional short grace window: if they rebind within N seconds and their
  GID was continuously inside one zone, upgrade to `late_grace`. This
  single rule removes most pressure from the binding layer.
* Scoring rules are per-round-type in content (e.g. majority wins, minority
  wins, correct-zone wins, speed bonus by arrival order using position
  history).
* Emits events to all consumers: `round_opened`, `countdown`,
  `round_closing`, `answers_locked`, `reveal`, `scores_updated`.

### 4.4 APIs and frontends

* **Player WS** (`/ws/player/{id}`): current round, question, "your zone
  right now" live feedback, personal score, binding-state prompts (ritual).
  Phone page is a single mobile web page — display + identity only, since
  answers are physical. Big text, works locked to portrait, survives
  reconnects (state is server-authoritative).
* **TouchDesigner WS** (`/ws/td`): round/cue events + aggregate data (zone
  counts per answer option for live bar visuals, reveal triggers). TD
  continues to read raw positions from TrackingBox directly.
* **Admin** (`/ws/admin` + privileged REST): show control
  (advance/close/reveal/pause), content reload, and the binding board —
  every player, state, last-seen dot on a floor map, one-tap rebind,
  one-tap ritual trigger. This page is the rehearsal instrument that tells
  us how much auto-rebind tuning reality requires.

## 5. Phases

* **Phase 0 — Scaffold & contract.** Repo, Makefile, tracking client
  speaking to `audience-tracker serve --backend mock`; pin TrackingBox
  commit; models mirrored; CI with pytest. Exit: game server logs live
  positions from the simulator.
* **Phase 1 — Onboarding & bindings.** Claim flow, binding state machine,
  SQLite write-through + crash recovery, minimal admin binding board. Exit:
  40 simulated people claimable; kill/restart game server mid-session with
  no state loss.
* **Phase 2 — Rounds & scoring.** Content format, round engine, zone
  evaluation at close, absent/grace semantics, score events; phone page v1.
  Exit: full multi-round game playable end-to-end against the simulator
  from phones.
* **Phase 3 — TouchDesigner integration.** TD WS, cue vocabulary agreed
  with the visuals team, zone-count aggregates, reveal choreography,
  ritual-corner lighting hook. Exit: TD reacts to a scripted show run.
* **Phase 4 — Robustness under churn.** Auto-rebind scorer, ritual rebind
  flow, scripted churn scenarios, TrackingBox-restart recovery, soak test
  at 40-60 simulated people for a full show duration. Exit: churn scenarios
  pass; no orphan lasts longer than one ritual.
* **Phase 5 — Rehearsal & ops.** Admin polish, structured event log +
  post-show replay (rebuild any moment from `binding_events` + `answers`),
  ops runbook (start order, health checks, what to do when X dies), content
  freeze process. Exit: a non-developer can run the show.

Phases 1-2 are the critical path; 3 can proceed in parallel once the event
vocabulary is fixed at the start of Phase 3.

## 6. Open questions (need answers from the show side)

1. Onboarding 40+ people: staggered entry with numbers on the big screen,
   printed QR/number cards at the door, or ushers with a tablet? This
   decides the claim UX.
2. Show length & re-entry: can people leave and return (bathroom)? If yes,
   the ritual rebind must be a first-class, rehearsed flow, not a fallback.
3. Zone geometry at 40+ density: are answer zones big enough that 20 people
   fit without occluding each other into tracking mush? May inform camera
   height.
4. Tie/edge rules: person standing on a zone boundary at close (first-match
   order decides — is that acceptable?); ties in scores; late joiners
   mid-show.
5. Lighting: how dark do scenes get? Drives ReID reliability expectations
   and how aggressive auto-rebind should be.

## 7. Testing strategy

The mock backend is the centerpiece: the whole game is developed and
load-tested with zero hardware. Beyond unit tests:

* **Scenario harness:** scripted runs against the simulator that inject
  churn — drop a GID and respawn nearby (auto-rebind path), drop two GIDs
  near each other and respawn two (ambiguity -> ritual path), restart
  TrackingBox mid-round (full resync), restart the game server mid-round
  (SQLite recovery). Each scenario asserts on the resulting
  `binding_events` and `answers` rows.
* **Load:** 60 simulated people + 60 fake phone WS clients for a full show
  script.
* **Rehearsal telemetry:** every real rehearsal writes the same event log;
  a small report script summarizes binding breaks per minute, orphan
  durations, and rebind accuracy — the tuning feedback loop for §4.2
  thresholds.
