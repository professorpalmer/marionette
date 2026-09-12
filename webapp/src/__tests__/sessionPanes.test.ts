import { describe, expect, it } from "vitest";
import {
  MAX_DORMANT_SESSION_PANES,
  retainSessionPanes,
  shouldReleaseOutgoingSessionChrome,
} from "../components/conversation/sessionPanes";

describe("retainSessionPanes", () => {
  it("keeps the active id first and prior panes after", () => {
    expect(retainSessionPanes({
      prev: ["sess-a"],
      activeId: "sess-b",
    })).toEqual(["sess-b", "sess-a"]);
  });

  it("caps dormant panes and evicts the oldest idle id", () => {
    expect(retainSessionPanes({
      prev: ["sess-d", "sess-c", "sess-b", "sess-a"],
      activeId: "sess-e",
      maxDormant: 3,
    })).toEqual(["sess-e", "sess-d", "sess-c", "sess-b"]);
  });

  it("never evicts a busy mid-turn pane even when over the cap", () => {
    expect(retainSessionPanes({
      prev: ["busy-1", "busy-2", "busy-3"],
      activeId: "sess-now",
      busyIds: ["busy-1", "busy-2", "busy-3"],
      maxDormant: 1,
    })).toEqual(["sess-now", "busy-1", "busy-2", "busy-3"]);
  });

  it("dedupes and ignores blank ids", () => {
    expect(retainSessionPanes({
      prev: ["sess-a", "sess-a", "", "sess-b"],
      activeId: "sess-a",
    })).toEqual(["sess-a", "sess-b"]);
  });

  it("defaults the dormant cap", () => {
    expect(MAX_DORMANT_SESSION_PANES).toBe(3);
  });

  it("clears every pane when there is no active session", () => {
    expect(retainSessionPanes({
      prev: ["sess-a", "sess-b"],
      activeId: null,
    })).toEqual([]);
  });
});

describe("shouldReleaseOutgoingSessionChrome", () => {
  it("keeps chrome for a retained outgoing pane", () => {
    expect(shouldReleaseOutgoingSessionChrome({
      outgoingId: "sess-a",
      retainedIds: ["sess-b", "sess-a"],
    })).toBe(false);
  });

  it("releases chrome only after eviction", () => {
    expect(shouldReleaseOutgoingSessionChrome({
      outgoingId: "sess-old",
      retainedIds: ["sess-b", "sess-a"],
    })).toBe(true);
  });
});
