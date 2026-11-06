import { fireEvent, render, screen } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import SwarmReasoningPicker from "../components/SwarmReasoningPicker";
import type { Config } from "../lib/api";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      updateSettings: vi.fn().mockResolvedValue({}),
      setPilotPreferences: vi.fn(),
    },
  };
});

describe("SwarmReasoningPicker", () => {
  it("shows the Settings worker effort, not the pilot picker effort", () => {
    render(
      <SwarmReasoningPicker
        config={{
          driver: "openai:gpt-5.2",
          reach: "cloud",
          budget: 1,
          models: ["openai:gpt-5.2"],
          reasoning_effort: "low",
          swarm_reasoning_effort: "high",
        }}
      />,
    );
    expect(screen.getByTestId("swarm-reasoning-picker")).toBeInTheDocument();
    expect(screen.getByText("Workers")).toBeInTheDocument();
    expect(screen.getByText("High")).toBeInTheDocument();
    expect(screen.queryByText("Low")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTitle(/Worker reasoning/i));
    expect(screen.getByLabelText("Worker reasoning picker")).toBeInTheDocument();
  });
});

// renderToString runs no effects, so this is exactly the first paint after a
// session-switch remount: it must show the configured level, not "Medium".
describe("SwarmReasoningPicker first paint", () => {
  it("paints the configured worker reasoning level", () => {
    const html = renderToString(<SwarmReasoningPicker config={{ swarm_reasoning_effort: "high" } as Config} sessionId="s" />);
    expect(html).toContain("High");
    expect(html).not.toContain("Medium");
  });
});
