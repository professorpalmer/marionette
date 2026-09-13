import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import PilotPicker from "../components/PilotPicker";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      swapPilot: vi.fn().mockResolvedValue({}),
      updateSettings: vi.fn().mockResolvedValue({}),
    },
  };
});

describe("PilotPicker trigger chrome", () => {
  it("sizes the model trigger to the label instead of filling the toolbar", () => {
    render(
      <PilotPicker
        config={{
          driver: "openrouter:deepseek/deepseek-v4-flash",
          reach: "cloud",
          budget: 1,
          models: ["openrouter:deepseek/deepseek-v4-flash"],
          model_labels: { "openrouter:deepseek/deepseek-v4-flash": "DeepSeek V4 Flash" },
        }}
      />,
    );
    const trigger = screen.getByTitle("openrouter:deepseek/deepseek-v4-flash");
    expect(trigger.className).not.toMatch(/\bw-full\b/);
    expect(trigger.className).not.toMatch(/\bflex-1\b/);
    const label = trigger.querySelector("span");
    expect(label?.className).toMatch(/pilot-picker-trigger-label|max-w-/);
    expect(label?.className).not.toMatch(/\bflex-1\b/);
  });
});

describe("PilotPicker reroute notice", () => {
  it("shows a visible notice when configured driver is unavailable", () => {
    render(
      <PilotPicker
        config={{
          driver: "openai:gpt-5.2",
          reach: "cloud",
          budget: 1,
          models: ["anthropic:claude-sonnet-4-6"],
          model_labels: {},
        }}
      />,
    );
    expect(screen.getByTestId("pilot-reroute-notice")).toHaveTextContent(/unavailable/i);
  });
});
