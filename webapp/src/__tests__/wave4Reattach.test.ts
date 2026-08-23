import { describe, expect, it } from "vitest";
import { reattachSessionStateFailureDecision } from "../components/conversation/sessionHydrate";

describe("reattachSessionStateFailureDecision", () => {
  it("retries then polls without optimistic busy", () => {
    expect(reattachSessionStateFailureDecision({ attempt: 1, maxAttempts: 2 })).toBe("retry");
    expect(reattachSessionStateFailureDecision({ attempt: 2, maxAttempts: 2 })).toBe("poll_only");
  });
});
