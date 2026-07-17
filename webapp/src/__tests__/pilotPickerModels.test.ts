import { describe, expect, it } from "vitest";
import {
  filterPilotModels,
  groupPilotModelsByProvider,
  organizePilotModels,
  pinCurrentPilot,
  providerOf,
} from "../lib/pilotPickerModels";

const MODELS = [
  "anthropic:claude-opus-4-8",
  "anthropic:claude-sonnet-4-6",
  "openai:gpt-5.2",
  "openrouter:z-ai/glm-5.2",
  "stub-oracle",
];

describe("providerOf", () => {
  it("returns the prefix before ':'", () => {
    expect(providerOf("anthropic:claude-opus-4-8")).toBe("anthropic");
    expect(providerOf("stub-oracle")).toBe("stub-oracle");
  });
});

describe("filterPilotModels", () => {
  it("matches model id substring", () => {
    expect(filterPilotModels(MODELS, "opus")).toEqual([
      "anthropic:claude-opus-4-8",
    ]);
  });

  it("matches provider prefix", () => {
    expect(filterPilotModels(MODELS, "openrouter")).toEqual([
      "openrouter:z-ai/glm-5.2",
    ]);
    expect(filterPilotModels(MODELS, "anthropic")).toEqual([
      "anthropic:claude-opus-4-8",
      "anthropic:claude-sonnet-4-6",
    ]);
  });

  it("returns all models when query is blank", () => {
    expect(filterPilotModels(MODELS, "  ")).toEqual(MODELS);
  });
});

describe("pinCurrentPilot", () => {
  it("pins the current driver at the top", () => {
    expect(pinCurrentPilot(MODELS, "openai:gpt-5.2")[0]).toBe("openai:gpt-5.2");
  });

  it("leaves order alone when current is missing", () => {
    expect(pinCurrentPilot(MODELS, "missing:model")).toEqual(MODELS);
  });
});

describe("groupPilotModelsByProvider", () => {
  it("groups by provider prefix", () => {
    const groups = groupPilotModelsByProvider(MODELS);
    expect(groups.map((g) => g.provider)).toEqual([
      "anthropic",
      "openai",
      "openrouter",
      "stub-oracle",
    ]);
    expect(groups[0].items).toEqual([
      "anthropic:claude-opus-4-8",
      "anthropic:claude-sonnet-4-6",
    ]);
  });
});

describe("organizePilotModels", () => {
  it("pins current above provider groups", () => {
    const { current, groups } = organizePilotModels(
      MODELS,
      "openai:gpt-5.2",
      "",
    );
    expect(current).toBe("openai:gpt-5.2");
    expect(groups.map((g) => g.provider)).toEqual([
      "anthropic",
      "openrouter",
      "stub-oracle",
    ]);
  });

  it("filters then pins", () => {
    const { current, groups } = organizePilotModels(
      MODELS,
      "anthropic:claude-opus-4-8",
      "claude",
    );
    expect(current).toBe("anthropic:claude-opus-4-8");
    expect(groups).toEqual([
      { provider: "anthropic", items: ["anthropic:claude-sonnet-4-6"] },
    ]);
  });
});
