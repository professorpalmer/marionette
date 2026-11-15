import { describe, expect, it } from "vitest";
import {
  formatContextWindow,
  formatPrice,
  modelFactParts,
  parseModelFacts,
  tierForSpec,
} from "../lib/pilotModelFacts";

describe("parseModelFacts", () => {
  it("reads price, context and image input from catalog rows", () => {
    const facts = parseModelFacts({
      all: [
        { spec: "openrouter:deepseek/deepseek-v4.1-flash", price_in: 0.3, price_out: 1.2, context_window: 1048576, image_input: true, billing: "metered" },
        { spec: "openrouter:z-ai/glm-5.3-flash", pricing: { prompt: 0.1, completion: 0.4 }, context_window: null, image_input: false },
        { spec: "cursor:composer-2.5", pricing: {}, image_input: null },
        { model: "no-spec" },
        "garbage",
      ],
    });
    expect(facts.size).toBe(3);
    expect(facts.get("openrouter:deepseek/deepseek-v4.1-flash")).toEqual({
      price: { kind: "rate", input: 0.3, output: 1.2 }, contextWindow: 1048576, imageInput: true, tier: null,
    });
    expect(facts.get("openrouter:z-ai/glm-5.3-flash")).toMatchObject({
      price: { kind: "rate", input: 0.1, output: 0.4 }, contextWindow: null, imageInput: false,
    });
    expect(facts.get("cursor:composer-2.5")).toMatchObject({ price: { kind: "unknown" }, imageInput: null });
  });

  it("names a plan-billed row Plan, whatever price the row carries", () => {
    const facts = parseModelFacts({
      all: [
        { spec: "opencode-go:mimo-v2.6-flash", billing: "plan", context_window: 1048576, image_input: true },
        { spec: "openai-codex:gpt-6-luna", billing: "plan", price_in: 0.1, price_out: 0.5 },
      ],
    });
    expect(facts.get("opencode-go:mimo-v2.6-flash")?.price).toEqual({ kind: "plan" });
    expect(facts.get("openai-codex:gpt-6-luna")?.price).toEqual({ kind: "plan" });
  });

  it("returns an empty map for a malformed payload", () => {
    expect(parseModelFacts(null).size).toBe(0);
    expect(parseModelFacts({ all: "x" }).size).toBe(0);
  });
});

describe("tierForSpec", () => {
  it("names tiers only from the model id itself", () => {
    expect(tierForSpec("openrouter:deepseek/deepseek-v4.1-flash:free")).toBe("Free tier");
    expect(tierForSpec("openrouter:deepseek/deepseek-v4.1-flash:nitro")).toBe("Fast routing");
    expect(tierForSpec("openrouter:deepseek/deepseek-v4.1-flash")).toBeNull();
    expect(tierForSpec("openrouter:deepseek/deepseek-v4.1-flash:unheard-of")).toBeNull();
    expect(tierForSpec("local:ext/bonsai")).toBe("Local");
    expect(tierForSpec("opencode-go:glm-5.3")).toBeNull();
  });
});

describe("modelFactParts", () => {
  it("leaves out each unknown fact, never a guess", () => {
    expect(modelFactParts({ price: { kind: "unknown" }, contextWindow: null, imageInput: null, tier: null })).toEqual([]);
  });

  it("formats known facts", () => {
    expect(modelFactParts({ price: { kind: "rate", input: 0.038, output: 0.5 }, contextWindow: 1048576, imageInput: false, tier: "Batch" })).toEqual([
      "$0.038 / $0.50 per MTok", "1.05M context", "Text only", "Batch",
    ]);
  });

  it("says Plan for a plan-billed model", () => {
    expect(modelFactParts({ price: { kind: "plan" }, contextWindow: 1048576, imageInput: true, tier: null })).toEqual([
      "Plan", "1.05M context", "Vision",
    ]);
  });

  it("formats prices and windows", () => {
    expect(formatPrice(0)).toBe("$0");
    expect(formatPrice(2)).toBe("$2.00");
    expect(formatPrice(0.09999999999999999)).toBe("$0.10");
    expect(formatPrice(0.435)).toBe("$0.44");
    expect(formatPrice(0.0399)).toBe("$0.04");
    expect(formatPrice(15)).toBe("$15");
    expect(formatPrice(12.5)).toBe("$12.5");
    expect(formatPrice(150)).toBe("$150");
    expect(formatContextWindow(262144)).toBe("262k");
    expect(formatContextWindow(1_000_000)).toBe("1M");
  });
});
