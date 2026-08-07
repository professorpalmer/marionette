import { describe, expect, it } from "vitest";
import {
  formatWorkspaceOpenLeaseExhaustedMessage,
  isWorkspaceOpenLeaseExhausted,
} from "../components/Conversation";
import {
  isWorkspaceHomeActive,
  WORKSPACE_CHIP_POPOVER_CLASS,
  WORKSPACE_CHIP_ROW_CLASS,
  workspaceChipRecents,
} from "../components/conversation/WorkspaceChip";

/**
 * WorkspaceChip lease-exhausted detector — mirrors LeftRail.isLeaseExhaustedError
 * contracts without mounting the full Conversation UI.
 */
describe("WorkspaceChip Home pin + recents", () => {
  const home = "C:\\Users\\me\\.pmharness\\home";

  it("filters Home and the active repo out of Recents", () => {
    const recents = [
      home,
      "C:\\Projects\\alpha",
      "C:\\Projects\\beta",
      "c:/Users/me/.pmharness/home",
    ];
    expect(workspaceChipRecents(recents, "C:\\Projects\\beta", home)).toEqual([
      "C:\\Projects\\alpha",
    ]);
  });

  it("marks Home active for empty repo or when repo equals home", () => {
    expect(isWorkspaceHomeActive(undefined, home)).toBe(true);
    expect(isWorkspaceHomeActive("", home)).toBe(true);
    expect(isWorkspaceHomeActive(home, home)).toBe(true);
    expect(isWorkspaceHomeActive("c:/Users/me/.pmharness/home", home)).toBe(true);
    expect(isWorkspaceHomeActive("C:\\Projects\\alpha", home)).toBe(false);
    expect(isWorkspaceHomeActive("", undefined)).toBe(false);
  });

  it("overflow row classes keep long paths inside the popover", () => {
    // Contract for WorkspaceChip popover markup (w-64): clip the panel and
    // give each recent row a bounded flex child so truncate can ellipsize.
    // Assert exported class strings — Vitest unit tests must typecheck under
    // tsc -b without Node fs/path/__dirname.
    expect(WORKSPACE_CHIP_POPOVER_CLASS).toMatch(/w-64[^"]*overflow-hidden/);
    expect(WORKSPACE_CHIP_ROW_CLASS).toMatch(/max-w-full min-w-0 overflow-hidden/);
  });
});

describe("isWorkspaceOpenLeaseExhausted", () => {
  it("requires lease_exhausted code (not bare 409)", () => {
    expect(isWorkspaceOpenLeaseExhausted(new Error("/api/workspace/open -> 409"))).toBe(false);
    expect(isWorkspaceOpenLeaseExhausted(new Error("/api/sessions/switch -> 409"))).toBe(false);
    expect(isWorkspaceOpenLeaseExhausted(new Error("/api/sessions/create -> 409"))).toBe(false);
    expect(isWorkspaceOpenLeaseExhausted({ code: "lease_exhausted", error: "busy" })).toBe(true);
    expect(isWorkspaceOpenLeaseExhausted(new Error("lease_exhausted: all slots busy"))).toBe(true);
    expect(
      isWorkspaceOpenLeaseExhausted(new Error("session runner lease exhausted: all concurrent sessions are busy")),
    ).toBe(true);
    expect(isWorkspaceOpenLeaseExhausted(new Error("/api/workspace/open -> 500"))).toBe(false);
    expect(isWorkspaceOpenLeaseExhausted(new Error("/api/other -> 409"))).toBe(false);
  });

  it("rejects unrelated 409 conflicts", () => {
    expect(isWorkspaceOpenLeaseExhausted({ status: 409 })).toBe(false);
    expect(isWorkspaceOpenLeaseExhausted({ status: 409, error: "pilot busy, try again" })).toBe(false);
    expect(isWorkspaceOpenLeaseExhausted({ status: 409, error: "Path already exists" })).toBe(false);
    expect(isWorkspaceOpenLeaseExhausted({ status: 409, code: "busy" })).toBe(false);
    expect(isWorkspaceOpenLeaseExhausted({ status: 409, code: "lease_exhausted" })).toBe(true);
  });

  it("formatWorkspaceOpenLeaseExhaustedMessage names busy sessions and capacity", () => {
    expect(
      formatWorkspaceOpenLeaseExhaustedMessage({
        code: "lease_exhausted",
        max_concurrent: 3,
        active_count: 3,
        busy_session_titles: ["One", "Two"],
      }),
    ).toMatch(/3\/3.*"One".*"Two"/s);
  });
});
