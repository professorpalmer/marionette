import { describe, expect, it } from "vitest";
import { normalizeRepoPath, repoPathsEqual } from "../lib/pathNormalize";

describe("pathNormalize", () => {
  it("matches Windows slash and drive-letter case drift", () => {
    expect(repoPathsEqual("C:\\Foo\\Bar", "c:/foo/bar")).toBe(true);
    expect(repoPathsEqual("C:/Foo/Bar/", "c:\\foo\\bar")).toBe(true);
  });

  it("normalizes to forward slashes with lowercase drive", () => {
    expect(normalizeRepoPath("C:\\Foo\\Bar")).toBe("c:/Foo/Bar");
    expect(normalizeRepoPath("c:/foo/bar/")).toBe("c:/foo/bar");
  });

  it("rejects unrelated roots", () => {
    expect(repoPathsEqual("C:\\Foo\\Bar", "C:\\Foo\\Baz")).toBe(false);
    expect(repoPathsEqual("", "c:/foo")).toBe(false);
  });
});
