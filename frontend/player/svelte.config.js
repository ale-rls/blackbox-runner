import adapter from "@sveltejs/adapter-static";

/** @type {import('@sveltejs/kit').Config} */
export default {
  kit: {
    // Pure SPA: every route falls back to index.html and resolves
    // client-side (ssr is off in src/routes/+layout.js). The build output
    // is plain static files — deployable to any static host, or served by
    // the game server itself (server/app.py).
    adapter: adapter({ fallback: "index.html", precompress: false }),
  },
};
