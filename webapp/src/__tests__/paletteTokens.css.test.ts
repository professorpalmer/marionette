import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const src = (rel: string) => resolve(__dirname, "..", rel);
const read = (rel: string) => readFileSync(src(rel), "utf8");

// Files whose palette and motion are closed. Extend as other files are swept.
const CLOSED_FILES = [
  "index.css",
  "components/RightDock.tsx",
  "components/leftRailPrimitives.tsx",
  "components/FileTree.tsx",
  "components/PluginInstallModal.tsx",
  "components/PluginsLibrary.tsx",
  "components/SettingsPane.tsx",
  "components/ModelsSettingsPage.tsx",
  "components/DiffReviewPane.tsx",
  "components/CheckpointsPane.tsx",
  "components/MemoryPane.tsx",
  "components/conversation/ImageLightbox.tsx",
  "components/ui/Switch.tsx",
  "components/TranscriptList.tsx",
  "components/conversation/ComposerDock.tsx",
  "components/StatusBar.tsx",
  "components/LeftRail.tsx",
  "components/RightPane.tsx",
  "components/StatePane.tsx",
  "components/EconomicsPane.tsx",
  "components/FileEditorPane.tsx",
  "components/PilotPicker.tsx",
];

const FORBIDDEN: Array<[string, RegExp]> = [
  ["raw red/rose/pink status color", /\b(?:text|bg|border|from|via|to|ring|outline)-(?:red|rose|pink)-\d/],
  ["raw amber/yellow/orange status color", /\b(?:text|bg|border|from|via|to|ring|outline)-(?:amber|yellow|orange)-\d/],
  ["raw green/emerald status color", /\b(?:text|bg|border|from|via|to|ring|outline)-(?:green|emerald)-\d/],
  ["rgba status literal", /rgba\(\s*(?:63,\s*185,\s*80|154,\s*168,\s*255)/],
  ["undefined --accent var", /var\(--accent\b/],
  ["invalid py-0.2", /\bpy-0\.2\b/],
  ["tailwindcss-animate class without the plugin", /\b(?:animate-in|animate-out|fade-in(?:-\d+)?|slide-in-from-[a-z]+(?:-\d+)?|zoom-in-\d+)\b/],
  ["transition-all", /\btransition-all\b/],
  ["raw ms duration in a transition/animation", /(?:transition|animation)(?:-duration)?:[^;]*(?<![.\d])[1-9]\d*ms\b/],
  ["inline <style> keyframes", /<style>\{`[\s\S]*@keyframes/],
];

describe("closed palette and motion vocabulary", () => {
  it.each(CLOSED_FILES)("%s uses only palette and motion tokens", (rel) => {
    const text = read(rel);
    const hits = FORBIDDEN.filter(([, re]) => re.test(text)).map(([name]) => name);
    expect(hits).toEqual([]);
  });

  it("paints the live dot and review badge with the accent token", () => {
    const css = read("index.css");
    expect(css).not.toMatch(/#9aa8ff/i);
    expect(css).toMatch(/\.right-pane-live \{[^}]*background: theme\('colors\.accent'\)/);
    expect(css).toMatch(/\.right-pane-badge \{[^}]*background: theme\('colors\.accent'\)/);
  });

  it("drops the unused Vite template stylesheet", () => {
    expect(existsSync(src("App.css"))).toBe(false);
  });

  it("defines one set of duration and easing tokens", () => {
    const config = readFileSync(resolve(__dirname, "../../tailwind.config.js"), "utf8");
    expect(config).toMatch(/transitionDuration: \{ DEFAULT: "120ms", fast: "120ms", base: "200ms" \}/);
    expect(config).toMatch(/transitionTimingFunction: \{ DEFAULT: "([^"]+)", base: "\1" \}/);
  });

  it("keeps DiffReviewPane keyframes in index.css", () => {
    const css = read("index.css");
    expect(css).toMatch(/@keyframes diff-apply-sweep/);
    expect(css).toMatch(/@keyframes diff-applied-pop/);
    const pane = read("components/DiffReviewPane.tsx");
    expect(pane).toContain("diff-apply-sweep");
    expect(pane).toContain("diff-applied-pop");
    expect(pane).not.toMatch(/style=\{\{\s*background:/);
  });
});
