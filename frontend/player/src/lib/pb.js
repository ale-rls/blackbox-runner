// PocketBase realtime (issue #17): the game server's WS stays the primary
// orchestration channel (timers, cues); PocketBase provides the shared
// public state — rounds and score_events are readable/subscribable with
// zero credentials (rules set by scripts/pocketbase_bootstrap.py).
import PocketBase from "pocketbase";

import { gameFetch } from "$lib/config.js";

const q = (s) => "'" + String(s).replace(/\\/g, "\\\\").replace(/'/g, "\\'") + "'";

// Shared instance so file URLs can be built anywhere once connected.
let pbInstance = null;

/**
 * Absolute URL for a PocketBase-stored file, via the SDK's file API.
 * ``ref`` is the round payload's audio_file:
 * {collection, record_id, filename}. Null until connectPocketBase() has
 * resolved (callers fall back to the game-served /audio URL).
 */
export function pbFileUrl(ref) {
  if (!ref || !pbInstance) return null;
  return pbInstance.files.getURL(
    { id: ref.record_id, collectionId: ref.collection, collectionName: ref.collection },
    ref.filename,
  );
}

export async function connectPocketBase({ playerId, onScore, onRoundRecord }) {
  const cfg = await gameFetch("/api/config").then((r) => r.json());
  const pb = new PocketBase(cfg.pocketbase_url);
  pbInstance = pb;

  // Score badge is driven entirely from the public score_events collection:
  // initial sum via REST, then live increments via realtime.
  let total = 0;
  try {
    const events = await pb.collection("score_events").getFullList({
      filter: `player_key=${q(playerId)}`,
    });
    total = events.reduce((sum, e) => sum + (e.points || 0), 0);
    onScore(total);
  } catch {
    // score badge is cosmetic — never block the page on it
  }
  pb.collection("score_events").subscribe("*", (e) => {
    if (e.action === "create" && e.record.player_key === playerId) {
      total += e.record.points || 0;
      onScore(total);
    }
  });

  // Round state changes straight from the DB — keeps the page honest even
  // if the game WS drops for a moment.
  pb.collection("rounds").subscribe("*", (e) => onRoundRecord(e.record, e.action));

  return pb;
}
