import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SwarmPane from "../components/SwarmPane";
import { api, type SwarmLive } from "../lib/api";
import { clearSWRCache } from "../lib/useStaleWhileRevalidate";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      swarmLive: vi.fn(),
      swarmCancel: vi.fn(),
      artifacts: vi.fn(),
    },
  };
});

const mockSwarmLive = vi.mocked(api.swarmLive);
const mockArtifacts = vi.mocked(api.artifacts);

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
    sessionStorage.clear();
    clearSWRCache();
    mockSwarmLive.mockResolvedValue(liveJob());
    mockArtifacts.mockResolvedValue([]);
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
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
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
        artifacts,
        artifacts_complete: true,
        tasks: Array.from({ length: 5 }, (_, i) => ({
          id: `task-${i}`,
          status: "running",
          role: "Worker",
          instruction: "",
          adapter: "agentic",
        })),
      }),
    );

    render(<SwarmPane />);

    await waitFor(() => {
      expect(screen.getByTitle("Model: final-model-0")).toBeInTheDocument();
    });
    expect(screen.queryByText("initial-model-0")).not.toBeInTheDocument();
    // One expanded routing card per task (5), not 10 router+fallback pairs.
    expect(screen.getAllByTitle(/^final-model-\d$/)).toHaveLength(5);
  });
});

describe("SwarmPane mid-run savings meters", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("shows cache, routing, and compact savings on a running job row", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        status: "running",
        tokens: 12_000,
        est_cost_usd: 0.05,
        tokens_cached: 8_000,
        cache_saved_usd: 0.0123,
        routing_saved_usd: 0.04,
        tool_output_tokens_saved: 1_500,
        tool_output_savings_usd: 0.003,
      }),
    );

    render(<SwarmPane />);

    await waitFor(() => {
      expect(screen.getByText("8,000 cached")).toBeInTheDocument();
      expect(screen.getByText("1,500 compact ($0.0030)")).toBeInTheDocument();
      expect(screen.getByText("cache $0.0123")).toBeInTheDocument();
      expect(screen.getByText("route $0.0400")).toBeInTheDocument();
    });
  });
});

describe("SwarmPane dead-run detection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
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

  it("uses server dead_run_failure when the live payload is slim", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        status: "complete",
        adapter: "agentic",
        artifacts_complete: false,
        dead_run_failure: "no_model",
        artifacts: [
          { type: "verification", headline: "audit", result: "failed", failure: "no_model" },
        ],
      }),
    );

    render(<SwarmPane />);
    await waitFor(() => expect(screen.getByText("Finished")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Finished"));

    await waitFor(() => {
      expect(screen.getByText(/all workers failed: no model/)).toBeInTheDocument();
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

describe("SwarmPane findings section collapse", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("collapses and expands the Findings section from the header", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        id: "job-findings",
        status: "complete",
        goal: "Audit findings collapse",
        adapter: "agentic",
        model: "agentic/z-ai/glm-5.2",
        artifacts_complete: true,
        artifacts: [
          { type: "FINDING", headline: "dead runner modules", confidence: 0.8 },
          { type: "RISK", headline: "thread-local budget", confidence: 0.7 },
        ],
      }),
    );

    render(<SwarmPane />);
    await waitFor(() => expect(screen.getByText("Finished")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Finished"));

    // Terminal jobs start collapsed; open the card so Findings is reachable.
    const goal = await screen.findByText("Audit findings collapse");
    fireEvent.click(goal);

    const findingsHeader = await screen.findByRole("button", { name: /Findings \(2\)/i });
    expect(screen.getByText("dead runner modules")).toBeInTheDocument();

    fireEvent.click(findingsHeader);
    await waitFor(() => {
      expect(screen.queryByText("dead runner modules")).not.toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /Findings \(2\)/i }));
    await waitFor(() => {
      expect(screen.getByText("dead runner modules")).toBeInTheDocument();
    });
  });

  it("lazy-loads full artifacts when expanding a slim finished job", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        id: "job-slim",
        status: "complete",
        goal: "Slim finished swarm",
        adapter: "agentic",
        artifacts_complete: false,
        artifacts: [
          { type: "ROUTING", headline: "", model: "glm-5.2", created_by: "router" },
        ],
      }),
    );
    mockArtifacts.mockResolvedValue([
      { type: "ROUTING", headline: "", model: "glm-5.2", created_by: "router" },
      { type: "FINDING", headline: "lazy finding landed", confidence: 0.9 },
    ]);

    render(<SwarmPane />);
    await waitFor(() => expect(screen.getByText("Finished")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Finished"));

    const goal = await screen.findByText("Slim finished swarm");
    fireEvent.click(goal);

    await waitFor(() => {
      expect(mockArtifacts).toHaveBeenCalledWith("job-slim");
      expect(screen.getByText("lazy finding landed")).toBeInTheDocument();
    });
  });
});
