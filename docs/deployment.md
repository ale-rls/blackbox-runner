# Deployment: blackbox-runner on Coolify + TrackingBox on-prem

This covers running **blackbox-runner** (this repo — bindings, rounds,
scoring, admin/player web) as a container on a remote Coolify instance,
while **TrackingBox** (camera, GPU, Spout output) stays on the venue
machine, exactly where it has to be.

If you're just rehearsing on a laptop with both pieces on one machine, you
don't need any of this — see the main [README](../README.md)'s
Development section, or `run.ps1` (repo root of both checkouts) which
launches TrackingBox + blackbox-runner together locally. This document is
for the "TrackingBox is at the venue, the game server runs elsewhere"
setup.

## Why this shape

blackbox-runner is the WebSocket **client** — it dials out to
TrackingBox's `/ws` and `/api/zones` (`TRACKING_WS_URL` /
`TRACKING_HTTP_URL`, see `server/config.py`), never the other way round.
TrackingBox can't run on Coolify itself: it needs a real camera, a GPU for
the YOLO/ReID pipeline, and Spout video output, none of which exist on a
generic Linux container host. So the split is:

```
Venue PC (Windows, GPU, camera, Spout)          Coolify host (remote VPS)
┌─────────────────────────────┐    Tailscale    ┌──────────────────────────┐
│ TrackingBox                 │◄───────────────►│ blackbox-runner container│
│ audience-tracker serve      │  private tailnet │ uvicorn server.app:app   │
│ :8000 (ws + /api/zones)     │                  │ :8100                    │
└─────────────────────────────┘                  └──────────────────────────┘
                                                        ▲
                                                        │ HTTPS/WSS via
                                                        │ Coolify's Traefik
                                                  Player phones / admin console
```

**Tailscale runs on the Coolify VPS host itself** (not inside the
container, not as a sidecar) — join it to the venue PC's tailnet and the
container reaches TrackingBox transparently through the host's normal
outbound routing, no extra Docker networking needed. This avoids exposing
TrackingBox's `/ws`/`/api/zones` to the public internet at all.

**This makes the live show dependent on the venue's internet uplink** for
every position update and round transition — the engine has reconnect
backoff (`TRACKING_RECONNECT_INITIAL_S`/`_MAX_S`) but a flaky venue
connection will visibly stall rounds. Use a wired uplink at the venue if at
all possible, and rehearse over the actual venue connection before the
show, not just from your home network.

## Prerequisites

- A Coolify instance (self-hosted or Coolify Cloud) with at least one
  server attached.
- Root/admin access to the venue TrackingBox machine, to install
  Tailscale there.
- This repo's `Dockerfile` / `docker-entrypoint.sh` / `.dockerignore`
  (already in the repo root).

## 1. Tailscale: bridge the venue to the Coolify host

**On the Coolify VPS** (SSH in):

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# follow the printed login URL once, authenticate to your tailnet
```

**On the venue TrackingBox PC** (Windows):

```powershell
winget install tailscale.tailscale
tailscale up
```

Confirm both sides see each other:

```bash
tailscale status
# should list both machines; note the venue PC's tailnet IP or
# MagicDNS name, e.g. 100.x.x.x or trackingbox-venue.<tailnet>.ts.net
```

Nothing on the venue PC needs to be exposed publicly — the Coolify host
reaches it only over the private tailnet, and only blackbox-runner (a
container on that host) ever needs to.

## 2. Build: Dockerfile, entrypoint, ignore file

Already in the repo root:

- **`Dockerfile`** — `python:3.11-slim`, installs the package editable
  (`pip install -e .`) so `server/app.py`'s
  `Path(__file__).parent.parent / "web"` keeps resolving to `./web` inside
  the image, exactly like local dev. Copies `server/`, `web/`, `content/`,
  `scripts/`. Exposes `8100`, declares a `/app/data` volume, and has a
  `HEALTHCHECK` hitting `/health`.
- **`docker-entrypoint.sh`** — on every container start (no args given),
  runs `scripts/import_content.py` (pushing the committed
  `content/show.yaml` into the DB at `GAME_DB_PATH`) before `exec`-ing
  uvicorn. This means **a deploy is the same thing as a content
  import/reload** — whatever's committed to `content/show.yaml` is what
  goes live, every time, no separate manual step. If `show.yaml` is
  invalid, `import_content.py` exits non-zero and the container fails to
  start rather than silently serving stale or broken content. Passing an
  explicit command (e.g. `docker run <image> python scripts/replay.py ...`)
  skips the import and runs that command directly — handy for one-off
  post-show analysis against the same image.
- **`.dockerignore`** — keeps `.venv/`, `.git/`, `data/`, `.env`, `tests/`
  out of the build context.

Test it locally before pushing to Coolify:

```bash
docker build -t blackbox-runner:test .
docker run -d --name bbr-test -p 8200:8100 \
  -e TRACKING_WS_URL=ws://<trackingbox-tailnet-host>:8000/ws \
  -e TRACKING_HTTP_URL=http://<trackingbox-tailnet-host>:8000 \
  blackbox-runner:test
docker logs -f bbr-test        # watch the import + startup log lines
curl http://localhost:8200/health
docker rm -f bbr-test
```

(On Windows/Git Bash, passing path-like values through `-e` can get
mangled by MSYS's automatic path conversion — set
`MSYS_NO_PATHCONV=1 docker run ...` if `GAME_DB_PATH` or similar comes out
looking like `C:/Program Files/Git/app/...` in the logs. Doesn't affect
Coolify, which sets env vars through its own UI.)

## 3. Coolify: create the application

1. **New Resource → Application → your Git source** (GitHub/GitLab, or a
   public/private URL), pick this repo and the branch/tag you want to
   ship. **Build Pack: Dockerfile** (Coolify detects the root
   `Dockerfile` automatically).
2. **Port**: `8100` (matches `EXPOSE 8100` / `GAME_PORT`).
3. **Persistent storage**: add a volume mounted at `/app/data`. This is
   the one thing that must survive redeploys — player bindings, answers,
   session state, scores. Everything else (content, code) is baked into
   the image and reproduced fresh on every deploy.
4. **Environment variables** (Coolify's dashboard, never committed):

   | Variable | Value | Notes |
   |---|---|---|
   | `TRACKING_WS_URL` | `ws://<trackingbox-tailnet-host>:8000/ws` | venue PC's Tailscale IP or MagicDNS name |
   | `TRACKING_HTTP_URL` | `http://<trackingbox-tailnet-host>:8000` | same host |
   | `GAME_DB_PATH` | `/app/data/blackbox-runner.db` | matches the mounted volume |
   | `ELEVENLABS_API_KEY` | *(secret)* | only if using in-console voice generation |
   | `ELEVENLABS_VOICE_ID` | *(optional)* | default voice for generation |
   | `RITUAL_ZONE_ID` | `ritual` | only if the ritual-corner rebind flow is used |

   Mark `ELEVENLABS_API_KEY` as a secret in Coolify's UI. Rotate whatever
   key is currently sitting in your local `.env` before it goes anywhere
   shared — it's gitignored, but treat it as already-seen once it's been
   pasted into any chat or ticket.

5. **Health check**: path `/health`, matching the Dockerfile's built-in
   `HEALTHCHECK` — wire Coolify's own health check to the same path so a
   bad deploy is caught before traffic hits it.
6. **Replicas: 1.** SQLite + in-memory engine state (`server/engine.py`,
   `server/bindings.py`) means this cannot horizontally scale. Don't let
   Coolify auto-scale or run more than one instance against the same
   volume.
7. **Domain**: attach your domain in Coolify; its Traefik proxy handles
   TLS and proxies WebSocket upgrades (`/ws/player/{seat}`, `/ws/td`, the
   admin WS) by default — nothing extra to configure there.
8. **Deploy.** Watch the build log for the same import/startup sequence
   you saw locally; then hit `https://<your-domain>/health`.

## 4. Redeploying (content or code changes)

Every deploy re-imports whatever's committed in `content/show.yaml` via
the entrypoint, and every fresh process load reads straight from the DB
at its own startup (`content_db.load_show_db` in `server/app.py`) — so a
redeploy *is* the content-apply step. The normal loop:

1. Edit `content/show.yaml` locally.
2. `python scripts/validate_content.py` against a reachable TrackingBox
   (or the venue's, over Tailscale) to catch zone typos before committing.
3. Commit, push.
4. Trigger a Coolify deploy (auto on push if you've wired that up, or
   manually from the dashboard).

No need to hit `/api/admin/content/reload` after a deploy — that endpoint
is for pushing a content change into an *already-running* process without
a restart (see `docs/runbook.md`'s content freeze process); a fresh
container start already picks up the freshly-imported content on its own.

## 5. Backups

The SQLite file on the `/app/data` volume is the entire record of a show —
bindings, answers, scores, all replayable via `scripts/replay.py`. Back it
up:

- Use Coolify's built-in backup feature on the volume if available, or
- A simple cron on the Coolify host copying the volume's mount path
  somewhere durable.

Do this especially right after a show — that's the whole point of having
run it.

## 6. Pre-show checklist

1. Tailscale up on both ends (`tailscale status` on the VPS shows the
   venue PC).
2. TrackingBox running and healthy on the venue PC **before** you
   (re)start blackbox-runner — same "backend must be up at startup or the
   show loads empty" constraint as local dev.
   ```powershell
   audience-tracker serve --config config.json   # or your venue run script
   curl http://localhost:8000/health
   ```
3. From outside: `curl https://<your-domain>/health` and
   `curl https://<your-domain>/api/admin/content` (should show the
   expected round count).
4. Claim a test player and run one full round end-to-end over the real
   venue connection — not just from your home network.
5. Know your rollback path (below) but don't expect to use it mid-show —
   lean on the rehearsal instead.

## 7. Rollback

Coolify keeps previous successful builds — redeploy the prior one from
its dashboard if a fresh deploy is broken. A mid-show rollback is risky
(it re-imports whatever content was committed at that older revision,
which may not match what players have already answered against); prefer
catching problems in the pre-show checklist over rolling back live.

## Troubleshooting

**Container starts then immediately exits, log ends at "Importing
content/show.yaml..."** — `scripts/import_content.py` rejected the show
content (invalid YAML, a "choice"-form option referencing a zone id that
doesn't exist in the venue's real `/api/zones`, etc.). The traceback in
Coolify's log names the exact round and problem; fix `show.yaml` and
redeploy. This is deliberate fail-fast behavior, not a bug — the same
class of error `scripts/validate_content.py` catches locally.

**`/health` shows `tracking_connected: false`** — the container can't
reach `TRACKING_WS_URL`. Check `tailscale status` on the Coolify host,
and that TrackingBox is actually running on the venue PC. The app itself
doesn't crash on this (it retries with backoff, `server/app.py`'s
lifespan logs a warning and keeps going), but no round can be answered
until it reconnects.

**Garbled/mangled non-ASCII text (`�`) in questions or narration** —
`content/show.yaml` must be saved as UTF-8. `server/content.py`'s
`load_show()` and `scripts/import_content.py` both read it with an
explicit `encoding="utf-8"`; if an editor re-saves the file in a different
encoding (Windows' default locale encoding is a common culprit), the
symptom is exactly this replacement-character garbling for German
umlauts/ß/smart quotes, and it can go unnoticed on Windows (which falls
back to a locale codepage) until it hits Linux/Docker's strict UTF-8 —
this is exactly what happened once already; see the Dockerfile-build
verification that caught it.
