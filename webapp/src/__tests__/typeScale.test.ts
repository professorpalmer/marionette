import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = resolve(__dirname, "..");

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "__tests__" ? [] : sources(path);
    return /\.(tsx?|css)$/.test(name) ? [path] : [];
  });
}

// Every text size comes from the ui-N scale in tailwind.config.js, so the
// sizing policy lives in one place. Ad-hoc px sizes had spread to ~1,000 sites.
describe("closed type scale", () => {
  it("has no ad-hoc px text sizes outside the ui-N tokens", () => {
    const hits = sources(SRC).flatMap((file) => {
      const text = readFileSync(file, "utf8");
      const found = [...text.matchAll(/text-\[\d+(?:\.\d+)?px\]|font-size:\s*\d+(?:\.\d+)?px/g)];
      return found.map((m) => `${relative(SRC, file)}: ${m[0]}`);
    });
    expect(hits).toEqual([]);
  });
});
