import { sveltekit } from "@sveltejs/kit/vite";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [sveltekit()],
  server: {
    // `npm run dev` against a locally running game server.
    proxy: {
      "/api": "http://localhost:8100",
      "/audio": "http://localhost:8100",
      "/ws": { target: "ws://localhost:8100", ws: true },
    },
  },
});
