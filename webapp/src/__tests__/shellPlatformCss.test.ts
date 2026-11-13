import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import tailwindConfig from "../../tailwind.config.js";
import { MONO_FONTS, MONO_STACK, SANS_FONTS } from "../lib/fontStacks";

const SRC = resolve(__dirname, "..");
const css = readFileSync(resolve(SRC, "index.css"), "utf8");
const cssRules = css.replace(/\/\*[\s\S]*?\*\//g, "");

function luminance(hex: string): number {
  const ch = (v: number) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b);
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function ruleBody(selector: string): string {
  const start = css.indexOf(`${selector} {`);
  expect(start, selector).toBeGreaterThan(-1);
  return css.slice(start, css.indexOf("}", start));
}

describe("scrollbars", () => {
  it("draws the 7px scrollbar with the webkit rules on every element", () => {
    expect(ruleBody("*::-webkit-scrollbar")).toMatch(/width:\s*7px/);
  });

  it("never sets scrollbar-width or scrollbar-color, which turn the webkit rules off", () => {
    // Chromium 121+ ignores ::-webkit-scrollbar on an element that has either
    // property. Only `scrollbar-width: none` (hide one element) is allowed.
    const widths = [...cssRules.matchAll(/scrollbar-width:\s*([^;]+);/g)].map((m) => m[1].trim());
    expect(widths.length).toBeGreaterThan(0);
    expect(widths.every((v) => v === "none")).toBe(true);
    expect(cssRules).not.toMatch(/scrollbar-color:/);
  });
});

describe("sidebar text on glass", () => {
  // Median material pixel under the rail at glass 60, from a native window
  // capture (dark wallpaper). #e0a45a measured 4.48:1 there.
  const GLASS_60_MATERIAL = "#424445";

  it("lifts muted, faint and accent text to 4.5:1 over the material", () => {
    const lifted = [
      ruleBody(":root[data-marionette-glass] .shell-sidebar :is(.text-muted, .text-faint)"),
      ruleBody(":root[data-marionette-glass] .shell-sidebar .text-accent"),
    ].map((body) => body.match(/color:\s*(#[0-9a-f]{6})/i)?.[1] ?? "");
    for (const color of lifted) {
      expect(color).toMatch(/^#/);
      expect(contrast(color, GLASS_60_MATERIAL)).toBeGreaterThanOrEqual(4.5);
    }
  });
});

describe("font stacks", () => {
  it("gives mono text a Windows font", () => {
    expect(MONO_FONTS).toEqual(expect.arrayContaining(["Cascadia Mono", "Consolas"]));
    expect(MONO_STACK).toContain('"Cascadia Mono", Consolas');
  });

  it("is the one source for Tailwind", () => {
    const fonts = tailwindConfig.theme?.extend?.fontFamily as Record<string, unknown>;
    expect(fonts.mono).toBe(MONO_FONTS);
    expect(fonts.sans).toBe(SANS_FONTS);
  });

  it("is not copied into other source files", () => {
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const path = join(dir, name);
        if (statSync(path).isDirectory()) {
          if (name !== "__tests__") walk(path);
        } else if (/\.(tsx?|css)$/.test(name)) {
          files.push(path);
        }
      }
    };
    walk(SRC);
    const copies = files
      .filter((f) => !f.endsWith("fontStacks.ts"))
      .filter((f) => /\bMenlo\b|SFMono-Regular|BlinkMacSystemFont/.test(readFileSync(f, "utf8")))
      .map((f) => relative(SRC, f));
    expect(copies).toEqual([]);
  });
});
