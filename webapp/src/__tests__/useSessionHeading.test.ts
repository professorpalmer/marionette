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
import { clearSWRCache, writeSWRCache } from "../lib/useStaleWhileRevalidate";

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

describe("useSessionHeading on a switch", () => {
  afterEach(() => clearSWRCache());

  // Rig 2026-11-14: on a cross-project switch the header kept the outgoing
  // title and project for about 750 ms, until the config read answered.
  it("shows the target row's title and project before any read answers", () => {
    writeSWRCache("sessions:/w/other", [
      { id: "sess-2", title: "Audit the harness", created: 1, repo: "/w/other", branch: "main" },
    ]);
    writeSWRCache("workspace:/w/other", { repo: "/w/other", branch: "dev", is_git: true });
    const { result, rerender } = renderHook(({ id }) => useSessionHeading(id, "/w/proj"), {
      initialProps: { id: "sess-1" },
    });
    rerender({ id: "sess-2" });
    expect(result.current).toEqual({ title: "Audit the harness", project: "other", branch: "dev" });
  });

  it("does not show the row's start branch when the repo was never read", () => {
    writeSWRCache("sessions:/w/other", [
      { id: "sess-2", title: "Audit the harness", created: 1, repo: "/w/other", branch: "main" },
    ]);
    expect(renderHook(() => useSessionHeading("sess-2", "/w/proj")).result.current.branch).toBe("");
  });

  // A nested session works in its own worktree, but it belongs to the parent
  // project. The header read "Marionette nest_kid-abc123" before.
  it("names a nested session's project from its parent project, not its worktree", () => {
    writeSWRCache("sessions:/w/proj", [
      { id: "kid", title: "kid", created: 1, repo: "/w/.pmharness-worktrees/nest_kid-abc123",
        workspace_root: "/w/proj", branch: "nest/kid-abc123", nest: { parent_id: "p1", name: "kid" } },
    ]);
    // The view stays on the project, so the workspace read names its branch.
    writeSWRCache("workspace:/w/proj", { repo: "/w/proj", branch: "main", is_git: true });
    expect(renderHook(() => useSessionHeading("kid", "/w/proj")).result.current)
      .toEqual({ title: "kid", project: "proj", branch: "nest/kid-abc123" });
  });
});
