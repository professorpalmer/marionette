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
