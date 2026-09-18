/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import babel from "@rolldown/plugin-babel";
import react, { reactCompilerPreset } from "@vitejs/plugin-react";
import { cspMetaTag } from "./src/lib/csp.ts";

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

// CSP injection. The packaged shell loads dist/index.html over file://, where
// Electron's webRequest never fires -- so a response header is not an option and
// the policy has to ride in the document. `apply: "build"` keeps the dev server
// (and "Live UI (Vite HMR)") uncsp'd on purpose: Vite's react-refresh preamble
// is an inline script, and a strict script-src would blank the dev page. The
// shipped artifact is the build, and that is what is guarded.
function cspPlugin() {
  return {
    name: "marionette-csp",
    apply: "build" as const,
    transformIndexHtml(html: string) {
      return html.replace("</head>", `  ${cspMetaTag()}\n  </head>`);
    },
  };
}

// Dev proxy: React (5273) -> Python harness backend (8799). In a packaged
// Electron build the same transport calls route through IPC instead -- see
// src/transport.ts. This keeps the app backend-agnostic, not web-locked.
export default defineConfig({
  // base "./" => relative asset paths so the build loads under file:// in Electron
  base: "./",
  plugins: [cspPlugin(), react(), babel({ presets: [compilerPreset()] })],
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
