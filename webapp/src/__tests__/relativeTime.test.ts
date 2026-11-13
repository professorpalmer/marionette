import { describe, expect, it } from "vitest";
import { compactAge } from "../lib/relativeTime";

describe("compactAge", () => {
  const now = Date.UTC(2026, 10, 13, 12, 0, 0);
  const ago = (s: number) => now / 1000 - s;
  it("gives a short age for recent rows", () => {
    expect(compactAge(ago(10), now)).toBe("now");
    expect(compactAge(ago(5 * 60), now)).toBe("5m");
    expect(compactAge(ago(3 * 3600), now)).toBe("3h");
    expect(compactAge(ago(2 * 86400), now)).toBe("2d");
    expect(compactAge(ago(15 * 86400), now)).toBe("2w");
  });
  it("gives a date for old rows and nothing for unknown times", () => {
    expect(compactAge(ago(60 * 86400), now)).toBe("Sep 14");
    expect(compactAge(Date.UTC(2025, 2, 4, 12) / 1000, now)).toBe("Mar 4 2025");
    expect(compactAge(0, now)).toBe("");
    expect(compactAge(undefined, now)).toBe("");
    expect(compactAge(ago(-3600), now)).toBe("");
  });
});
