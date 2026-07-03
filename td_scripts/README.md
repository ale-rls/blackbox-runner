# TouchDesigner scripts

These run inside TouchDesigner's own Python (they use `op()`), not the game
server's venv — nothing here is imported or unit-tested by `pytest`.

* `td_receive_cues.py` — Callbacks DAT for a WebSocket DAT pointed at
  `ws://localhost:8100/ws/td`. Maintains `round`, `zone_counts`, and
  `scores` Table DATs from the round/cue stream. See
  [../docs/touchdesigner.md](../docs/touchdesigner.md) for the full cue
  vocabulary and setup steps.

For raw per-GID positions (instancing, floor overlays), connect a second
WebSocket DAT straight to TrackingBox (`ws://localhost:8000/ws`) and use
its own `td_receive_state.py` / `td_fetch_zone_counts.py` — this repo does
not duplicate that path.
