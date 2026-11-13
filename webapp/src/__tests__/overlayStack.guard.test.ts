import { describe, expect, it } from "vitest";
import cssSource from "../index.css?raw";
import pkg from "../../package.json";

// One overlay stack: Base UI owns focus, Escape, outside press and focus
// return. This guard fails when a component builds that behavior by hand again.
const sources = import.meta.glob<string>(["../components/**/*.tsx", "../lib/**/*.{ts,tsx}"], {
  query: "?raw",
  import: "default",
  eager: true,
});

const RULES: Array<{ name: string; pattern: RegExp }> = [
  { name: "hand-set dialog role", pattern: /role=["{]["']?(dialog|alertdialog)\b/ },
  { name: "hand-set aria-modal", pattern: /aria-modal=/ },
  { name: "hand-set menu role", pattern: /role=["{]["']?menu(item(radio|checkbox)?)?["'}]/ },
  { name: "native <dialog> element", pattern: /<dialog[\s>]/ },
  { name: "document or window outside-press listener", pattern: /(document|window)\.addEventListener\(\s*["'](mousedown|pointerdown|click)["']/ },
  { name: "removed overlay helper", pattern: /overlayFocus|overlayPortal|OverlayPortal|useOverlayFocus|moveMenuFocus/ },
];

const OWNER = "../components/ui/overlay.tsx";

describe("overlay stack guard", () => {
  it("finds the component sources", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50);
    expect(sources[OWNER]).toContain("@base-ui/react/dialog");
  });

  it("has no hand-rolled dialog, menu, or outside-press code outside ui/overlay.tsx", () => {
    const hits: string[] = [];
    for (const [file, src] of Object.entries(sources)) {
      if (file === OWNER) continue;
      for (const rule of RULES) {
        if (rule.pattern.test(src)) hits.push(`${file}: ${rule.name}`);
      }
    }
    expect(hits).toEqual([]);
  });

  it("pins one primitive library and no second one", () => {
    const deps = { ...pkg.dependencies, ...pkg.devDependencies } as Record<string, string>;
    expect(deps["@base-ui/react"]).toBe("1.9.0");
    const others = Object.keys(deps).filter((name) =>
      /^(@radix-ui\/|radix-ui$|@headlessui\/|react-aria|@ark-ui\/|@reach\/)/.test(name));
    expect(others).toEqual([]);
  });

  it("keeps the starting-style fade and drops the old instant-close hook", () => {
    expect(cssSource).toContain("[data-starting-style]");
    expect(cssSource).not.toContain("[data-instant]");
  });
});
