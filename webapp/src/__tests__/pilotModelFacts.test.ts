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
        { spec: "openrouter:deepseek/deepseek-v4.1-flash", price_in: 0.3, price_out: 1.2, context_window: 1048576, image_input: true },
        { spec: "openrouter:z-ai/glm-5.3-flash", pricing: { prompt: 0.1, completion: 0.4 }, context_window: null, image_input: false },
        { spec: "cursor:composer-2.5", pricing: {}, image_input: null },
        { model: "no-spec" },
        "garbage",
      ],
    });
    expect(facts.size).toBe(3);
    expect(facts.get("openrouter:deepseek/deepseek-v4.1-flash")).toEqual({
      priceIn: 0.3, priceOut: 1.2, contextWindow: 1048576, imageInput: true, tier: null,
    });
    expect(facts.get("openrouter:z-ai/glm-5.3-flash")).toMatchObject({ priceIn: 0.1, priceOut: 0.4, contextWindow: null, imageInput: false });
    expect(facts.get("cursor:composer-2.5")).toMatchObject({ priceIn: null, priceOut: null, imageInput: null });
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
  it("says unknown for each missing fact, never a guess", () => {
    expect(modelFactParts({ priceIn: null, priceOut: 1, contextWindow: null, imageInput: null, tier: null })).toEqual([
      "Price unknown", "Context unknown", "Vision unknown",
    ]);
  });

  it("formats known facts", () => {
    expect(modelFactParts({ priceIn: 0.038, priceOut: 0.5, contextWindow: 1048576, imageInput: false, tier: "Batch" })).toEqual([
      "$0.038 / $0.50 per MTok", "1.05M context", "Text only", "Batch",
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
