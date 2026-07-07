import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// base: the built app is served by the game server, index.html at
// /p/{player_id} and assets mounted at /player-app/ (server/app.py).
export default defineConfig({
  base: "/player-app/",
  plugins: [svelte()],
  server: {
    // `npm run dev` against a locally running game server.
    proxy: {
      "/api": "http://localhost:8100",
      "/audio": "http://localhost:8100",
      "/ws": { target: "ws://localhost:8100", ws: true },
    },
  },
});
