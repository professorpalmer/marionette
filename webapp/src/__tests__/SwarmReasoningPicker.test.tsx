import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import SwarmReasoningPicker from "../components/SwarmReasoningPicker";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      updateSettings: vi.fn().mockResolvedValue({}),
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
    expect(screen.getByText("Workers")).toBeInTheDocument();
    expect(screen.getByText("High")).toBeInTheDocument();
    expect(screen.queryByText("Low")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTitle(/Worker reasoning/i));
    expect(screen.getByLabelText("Worker reasoning picker")).toBeInTheDocument();
  });
});
