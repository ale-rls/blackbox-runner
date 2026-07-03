# Run with TouchDesigner

TouchDesigner needs two independent connections:

* **Raw positions**, straight from TrackingBox (`ws://localhost:8000/ws`) —
  unchanged from TrackingBox's own setup. See
  [TrackingBox's touchdesigner.md](https://github.com/ale-rls/TrackingBox/blob/main/docs/touchdesigner.md)
  and `td_scripts/td_receive_state.py` in that repo for GID/floor/zone
  instancing.
* **Round/cue events**, from this game server (`ws://localhost:8100/ws/td`)
  — round state, live zone counts for bar visuals, and reveal/score
  choreography. That's what this doc and `td_scripts/td_receive_cues.py`
  cover.

TouchDesigner never needs to know about players, GIDs, or bindings — only
the current round and the cues below.

## Setup

* **WebSocket DAT**: network address `localhost`, port `8100`, request
  `/ws/td`. Enable auto-reconnect if available — the server sends a fresh
  `hello` on every connect, so a dropped connection self-heals.
* **Callbacks DAT**: `td_scripts/td_receive_cues.py`
* **Output tables**: Table DATs named `round`, `zone_counts`, and `scores`
  (created automatically by the script if missing).

## Cue vocabulary

Every message is `{"type": "<cue>", ...payload}`. This is the same channel
the design doc calls the "round/cue WS" — round lifecycle events and
one-off cues share it.

| Cue | When | Payload |
|---|---|---|
| `hello` | once, on connect | `round` (current round or `null`), `zone_counts`, `zones` (TrackingBox's zone map, for floor overlays) |
| `round_opened` | a round starts | round fields: `round_id`, `index`, `state`, `question`, `options`, `duration_s`, `grace_s`, `opened_at`, `closed_at` |
| `zone_counts` | every ~1s while a round is `active` | `round_id`, `counts` (`{zone_id: player_count}`) — drives live bar visuals during play |
| `round_closing` | answers just locked | round fields, `state: "closing"` |
| `answers_locked` | grace window has resolved (late rebinds folded in) | round fields |
| `reveal` | scores computed | round fields + `tally` (`{zone_id: count}`) + `winning_zones` (list of zone ids) — trigger reveal choreography here |
| `scores_updated` | right after `reveal` | `scores` (`{player_id: total_points}`) |
| `ritual_prompt` | an orphaned player is sent to the lit corner (Phase 4; stubbed manually until then — see below) | free-form, e.g. `{"player_id": "...", "corner_zone": "..."}` |

Round fields always describe the *current* round — there's no need to
diff against a previous message; each cue's payload is a complete
snapshot.

## Firing test cues by hand

Phase 4 wires up the automatic ritual-rebind trigger. Until then (and for
any other one-off cue you want to rehearse against), an operator can push
any named cue straight to TD and every connected phone:

```bash
curl -X POST http://localhost:8100/api/admin/cues/ritual_prompt \
  -H "Content-Type: application/json" \
  -d '{"payload": {"player_id": "seat-14", "corner_zone": "answer_a"}}'
```

TD receives `{"type": "ritual_prompt", "player_id": "seat-14", "corner_zone": "answer_a"}`
exactly as it would once Phase 4 fires it automatically — build and test
the lighting hook against this now.

## Sanity check

```bash
curl -s http://localhost:8100/api/rounds/current | jq
curl -s http://localhost:8100/api/scores | jq
```
