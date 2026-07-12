import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { TanStackRouterVite } from "@tanstack/router-plugin/vite";

// The router plugin scans src/routes/ and (re)generates src/routeTree.gen.ts.
// It must run BEFORE @vitejs/plugin-react so the generated tree is transformed.
// Same-origin app: the control plane serves ui/dist and exposes the API under
// /api, so no proxy is needed in production. The dev proxy below forwards /api
// to a locally running control plane for `npm run dev`.
export default defineConfig({
  plugins: [
    TanStackRouterVite({ autoCodeSplitting: true }),
    react(),
  ],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8080",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
