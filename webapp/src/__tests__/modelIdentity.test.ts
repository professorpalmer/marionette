import { describe, expect, it } from "vitest";
import {
  collapseEnginePrefixes,
  displayModelId,
  isEngineOnlyModelId,
  modelIdsEqual,
  stripEnginePrefixes,
} from "../lib/modelIdentity";

describe("modelIdentity display helpers", () => {
  it("strips repeated agentic/native prefixes idempotently", () => {
    expect(stripEnginePrefixes("agentic/agentic/deepseek/deepseek-v4-pro")).toBe(
      "deepseek/deepseek-v4-pro",
    );
    expect(collapseEnginePrefixes("agentic/agentic/deepseek/deepseek-v4-pro")).toBe(
      "agentic/deepseek/deepseek-v4-pro",
    );
  });

  it("strips engine prefixes for pinned and auto-routed badges alike", () => {
    expect(
      displayModelId("agentic/meta/muse-spark-1.1", { policy: "explicit_pin" }),
    ).toBe("meta/muse-spark-1.1");
    expect(
      displayModelId("agentic/openai-codex/gpt-5.6-luna", { policy: "explicit_pin" }),
    ).toBe(
      displayModelId("agentic/openai-codex/gpt-5.6-luna", { policy: "balanced" }),
    );
  });

  it("strips engine prefixes for auto-routed badges including doubles", () => {
    expect(
      displayModelId("agentic/agentic/deepseek/deepseek-v4-pro", {
        policy: "balanced",
        adapterFallback: "agentic",
      }),
    ).toBe("deepseek/deepseek-v4-pro");
  });

  it("never treats bare agentic/native as a display model", () => {
    expect(isEngineOnlyModelId("")).toBe(true);
    expect(isEngineOnlyModelId("agentic")).toBe(true);
    expect(isEngineOnlyModelId("native")).toBe(true);
    expect(isEngineOnlyModelId("agentic/z-ai/glm-5.2")).toBe(false);
    expect(displayModelId("agentic", { adapterFallback: "agentic" })).toBe("");
    expect(displayModelId("", { adapterFallback: "agentic" })).toBe("");
    expect(displayModelId("", { adapterFallback: "openrouter" })).toBe("openrouter");
  });

  it("compares identity across prefix shapes", () => {
    expect(
      modelIdsEqual(
        "agentic/deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-pro",
      ),
    ).toBe(true);
  });
});
