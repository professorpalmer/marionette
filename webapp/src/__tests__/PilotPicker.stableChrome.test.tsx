import { render, screen } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import PilotPicker from "../components/PilotPicker";
import type { Config } from "../lib/api";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { ...actual.api, swapPilot: vi.fn().mockResolvedValue({}) } };
});

const config: Config = {
  driver: "anthropic:claude-sonnet-4-6",
  reach: "cloud",
  budget: 1,
  models: ["anthropic:claude-sonnet-4-6"],
  model_labels: { "anthropic:claude-sonnet-4-6": "Sonnet 4.6" },
  reasoning_effort: "high",
  reasoning_support: { "anthropic:claude-sonnet-4-6": true },
};

describe("PilotPicker first paint", () => {
  // Server render runs no effects, so it shows exactly the first client paint.
  it("paints the model label and reasoning level before any effect runs", () => {
    const html = renderToString(<PilotPicker config={config} sessionId="s1" />);
    expect(html).toContain("Sonnet 4.6");
    expect(html).toContain("Reasoning effort (High)");
  });

  it("paints the staged pending model on a new session", () => {
    const html = renderToString(
      <PilotPicker
        config={{ ...config, models: [...config.models!, "openai:gpt-5.2"], model_labels: { ...config.model_labels, "openai:gpt-5.2": "GPT-5.2" } }}
        pendingModel="openai:gpt-5.2"
      />,
    );
    expect(html).toContain("GPT-5.2");
  });
});

describe("PilotPicker binding notice", () => {
  it("shows binding progress in the trigger, not as an in-flow toolbar row", () => {
    const { container } = render(
      <PilotPicker config={config} sessionId="s1" setupNotice="Setting Sonnet 4.6 for this session..." modelSelectionDisabled />,
    );
    expect(screen.queryByTestId("pilot-reroute-notice")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Setting Sonnet 4.6 for this session...");
    expect(screen.getByRole("status")).toHaveClass("sr-only");
    expect(container.querySelector("button[aria-haspopup] .animate-spin")).toBeTruthy();
  });

  it("floats a failure notice above the picker so toolbar height is unchanged", () => {
    render(<PilotPicker config={config} sessionId="s1" setupNotice="Could not set Sonnet 4.6. Choose a model before sending." />);
    const notice = screen.getByTestId("pilot-reroute-notice");
    expect(notice).toHaveTextContent("Could not set Sonnet 4.6");
    expect(notice).toHaveClass("absolute", "bottom-full");
  });
});
