import { describe, expect, it } from "vitest";
import {
  buildSessionSearchQuery,
  mapSessionSearchHits,
  normalizeSessionSearchHits,
} from "../lib/sessionSearch";

describe("sessionSearch helpers", () => {
  it("buildSessionSearchQuery returns null for empty/whitespace", () => {
    expect(buildSessionSearchQuery("")).toBeNull();
    expect(buildSessionSearchQuery("   ")).toBeNull();
  });

  it("buildSessionSearchQuery encodes q and omits default limit", () => {
    expect(buildSessionSearchQuery("hello world")).toBe("q=hello+world");
    expect(buildSessionSearchQuery("x", 20)).toBe("q=x");
    expect(buildSessionSearchQuery("x", 10)).toBe("q=x&limit=10");
  });

  it("normalizeSessionSearchHits drops malformed rows", () => {
    expect(normalizeSessionSearchHits(null)).toEqual([]);
    expect(normalizeSessionSearchHits({ session_id: "x" })).toEqual([]);
    expect(
      normalizeSessionSearchHits([
        { session_id: "a", snippet: " hit ", rank: 1.5 },
        { session_id: "", snippet: "nope", rank: 0 },
        null,
        { session_id: "b", snippet: 12, rank: "bad" },
      ]),
    ).toEqual([
      { session_id: "a", snippet: "hit", rank: 1.5 },
      { session_id: "b", snippet: "12", rank: 0 },
    ]);
  });

  it("mapSessionSearchHits resolves titles and falls back to Untitled", () => {
    expect(mapSessionSearchHits(undefined, {})).toEqual([]);
    expect(
      mapSessionSearchHits(
        [
          { session_id: "s1", snippet: "...alpha...", rank: -2 },
          { session_id: "s2", snippet: "", rank: 0 },
        ],
        { s1: "Alpha chat" },
      ),
    ).toEqual([
      { id: "s1", title: "Alpha chat", snippet: "...alpha...", rank: -2 },
      { id: "s2", title: "Untitled", snippet: "", rank: 0 },
    ]);
  });
});
