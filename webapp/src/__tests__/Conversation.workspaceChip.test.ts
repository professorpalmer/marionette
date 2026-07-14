import { describe, expect, it } from "vitest";
import { isWorkspaceOpenLeaseExhausted } from "../components/Conversation";

/**
 * WorkspaceChip lease-exhausted detector — mirrors LeftRail.isLeaseExhaustedError
 * contracts without mounting the full Conversation UI.
 */
describe("isWorkspaceOpenLeaseExhausted", () => {
  it("detects postJSON 409 and lease_exhausted code", () => {
    expect(isWorkspaceOpenLeaseExhausted(new Error("/api/workspace/open -> 409"))).toBe(true);
    expect(isWorkspaceOpenLeaseExhausted(new Error("/api/sessions/switch -> 409"))).toBe(true);
    expect(isWorkspaceOpenLeaseExhausted(new Error("/api/sessions/create -> 409"))).toBe(true);
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
});
