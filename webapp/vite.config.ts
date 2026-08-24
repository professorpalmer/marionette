/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import babel from "@rolldown/plugin-babel";
import react, { reactCompilerPreset } from "@vitejs/plugin-react";

/**
 * Hermes-scoped compiler preset: only parse modules that contain JSX or a
 * react import. The default filter matches any PascalCase / use* declaration
 * and would Babel-parse every TS file.
 */
function compilerPreset() {
  const preset = reactCompilerPreset();
  const filter = preset.rolldown.filter ?? { code: /(?:)/ };
  filter.code = /\/>|<\/|from\s*['"][^'"]*react/;
  preset.rolldown.filter = filter;
  return preset;
}

// Dev proxy: React (5273) -> Python harness backend (8799). In a packaged
// Electron build the same transport calls route through IPC instead -- see
// src/transport.ts. This keeps the app backend-agnostic, not web-locked.
export default defineConfig({
  // base "./" => relative asset paths so the build loads under file:// in Electron
  base: "./",
  plugins: [react(), babel({ presets: [compilerPreset()] })],
  server: {
    host: "127.0.0.1",
    port: 5273,
    proxy: {
      "/api": { target: "http://127.0.0.1:8799", changeOrigin: true },
    },
  },
  build: { outDir: "dist" },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/__tests__/setup.ts"],
    css: true,
    include: [
      "src/__tests__/**/*.{test,spec}.{ts,tsx}",
      "src/state/**/*.test.ts",
    ],
  },
});
