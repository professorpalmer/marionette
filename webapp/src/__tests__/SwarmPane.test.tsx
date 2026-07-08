import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SwarmPane from "../components/SwarmPane";
import { api, type SwarmLive } from "../lib/api";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      swarmLive: vi.fn(),
      swarmCancel: vi.fn(),
    },
  };
});

const mockSwarmLive = vi.mocked(api.swarmLive);

function liveJob(overrides: Partial<SwarmLive["jobs"][number]> = {}): SwarmLive {
  return {
    session: { tokens_used: 0, est_cost_usd: 0 },
    jobs: [
      {
        id: "job-1",
        goal: "Audit auth flow",
        status: "running",
        ...overrides,
      },
    ],
  };
}

describe("SwarmPane model badge", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    mockSwarmLive.mockResolvedValue(liveJob());
  });

  it("renders the routed model on the job badge", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({ model: "anthropic/claude-sonnet-4", adapter: "openrouter" }),
    );

    render(<SwarmPane />);

    await waitFor(() => {
      expect(screen.getByTitle("Model: anthropic/claude-sonnet-4")).toHaveTextContent(
        "anthropic/claude-sonnet-4",
      );
    });
  });

  it("falls back to the adapter when no routed model is present", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        adapter: "openrouter",
        tasks: [{ id: "task-1", status: "running", adapter: "openrouter", role: "Worker", instruction: "" }],
      }),
    );

    render(<SwarmPane />);

    await waitFor(() => {
      expect(screen.getByTitle("Model: openrouter")).toHaveTextContent("openrouter");
    });
  });
});
