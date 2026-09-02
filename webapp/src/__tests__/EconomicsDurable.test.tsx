import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import EconomicsDurable from "../components/EconomicsDurable";

function economicsData(models: unknown[]) {
  return {
    available: true,
    recent_jobs: [{
      job_id: "job-models",
      status: "completed",
      accounting_owned: true,
      models,
    }],
  } as any;
}

describe("EconomicsDurable model ID rendering", () => {
  it("renders valid provider:model IDs unchanged", () => {
    render(<EconomicsDurable data={economicsData([
      { model_id: "openai:gpt-5" },
      { model_id: "anthropic:claude-sonnet-4" },
    ])} />);

    expect(screen.getByTitle("openai:gpt-5, anthropic:claude-sonnet-4")).toHaveTextContent(
      "openai:gpt-5, anthropic:claude-sonnet-4",
    );
  });

  it("ignores malformed and whitespace-only model IDs without crashing", () => {
    render(<EconomicsDurable data={economicsData([
      { model_id: null },
      { model_id: [] },
      { model_id: {} },
      { model_id: 42 },
      {},
      { model_id: "   " },
      { model_id: "google:gemini-2.5-pro" },
    ])} />);

    expect(screen.getByTitle("google:gemini-2.5-pro")).toHaveTextContent("google:gemini-2.5-pro");
    expect(screen.queryByText("42")).not.toBeInTheDocument();
  });
});
