// Where the game server lives. Empty (the default) means same-origin —
// the app is being served by the game server itself. Only used as a
// dev/same-origin convenience now (issue #16): the deployed phone talks
// only to PocketBase and never reaches the game server.
export const GAME_URL = import.meta.env.VITE_GAME_URL || "";

// PocketBase's public URL. The deployed player frontend (issue #16) has no
// route to the venue game server, so it reads and writes everything through
// PocketBase directly. Build the standalone bundle with
//   VITE_POCKETBASE_URL=https://pocketbase.example.com npm run build
// When empty (same-origin dev, game server serving the page) pb.js falls
// back to asking the game server via /api/config.
export const POCKETBASE_URL = import.meta.env.VITE_POCKETBASE_URL || "";

export function gameFetch(path, opts) {
  return fetch(`${GAME_URL}${path}`, opts);
}

/** Absolute URL for a game-server path like /audio/x.mp3 (no-op when
 * same-origin; already-absolute URLs pass through untouched). */
export function gameUrl(path) {
  if (!path || /^https?:/.test(path)) return path;
  return `${GAME_URL}${path}`;
}

export function gameWsUrl(path) {
  if (GAME_URL) return GAME_URL.replace(/^http/, "ws") + path;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}${path}`;
}
