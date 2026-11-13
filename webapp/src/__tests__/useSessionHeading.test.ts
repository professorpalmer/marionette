import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", () => ({
  api: {
    // The reads stay pending: this is the first paint after a reload.
    sessions: () => new Promise(() => {}),
    getWorkspace: () => new Promise(() => {}),
  },
}));

import { projectName, rememberHeadingTitle, useSessionHeading } from "../components/conversation/useSessionHeading";

afterEach(() => localStorage.clear());

describe("projectName", () => {
  it.each([
    ["/Users/a/Projects/marionette", "marionette"],
    ["/Users/a/Projects/marionette/", "marionette"],
    ["C:\\Users\\a\\proj", "proj"],
    ["", ""],
  ])("%s -> %s", (repo, name) => {
    expect(projectName(repo)).toBe(name);
  });
});

describe("useSessionHeading before its first read", () => {
  // Rig L0350: for about 200 ms after a reload the header read "Marionette".
  it("shows the last known title of the session", () => {
    rememberHeadingTitle("sess-1", "Fix the calc bug");
    expect(renderHook(() => useSessionHeading("sess-1", "/w/proj")).result.current.title).toBe("Fix the calc bug");
    // The session id can arrive after the first paint.
    expect(renderHook(() => useSessionHeading(null, "/w/proj")).result.current.title).toBe("Fix the calc bug");
  });

  it("does not show the title of another session", () => {
    rememberHeadingTitle("sess-1", "Fix the calc bug");
    expect(renderHook(() => useSessionHeading("sess-2", "/w/proj")).result.current.title).toBe("");
  });
});
