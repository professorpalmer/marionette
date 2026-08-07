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

  it("keeps full registry id for explicit pins", () => {
    expect(
      displayModelId("agentic/meta/muse-spark-1.1", { policy: "explicit_pin" }),
    ).toBe("agentic/meta/muse-spark-1.1");
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
