import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev proxy: React (5273) -> Python harness backend (8799). In a packaged
// Electron build the same transport calls route through IPC instead -- see
// src/transport.ts. This keeps the app backend-agnostic, not web-locked.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5273,
    proxy: {
      "/api": { target: "http://127.0.0.1:8799", changeOrigin: true },
    },
  },
  build: { outDir: "dist" },
});
