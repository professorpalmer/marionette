import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

describe("SwarmPane routing dedupe", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it("shows one routing model row per task when router and router-fallback both exist", async () => {
    // Ground truth: a 5-worker swarm stores 10 ROUTING artifacts (router +
    // router-fallback per task). Display must show the final choice only.
    const artifacts = Array.from({ length: 5 }, (_, i) => [
      {
        type: "ROUTING",
        headline: "",
        task_id: `task-${i}`,
        model: `initial-model-${i}`,
        created_by: "router",
        est_cost_usd: 0.01,
      },
      {
        type: "ROUTING",
        headline: "",
        task_id: `task-${i}`,
        model: `final-model-${i}`,
        created_by: "router-fallback",
        est_cost_usd: 0.02,
      },
    ]).flat();

    mockSwarmLive.mockResolvedValue(
      liveJob({
        status: "running",
        model: "final-model-0",
        adapter: "agentic",
        tasks: Array.from({ length: 5 }, (_, i) => ({
          id: `task-${i}`,
          status: "running",
          adapter: "agentic",
          role: "Worker",
          instruction: `work ${i}`,
        })),
        artifacts,
      }),
    );

    render(<SwarmPane />);

    await waitFor(() => {
      // Routing rows use title={art.model}; the job badge uses "Model: …",
      // so exact title match isolates the five routing cards.
      for (let i = 0; i < 5; i++) {
        expect(screen.getByTitle(`final-model-${i}`)).toBeInTheDocument();
        expect(screen.queryByTitle(`initial-model-${i}`)).not.toBeInTheDocument();
      }
    });
  });
});

describe("SwarmPane dead-run detection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it("renders a complete job whose every artifact failed as a failed run with the reason", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        status: "complete",
        adapter: "agentic",
        artifacts: [
          { type: "verification", headline: "audit", result: "failed", failure: "no_model" },
          { type: "verification", headline: "audit", result: "failed", failure: "no_model" },
        ],
      }),
    );

    render(<SwarmPane />);

    // Terminal jobs fold into the collapsed Finished accordion; open it.
    await waitFor(() => expect(screen.getByText("Finished")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Finished"));

    await waitFor(() => {
      expect(screen.getByText(/all workers failed: no model/)).toBeInTheDocument();
      expect(screen.getByText("failed")).toBeInTheDocument();
      expect(screen.queryByText("done")).not.toBeInTheDocument();
    });
  });

  it("keeps a complete job with real findings rendered as done", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        status: "complete",
        adapter: "agentic",
        artifacts: [
          { type: "finding", headline: "found a bug" },
          { type: "verification", headline: "audit", result: "failed", failure: "no_model" },
        ],
      }),
    );

    render(<SwarmPane />);

    await waitFor(() => expect(screen.getByText("Finished")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Finished"));

    await waitFor(() => {
      expect(screen.getByText("done")).toBeInTheDocument();
      expect(screen.queryByText(/all workers failed/)).not.toBeInTheDocument();
    });
  });
});
