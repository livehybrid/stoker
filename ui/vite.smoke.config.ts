import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { TanStackRouterVite } from "@tanstack/router-plugin/vite";

/*
 * A single-file build of the console, for smoke.mjs.
 *
 * The shipped build is ES modules with per-route code splitting, which is what
 * a browser wants and what jsdom cannot evaluate: it has no module-script
 * support, so `window.eval` of the entry fails on its first `export`. This
 * config builds the same sources into one IIFE that jsdom can run.
 *
 * The trade-off is explicit: the smoke test exercises the same code, not the
 * same chunking. It will not catch a broken dynamic import in the real build.
 * What it does catch is the whole app rendering every route without throwing,
 * which is the failure that actually happens.
 */
export default defineConfig({
  plugins: [TanStackRouterVite({ autoCodeSplitting: false }), react()],
  build: {
    outDir: "dist-smoke",
    sourcemap: false,
    // One file: no dynamic imports for jsdom to fail to fetch.
    lib: {
      entry: "src/main.tsx",
      name: "StokerUI",
      formats: ["iife"],
      fileName: () => "smoke-bundle.js",
    },
  },
  define: { "process.env.NODE_ENV": '"production"' },
});
