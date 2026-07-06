# Ops Runbook

This is written for whoever is running the show from the booth — stage
manager, usher lead, whoever. No programming required to follow it. If
something isn't covered here, screenshot the admin dashboard and the
terminal output before touching anything else.

## Before doors open

1. **Content is frozen.** See [Content freeze process](#content-freeze-process)
   below — do this the day before, not at showtime.
2. **Camera is positioned and the venue calibration is saved** to
   `config.json` in the TrackingBox checkout (see TrackingBox's own
   `docs/floor_projection.md` if this hasn't been done yet for this venue).
3. Confirm both machines/checkouts are on the versions you rehearsed with:
   - TrackingBox: pinned commit in this repo's [README](../README.md).
   - This repo: whatever tag/commit you rehearsed the show on.

## Start order

Start TrackingBox **first**, wait for it to be healthy, **then** start the
game server. Starting them the other way round isn't harmful (the game
server retries the connection), but it makes the first couple of minutes
of logs confusing.

```bash
# 1. TrackingBox, from its own checkout, with the venue's calibrated config
audience-tracker serve --config config.json --port 8000

# check it's alive before moving on:
curl http://localhost:8000/health
#   {"status":"ok","pipeline":true,"pipeline_running":true}
#   pipeline_running:false means tracking died while the API stayed up — see
#   "TrackingBox is up but nobody is being tracked" below.

# 2. Game server, from this repo
make dev
# or, equivalently, with any RITUAL_ZONE_ID / tuning overrides for the night:
RITUAL_ZONE_ID=ritual make dev

curl http://localhost:8100/health
#   {"status":"ok","tracking_connected":true,"tracking_ws_url":"..."}
```

Open the admin dashboard: `http://localhost:8100/admin/`. The binding
board's connection indicator should say **connected** in green. If it
says **disconnected**, the game server process isn't reachable — check its
terminal for a crash, restart it (state survives, see
[Crash recovery](#crash-recovery-what-actually-happens) below).

## Onboarding players

However the door process hands out player numbers (see
`docs/architecture.md` §6 open question 1 — staggered entry, printed
cards, or ushers with a tablet, depending on what was decided for this
run), each player's phone needs to land on:

```
http://<venue-machine-ip>:8100/p/<their-player-id>
```

They type the number shown on their dot and tap Claim. Watch the binding
board fill up as people join. **Do not start the first round until the
binding board looks right** — spot-check a few players' states, and use
the floor map to sanity-check that GIDs are roughly where you'd expect
people to physically be standing.

## Running the show

From the admin dashboard's **Round control** section:

- **Start next round** — opens the next question of the imported show.
  Timing is automatic from there (`duration_s`, then `grace_s`), but you
  can also:
- **Close** — lock in answers early (e.g. everyone's clearly settled and
  you don't want to wait out the full timer).
- **Reveal** — force the reveal early, or manually finish a round that got
  interrupted by a restart (see below).

Watch the **Scoreboard** section update after each reveal. The **Binding
board** keeps running underneath the whole time — orphaned players
(yellow) are being prompted to the ritual corner automatically; a
persistent orphan (more than a few seconds) is your cue to walk over and
help, or use the **Rebind** button next to their row once you can see
which dot is them.

### Narration steps and audio

Steps with `type: narration` (the Modell-Intro,
epoch intros/outros) have no answers and no timer when `duration_s: 0`:
**Start next round** opens them, the players read the `text` and hear the
mp3, and you press **Reveal** to finish the step and move on.

Voice-over mp3s live in `content/audio/` (override with `GAME_AUDIO_DIR`)
and are served at `/audio/<name>.mp3`. To wire one up: drop the file in
that folder, set the step's audio file via the **Show editor** (or set
`audio: <name>.mp3` in `show.yaml` and re-import), then **Reload content**
from the admin dashboard (only works between rounds).
Players unlock audio with their first tap (the claim button); anyone who
reconnects without tapping gets a "Tippen, um den Ton zu starten" overlay
the next time a step tries to speak.

### Show editor and ElevenLabs voice generation

The **Show editor** card at the bottom of the admin dashboard lists every
step of the imported show. **Edit** changes a step's question/title,
narration text, audio file, timing, and labels; **Save changes** writes
straight to the server's database and hot-reloads it if no round is in
flight. If a round *is* in flight the edit is still saved and takes effect
on the next **Reload content**. Structural changes — adding/removing
steps, changing zones or form types — are done in the authoring copy
`content/show.yaml`, then applied with `scripts/import_content.py` (see
[Content freeze process](#content-freeze-process)). Note that admin-console
edits live only in the DB: re-importing show.yaml overwrites them, so port
edits you want to keep back into the YAML.

To generate voice-overs with ElevenLabs, start the server with an API key
(and usually a default voice):

```bash
ELEVENLABS_API_KEY=... ELEVENLABS_VOICE_ID=... make dev
# optional: ELEVENLABS_MODEL_ID (default eleven_multilingual_v2, good German)
```

Then use **Generate voice** on a step (renders that step's `text` to
`content/audio/<step-id>.mp3` and points the step's `audio:` at it) or
**Generate missing audio** to fill every step that has no mp3 on disk.
The voice id field in the card header overrides the default voice per
generation — handy for auditioning voices. Generation costs ElevenLabs
credits and a long monologue takes tens of seconds; do this in prep, not
mid-show.

## Health checks, anytime

```bash
curl http://localhost:8000/health          # TrackingBox
curl http://localhost:8100/health          # game server
curl http://localhost:8100/api/rounds/current
curl http://localhost:8100/api/scores
```

## When things go wrong

**TrackingBox process dies.** The game server keeps running and keeps
retrying the connection (exponential backoff, capped). Every bound player
will show as `lost` within a few seconds — this is expected, not a bug.
Restart TrackingBox with the same command as before; the game server
reconnects automatically, gets a fresh snapshot, and the auto-rebind/
ritual flow takes it from there. You don't need to restart the game
server for this.

**TrackingBox is up but nobody is being tracked.** `curl .../health` shows
`"pipeline_running": false` — the tracking thread died but the API stayed
alive. This means a persistent camera fault (unplugged, driver crash).
Check the TrackingBox terminal output, fix the camera connection, and
restart TrackingBox.

**Game server process dies.** Restart it (`make dev` again, or however
your venue script launches it). See
[Crash recovery](#crash-recovery-what-actually-happens) — nothing is lost,
but read that section once before showtime so a restart mid-round doesn't
surprise you.

**A specific player is stuck `lost` or `orphaned` for a while.** This is
normal under heavy tracking churn and usually resolves itself (auto-rebind
or the ritual prompt) within a few seconds. If it doesn't:
1. Have them re-type their number if the phone is showing the claim form
   again (it does, automatically, once `lost`).
2. Or use the admin dashboard's **Rebind** button on their row — type the
   GID you can see is them (cross-reference the floor position) and hit
   Rebind. This works from any state and can even steal a GID from someone
   else if you've misjudged (that other player will show `lost` and can
   redo the same process).

**WebSocket DAT in TouchDesigner shows stale data.** Enable auto-reconnect
on the WebSocket DAT if your TD build supports it. Both `/ws` (raw
positions, from TrackingBox) and `/ws/td` (round/cue events, from this
server) resend a full snapshot/`hello` on every reconnect, so a dropped
connection self-heals once it reconnects.

**A round got interrupted mid-way (server crash, TrackingBox restart
during closing).** After the game server restarts, check
`GET /api/rounds/current` — if it shows a round in `closing` state, that
round's answers were preserved but nobody revealed it yet. Hit **Reveal**
on the admin dashboard to finish it manually; it will not repeat or
re-run.

## Crash recovery: what actually happens

Every meaningful state change (a claim, a rebind, a round opening,
closing, or revealing, a score) is written to SQLite immediately — not
batched, not buffered. If the game server process dies for any reason and
you restart it against the same database file:

- Every player's binding state (who's bound to what GID, or lost) is
  exactly as it was the instant before the crash.
- If the last round had already fully finished (revealed), the show
  resumes cleanly at the next round — nothing repeats.
- If a round was interrupted mid-flight (someone had closed it, but it
  hadn't been revealed yet), it comes back in a `closing` state with every
  answer that had already been recorded — nothing is lost — and waits for
  an operator to hit **Reveal**. It deliberately does *not* try to guess
  how much time was left on the original timer and resume it automatically
  (the server doesn't know how long it was down for).

You do not need to do anything special to "resume" a session — starting
the game server against the same `--db` file (the default, unless
`GAME_DB_PATH` was overridden) picks the same session back up
automatically. The startup log line tells you which happened:

```
Started new session 1              # fresh start
Resuming session 1 (crash recovery)  # picked up where it left off
```

## Post-show: replay and audit

Every binding change, answer, and score event is in the database with a
timestamp and a reason — this is the "why did seat-14 lose points in round
3" answer machine.

```bash
python scripts/replay.py --db data/blackbox-runner.db --list-sessions
python scripts/replay.py --db data/blackbox-runner.db --timeline
python scripts/replay.py --db data/blackbox-runner.db --player seat-14
python scripts/replay.py --db data/blackbox-runner.db --at 1730000000   # state as of a unix timestamp
```

This is safe to run while the show is still live (SQLite WAL mode allows
concurrent reads), so it also works as a rehearsal-night tuning tool — see
`docs/architecture.md` §7's note on the rehearsal telemetry feedback loop
for `REBIND_MAX_DISTANCE` / `REBIND_MAX_GAP_S` / `ORPHAN_AFTER_S`.

## Content freeze process

The server plays the show from its database; `content/show.yaml` is the
git-tracked authoring copy. Content changes flow show.yaml → validate →
import → reload:

1. Whoever owns the questions edits `content/show.yaml` — directly (plain
   YAML, no code required; see the existing rounds for the format:
   `type: majority | minority | correct_zone | narration`, `options` each
   with a `zone` that must match a real TrackingBox zone id). Simple
   text/label/timing changes can instead be made live in the admin
   dashboard's **Show editor** (they skip the import, but also aren't in
   git — port keepers back into the YAML).
2. Validate it against the venue's actual zone map before the show
   (requires TrackingBox already running with the venue's zone config):
   ```bash
   python scripts/validate_content.py
   #   OK: content/show.yaml — 3 round(s) validated against 3 live zone(s)
   #     [majority     ] r1: 'Coffee or tea?' -> answer_a (Coffee), answer_b (Tea)
   #     ...
   ```
3. Import it into the game database (validates again; writes nothing if
   invalid — add `--dry-run` to only check):
   ```bash
   python scripts/import_content.py   # defaults: content/show.yaml -> data/blackbox-runner.db
   ```
4. If the server is already running, apply it without a restart:
   ```bash
   curl -X POST http://localhost:8100/api/admin/content/reload
   ```
   This re-validates the imported rounds (every "choice" answer option must
   reference a real TrackingBox zone — a typo is rejected, not silently
   accepted) and swaps them in. It's refused with a 409 if a round is
   currently in progress — reload between rounds only.

**Freeze content before doors open.** Import/reload is for rehearsal
iteration, not for changing the show while it's running. Once the show
starts, don't touch content unless you have a specific, tested reason and
you do it between rounds — reordering or removing rounds mid-show can
shift what "round 3" means for anyone relying on the recovered index
after a restart.
