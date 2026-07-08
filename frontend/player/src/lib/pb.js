// The deployed player frontend (issue #16) has no route to the venue game
// server, so PocketBase is its *only* backend: every read (player state,
// round content, scores, per-player reveal, claimable GIDs) is a realtime
// subscription here, and the one write it makes — claiming a GID — is a
// record the game server consumes over its own realtime stream and resolves
// back. No polling, no /api/* calls to the game server.
import PocketBase from "pocketbase";

import { POCKETBASE_URL, gameFetch } from "$lib/config.js";

const q = (s) => "'" + String(s).replace(/\\/g, "\\\\").replace(/'/g, "\\'") + "'";

// Shared instance so submitClaim can be used anywhere once connectPlayer()
// has resolved.
let pbInstance = null;

// PocketBase's base URL, known immediately in a standalone build and
// resolved once from the game server in same-origin dev. File URLs only
// need this — not a live PocketBase connection.
let pbBaseUrl = POCKETBASE_URL || null;

/**
 * Resolve (and cache) PocketBase's base URL. Pages that never call
 * connectPlayer (the /listen route) call this so pbFileUrl can work.
 */
export async function ensurePocketbaseUrl() {
  if (!pbBaseUrl) {
    // Same-origin dev: the game server serves the page and can hand us the URL.
    const cfg = await gameFetch("/api/config").then((r) => r.json());
    pbBaseUrl = cfg.pocketbase_url;
  }
  return pbBaseUrl;
}

/**
 * Absolute URL for a PocketBase-stored file (the /api/files/... form).
 * ``ref`` is the round payload's audio_file:
 * {collection, record_id, filename}. Null until the base URL is known
 * (callers fall back to the game-served /audio URL, which only exists in
 * same-origin dev).
 */
export function pbFileUrl(ref) {
  if (!ref || !pbBaseUrl) return null;
  return `${pbBaseUrl}/api/files/${ref.collection}/${ref.record_id}/${encodeURIComponent(ref.filename)}`;
}

/**
 * Connect to PocketBase and wire every realtime subscription the player page
 * needs. Callbacks fire with the latest value on connect and on every change:
 *
 *   onPlayer({id, gid, state, display_name})  this device's binding state
 *   onRound(payload)                          current round (the denormalized
 *                                             rounds.payload the server writes)
 *   onYourAnswer({zone, resolved})            this player's own reveal
 *   onScore(total)                            personal score (sum of events)
 *   onAvailable([gid, ...])                   GIDs currently claimable
 *
 * Returns the PocketBase instance (kept for pbFileUrl / submitClaim).
 */
export async function connectPlayer({
  playerId,
  onPlayer,
  onRound,
  onYourAnswer,
  onScore,
  onAvailable,
}) {
  const pb = new PocketBase(await ensurePocketbaseUrl());
  pbInstance = pb;

  // Active session id + claimable GIDs live on the game_state singleton.
  // Fetch it first (awaited) so player/round/reveal reads can scope to the
  // current show — a returning player_key may still have stale rows from a
  // previous session.
  let sessionId = null;
  const applyGameState = (rec) => {
    if (!rec) return;
    if (rec.session_id) sessionId = rec.session_id;
    onAvailable && onAvailable(rec.available_gids || []);
  };
  try {
    const rows = await pb.collection("game_state").getFullList({ sort: "-updated_at" });
    applyGameState(rows[0]);
  } catch {
    // No game_state yet (fresh instance): claims stay open, server validates.
  }
  pb.collection("game_state").subscribe("*", (e) => applyGameState(e.record));

  const inSession = (rec) => !sessionId || !rec.session || rec.session === sessionId;

  // This device's player record (bound / lost / orphaned state).
  const applyPlayer = (rec) => {
    if (!rec || rec.player_key !== playerId || !inSession(rec)) return;
    onPlayer &&
      onPlayer({
        id: rec.player_key,
        gid: rec.gid,
        state: rec.state,
        display_name: rec.display_name || null,
      });
  };
  try {
    const rows = await pb
      .collection("players")
      .getFullList({ filter: `player_key=${q(playerId)}` });
    const mine = sessionId
      ? rows.find((r) => r.session === sessionId)
      : rows[rows.length - 1];
    if (mine) applyPlayer(mine);
  } catch {
    // player record appears on first claim; nothing to show until then.
  }
  pb.collection("players").subscribe("*", (e) => applyPlayer(e.record));

  // Round state + content: the server denormalizes the whole player-facing
  // payload onto the public rounds record, so one subscription carries
  // everything (and, at reveal, the tally + winning zones).
  let currentIdx = -1;
  const applyRound = (rec) => {
    if (!rec || !rec.payload || !inSession(rec)) return;
    if (typeof rec.idx === "number" && rec.idx < currentIdx) return; // ignore stale older rounds
    if (typeof rec.idx === "number") currentIdx = rec.idx;
    onRound && onRound(rec.payload);
  };
  try {
    const filter = sessionId ? `session=${q(sessionId)}` : "";
    const rows = await pb
      .collection("rounds")
      .getFullList({ filter, sort: "-idx" });
    if (rows[0]) applyRound(rows[0]);
  } catch {
    // no round opened yet
  }
  pb.collection("rounds").subscribe("*", (e) => applyRound(e.record));

  // This player's own reveal ("you were here"), from the public projection —
  // the full answers table stays superuser-only.
  const applyReveal = (rec) => {
    if (!rec || rec.player_key !== playerId || !inSession(rec)) return;
    onYourAnswer && onYourAnswer({ zone: rec.zone || null, resolved: rec.resolved });
  };
  pb.collection("player_reveals").subscribe("*", (e) => applyReveal(e.record));

  // Score badge: initial sum via REST, then live increments from score_events.
  let total = 0;
  try {
    const events = await pb.collection("score_events").getFullList({
      filter: `player_key=${q(playerId)}`,
    });
    total = events.reduce((sum, e) => sum + (e.points || 0), 0);
    onScore && onScore(total);
  } catch {
    // score badge is cosmetic — never block the page on it
  }
  pb.collection("score_events").subscribe("*", (e) => {
    if (e.action === "create" && e.record.player_key === playerId && inSession(e.record)) {
      total += e.record.points || 0;
      onScore && onScore(total);
    }
  });

  return pb;
}

/**
 * Connect the /listen (operator/debug) page: like connectPlayer but with no
 * player seat — it watches the whole show instead of one phone's slice.
 *
 *   onRound(payload)          current round (rounds.payload; at reveal the
 *                             same payload carries tally + winning_zones)
 *   onScores({playerId: n})   full scoreboard, summed from score_events
 *   onZoneCounts(counts)      live per-zone headcount from the live_stats
 *                             singleton the game server mirrors out of its
 *                             /ws/td stream (absent until the collection is
 *                             bootstrapped — the page degrades to the tally)
 *
 * Returns the PocketBase instance.
 */
export async function connectListener({ onRound, onScores, onZoneCounts }) {
  const pb = new PocketBase(await ensurePocketbaseUrl());

  // Active session id first, so round/score reads scope to the current show.
  let sessionId = null;
  try {
    const rows = await pb.collection("game_state").getFullList({ sort: "-updated_at" });
    if (rows[0]?.session_id) sessionId = rows[0].session_id;
  } catch {
    // No game_state yet (fresh instance): show everything unscoped.
  }

  const inSession = (rec) => !sessionId || !rec.session || rec.session === sessionId;

  let currentIdx = -1;
  const applyRound = (rec) => {
    if (!rec || !rec.payload || !inSession(rec)) return;
    if (typeof rec.idx === "number" && rec.idx < currentIdx) return; // ignore stale older rounds
    if (typeof rec.idx === "number") currentIdx = rec.idx;
    onRound && onRound(rec.payload);
  };
  try {
    const filter = sessionId ? `session=${q(sessionId)}` : "";
    const rows = await pb.collection("rounds").getFullList({ filter, sort: "-idx" });
    if (rows[0]) applyRound(rows[0]);
  } catch {
    // no round opened yet
  }
  pb.collection("rounds").subscribe("*", (e) => applyRound(e.record));

  // Whole-show scoreboard: initial sums via REST, then live increments.
  const totals = {};
  const bump = (rec) => {
    totals[rec.player_key] = (totals[rec.player_key] || 0) + (rec.points || 0);
  };
  try {
    const filter = sessionId ? `session=${q(sessionId)}` : "";
    for (const ev of await pb.collection("score_events").getFullList({ filter })) bump(ev);
    onScores && onScores({ ...totals });
  } catch {
    // scoreboard is cosmetic on this page — never block on it
  }
  pb.collection("score_events").subscribe("*", (e) => {
    if (e.action !== "create" || !inSession(e.record)) return;
    bump(e.record);
    onScores && onScores({ ...totals });
  });

  // Live zone counts. Guarded separately: live_stats only exists once
  // scripts/pocketbase_bootstrap.py has run against this instance.
  const applyStats = (rec) => {
    if (!rec) return;
    if (sessionId && rec.session_id && rec.session_id !== sessionId) return;
    onZoneCounts && onZoneCounts(rec.zone_counts || {});
  };
  try {
    const rows = await pb.collection("live_stats").getFullList({ sort: "-updated_at" });
    applyStats(rows[0]);
  } catch {
    // collection missing or empty — bars stay on the reveal tally
  }
  pb.collection("live_stats").subscribe("*", (e) => applyStats(e.record)).catch(() => {});

  return pb;
}

/**
 * Submit a claim: create a public claim_requests row, then wait for the game
 * server (which consumes the collection over realtime) to resolve it. Resolves
 * on success; rejects with the server's message on failure. The player's own
 * ``players`` record flips to ``bound`` via the subscription above.
 */
export async function submitClaim(playerId, gid, displayName) {
  const pb = pbInstance;
  if (!pb) throw new Error("Noch nicht verbunden — bitte kurz warten.");
  const req = await pb.collection("claim_requests").create({
    player_key: playerId,
    gid,
    display_name: displayName || "",
    status: "pending",
    at: Date.now() / 1000,
  });

  return await new Promise((resolve, reject) => {
    let settled = false;
    let unsub = null;
    let poll = null;
    const cleanup = () => {
      if (poll) clearInterval(poll);
      if (unsub) Promise.resolve(unsub).then((fn) => fn && fn()).catch(() => {});
    };
    const finish = (rec) => {
      if (settled || !rec) return;
      if (rec.status === "done") {
        settled = true;
        cleanup();
        resolve();
      } else if (rec.status === "error") {
        settled = true;
        cleanup();
        reject(new Error(rec.detail || `Verbindung fehlgeschlagen`));
      }
    };
    pb.collection("claim_requests")
      .subscribe(req.id, (e) => finish(e.record))
      .then((fn) => {
        unsub = fn;
        // Catch a resolution that landed before the subscription was live.
        pb.collection("claim_requests").getOne(req.id).then(finish).catch(() => {});
      })
      .catch(() => {});
    // Belt-and-suspenders fallback if a realtime event is ever missed.
    let waited = 0;
    poll = setInterval(async () => {
      waited += 1;
      try {
        finish(await pb.collection("claim_requests").getOne(req.id));
      } catch {
        /* keep waiting */
      }
      if (!settled && waited >= 12) {
        settled = true;
        cleanup();
        reject(new Error("Zeitüberschreitung — bitte erneut versuchen"));
      }
    }, 1000);
  });
}
