import { describe, expect, it } from "vitest";
import {
  REASONING_LEVELS,
  labelForEffort,
  showReasoningEffort,
} from "../lib/reasoningSupport";

describe("labelForEffort", () => {
  it("labels xhigh as Ultra", () => {
    expect(labelForEffort("xhigh")).toBe("Ultra");
    expect(REASONING_LEVELS.some((l) => l.value === "xhigh" && l.label === "Ultra")).toBe(true);
  });
});

describe("showReasoningEffort", () => {
  it("fails open when the map is missing or empty", () => {
    expect(showReasoningEffort(undefined, "openrouter:stealth/ox-alpha")).toBe(true);
    expect(showReasoningEffort(null, "anthropic:claude-haiku-4-5")).toBe(true);
    expect(showReasoningEffort({}, "openai-codex:gpt-5.6-luna")).toBe(true);
  });

  it("honors an explicit false and does not fail open on a missing key", () => {
    const support = {
      "openai-codex:gpt-5.6-luna": true,
      "anthropic:claude-haiku-4-5": false,
      "opencode-go:ox-alpha-free": false,
      "openrouter:stealth/ox-alpha": true,
    };
    expect(showReasoningEffort(support, "openai-codex:gpt-5.6-luna")).toBe(true);
    expect(showReasoningEffort(support, "anthropic:claude-haiku-4-5")).toBe(false);
    expect(showReasoningEffort(support, "opencode-go:ox-alpha-free")).toBe(false);
    expect(showReasoningEffort(support, "openrouter:stealth/ox-alpha")).toBe(true);
    expect(showReasoningEffort(support, "opencode-go:muse-spark-1.2-contributor")).toBe(false);
  });
});
