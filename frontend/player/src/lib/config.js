// Where the game server lives. Empty (the default) means same-origin —
// the app is being served by the game server itself. For a standalone
// static deployment (Coolify, any static host), build with
//   VITE_GAME_URL=https://game.example.com npm run build
// and everything (REST, WS, /audio, /api/config) points there instead.
export const GAME_URL = import.meta.env.VITE_GAME_URL || "";

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
