import { describe, expect, it } from "vitest";
import {
  filterPilotModels,
  groupPilotModelsByProvider,
  fallbackPilot,
  modelLabelOf,
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

  it("matches Ox Alpha friendly name to the wire id", () => {
    const zen = ["opencode-zen:x-preview-f-free", "opencode-zen:big-pickle"];
    expect(filterPilotModels(zen, "Ox Alpha")).toEqual([
      "opencode-zen:x-preview-f-free",
    ]);
    expect(modelLabelOf("opencode-zen:x-preview-f-free")).toBe("Ox Alpha Free");
  });

  it("matches Ox Alpha friendly name for OpenCode Go specs", () => {
    const goModels = [
      "opencode-go:ox-alpha-free",
      "opencode-go:deepseek-v4-flash",
    ];
    expect(filterPilotModels(goModels, "Ox Alpha")).toEqual([
      "opencode-go:ox-alpha-free",
    ]);
    expect(modelLabelOf("opencode-go:ox-alpha-free")).toBe("Ox Alpha Free");
  });

  it("matches backend catalog labels for non-Ox models", () => {
    const models = [
      "opencode-zen:big-pickle",
      "opencode-zen:mimo-v2.5-free",
    ];
    const labels = {
      "opencode-zen:big-pickle": "Big Pickle",
      "opencode-zen:mimo-v2.5-free": "MiMo-V2.5 Free",
    };
    expect(filterPilotModels(models, "pickle", labels)).toEqual([
      "opencode-zen:big-pickle",
    ]);
    expect(filterPilotModels(models, "mimo", labels)).toEqual([
      "opencode-zen:mimo-v2.5-free",
    ]);
    expect(modelLabelOf("opencode-zen:big-pickle", labels)).toBe("Big Pickle");
    expect(modelLabelOf("opencode-zen:mimo-v2.5-free", labels)).toBe("MiMo-V2.5 Free");
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

describe("fallbackPilot", () => {
  it("keeps the current spec when it is still listed", () => {
    expect(fallbackPilot(MODELS, "openai:gpt-5.2")).toBe("openai:gpt-5.2");
  });

  it("swaps to the first live model when the current provider is gone", () => {
    expect(
      fallbackPilot(
        ["zai:glm-5.3", "zai:glm-5.2"],
        "openrouter:deepseek/deepseek-v4-flash",
      ),
    ).toBe("zai:glm-5.3");
  });

  it("keeps a bare id that still matches a live provider spec", () => {
    expect(
      fallbackPilot(["opencode-go:deepseek-v4-flash", "zai:glm-5.2"], "deepseek-v4-flash"),
    ).toBe("opencode-go:deepseek-v4-flash");
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

  it("keeps the wire spec selected when searching a catalog label", () => {
    const { current, groups } = organizePilotModels(
      ["opencode-zen:big-pickle", "opencode-zen:mimo-v2.5-free"],
      "opencode-zen:big-pickle",
      "pickle",
      { "opencode-zen:big-pickle": "Big Pickle" },
    );
    expect(current).toBe("opencode-zen:big-pickle");
    expect(groups).toEqual([]);
  });
});
