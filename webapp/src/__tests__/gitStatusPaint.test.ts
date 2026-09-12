import { describe, expect, it } from "vitest";
import { gitStatusPaintOnRepoChange } from "../lib/gitStatusPaint";

describe("gitStatusPaintOnRepoChange", () => {
  it("keeps last-good lists instead of blanking a known repo", () => {
    const cached = {
      files: [{ status: "M", path: "README.md" }],
      branches: [{ name: "dev", active: true }],
    };
    expect(gitStatusPaintOnRepoChange(cached)).toEqual({
      kind: "keep",
      snapshot: cached,
    });
  });

  it("blanks only when that repo has never been loaded", () => {
    expect(gitStatusPaintOnRepoChange(undefined)).toEqual({ kind: "blank" });
  });
});
