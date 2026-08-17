import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SwarmPane, { jobSavings } from "../components/SwarmPane";
import { api, type Job, type SwarmLive } from "../lib/api";
import { dispatchProjectSelected } from "../lib/panelTransition";
import { clearSWRCache, writeSWRCache } from "../lib/useStaleWhileRevalidate";

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
const mockSwarmCancel = vi.mocked(api.swarmCancel);
const mockArtifacts = vi.mocked(api.artifacts);

function liveJob(
  jobOverrides: Partial<SwarmLive["jobs"][number]> = {},
  sessionOverrides: Partial<SwarmLive["session"]> = {},
): SwarmLive {
  return {
    session: { tokens_used: 0, est_cost_usd: 0, ...sessionOverrides },
    jobs: [
      {
        id: "job-1",
        goal: "Audit auth flow",
        status: "running",
        ...jobOverrides,
      },
    ],
  };
}

function finishedJob(
  id: string,
  goal: string,
  overrides: Partial<SwarmLive["jobs"][number]> = {},
): SwarmLive {
  return liveJob({ id, goal, status: "complete", adapter: "agentic", ...overrides });
}

function expectBefore(first: HTMLElement, second: HTMLElement) {
  expect(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
}

describe("SwarmPane sort and filter controls", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
    mockSwarmLive.mockResolvedValue({
      session: { tokens_used: 0, est_cost_usd: 0 },
      jobs: [
        { id: "job-z", goal: "Older active audit", status: "running", created_at: "2026-01-01T00:00:00Z", model: "model-old" },
        { id: "job-a", goal: "Newest active build", status: "running", created_at: "2026-03-01T00:00:00Z", model: "model-new" },
        { id: "job-no-time", goal: "Active job without timestamp", status: "running" },
        { id: "job-y", goal: "Older completed review", status: "complete", created_at: "2026-01-15T00:00:00Z" },
        { id: "job-b", goal: "Newest failed review", status: "failed", created_at: "2026-02-15T00:00:00Z" },
        { id: "job-c", goal: "Degraded architecture review", status: "complete", created_at: "2026-02-01T00:00:00Z", outcome: { quality: "degraded", trustworthy: false, reasons: ["only verification artifacts"] } },
      ],
    });
  });

  it("distributes filter and sort controls evenly across the toolbar", async () => {
    render(<SwarmPane />);
    const filter = await screen.findByLabelText("Filter swarms");
    const sort = screen.getByLabelText("Sort swarms");

    expect(filter.parentElement).toHaveClass("grid", "grid-cols-2");
    expect(filter).toHaveClass("w-full");
    expect(sort).toHaveClass("w-full");
  });

  it("sorts active and finished jobs newest-first by creation time", async () => {
    render(<SwarmPane />);

    const newestActive = await screen.findByText("Newest active build");
    expectBefore(newestActive, screen.getByText("Older active audit"));

    fireEvent.click(screen.getByText("Finished"));
    expectBefore(
      await screen.findByText("Newest failed review"),
      screen.getByText("Degraded architecture review"),
    );
    expectBefore(
      screen.getByText("Degraded architecture review"),
      screen.getByText("Older completed review"),
    );
  });

  it("reverses both lifecycle groups when Oldest is selected", async () => {
    render(<SwarmPane />);
    await screen.findByText("Newest active build");

    fireEvent.change(screen.getByLabelText("Sort swarms"), { target: { value: "oldest" } });
    expectBefore(screen.getByText("Older active audit"), screen.getByText("Newest active build"));
    expectBefore(screen.getByText("Newest active build"), screen.getByText("Active job without timestamp"));

    fireEvent.click(screen.getByText("Finished"));
    expectBefore(
      await screen.findByText("Older completed review"),
      screen.getByText("Degraded architecture review"),
    );
  });

  it("filters by lifecycle without conflating failed and untrustworthy jobs", async () => {
    render(<SwarmPane />);
    await screen.findByText("Newest active build");

    fireEvent.change(screen.getByLabelText("Filter swarms"), { target: { value: "failed" } });
    expect(await screen.findByText("Newest failed review")).toBeInTheDocument();
    expect(screen.queryByText("Degraded architecture review")).not.toBeInTheDocument();
    expect(screen.queryByText("Newest active build")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Filter swarms"), { target: { value: "untrustworthy" } });
    expect(await screen.findByText("Degraded architecture review")).toBeInTheDocument();
    expect(screen.queryByText("Newest failed review")).not.toBeInTheDocument();
  });

  it("clears a filter with no matching jobs", async () => {
    render(<SwarmPane />);
    await screen.findByText("Newest active build");

    fireEvent.change(screen.getByLabelText("Filter swarms"), { target: { value: "cancelled" } });
    expect(await screen.findByText("No swarm jobs match this filter")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear filter" }));
    expect(await screen.findByText("Newest active build")).toBeInTheDocument();
  });
});

describe("SwarmPane SWR cache first-open", () => {
  const REPO = "C:\\Users\\pwall\\Projects\\warm-swarm";

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
    dispatchProjectSelected(REPO);
  });

  it("renders seeded jobs immediately without Loading swarm jobs...", async () => {
    const payload = liveJob({
      id: "job-warm",
      goal: "Pre-warmed swarm job",
      status: "running",
    });
    writeSWRCache(`swarm:${REPO}`, payload);
    // Hang the network fetch so we only see the cache seed (activity-poll warm path).
    mockSwarmLive.mockImplementation(() => new Promise(() => {}));

    render(<SwarmPane />);

    expect(screen.queryByText("Loading swarm jobs...")).not.toBeInTheDocument();
    expect(screen.getByText("Pre-warmed swarm job")).toBeInTheDocument();
  });

  it("shows Loading swarm jobs... on cold mount with empty cache", async () => {
    mockSwarmLive.mockImplementation(() => new Promise(() => {}));

    render(<SwarmPane />);

    expect(screen.getByText("Loading swarm jobs...")).toBeInTheDocument();
    expect(screen.queryByText("Pre-warmed swarm job")).not.toBeInTheDocument();
  });
});

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

  it("shows routing placeholder instead of bare agentic while running", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        adapter: "agentic",
        model: "agentic",
        status: "running",
        tasks: [
          {
            id: "task-1",
            status: "running",
            adapter: "agentic",
            role: "implement (agentic)",
            instruction: "",
          },
        ],
      }),
    );

    render(<SwarmPane />);

    await waitFor(() => {
      expect(screen.getByTitle("Model routing in progress")).toHaveTextContent("routing…");
    });
    expect(screen.queryByTitle("Model: agentic")).not.toBeInTheDocument();
  });

  it("shows each worker's own model without inheriting the job model", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        model: "job-default",
        adapter: "agentic",
        tasks: [
          { id: "task-a", status: "running", adapter: "agentic", role: "worker-a", instruction: "", model: "model-a" },
          { id: "task-b", status: "running", adapter: "agentic", role: "worker-b", instruction: "", model: "model-b" },
          { id: "task-unknown", status: "running", adapter: "agentic", role: "worker-unknown", instruction: "" },
        ],
      }),
    );

    render(<SwarmPane />);

    await waitFor(() => {
      expect(screen.getByText("(model-a)")).toBeInTheDocument();
      expect(screen.getByText("(model-b)")).toBeInTheDocument();
    });
    expect(screen.queryByText("(job-default)")).not.toBeInTheDocument();
  });

  it("updates a worker model when routing settles on a later poll", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let pollCount = 0;
      mockSwarmLive.mockImplementation(async () => {
        pollCount += 1;
        return liveJob({
          model: "job-default",
          adapter: "agentic",
          tasks: [{
            id: "task-1", status: "running", adapter: "agentic",
            role: "worker", instruction: "",
            ...(pollCount > 2 ? { model: "routed-model" } : {}),
          }],
        });
      });

      render(<SwarmPane />);
      await waitFor(() => expect(screen.queryByText("(routed-model)")).not.toBeInTheDocument());
      await vi.advanceTimersByTimeAsync(6000);
      await waitFor(() => expect(screen.getByText("(routed-model)")).toBeInTheDocument());
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("SwarmPane worker details", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("lets a terminal worker reveal its source-backed identity", async () => {
    mockSwarmLive.mockResolvedValue(
      finishedJob("job-terminal-worker", "Inspect completed worker", {
        artifacts_complete: true,
        tasks: [{
          id: "task-terminal",
          status: "complete",
          adapter: "agentic",
          role: "completed-worker",
          instruction: "",
          model: "agentic/gpt-5.6-luna",
          completed_at: "2026-08-16T17:00:00+00:00",
        }],
      }),
    );

    render(<SwarmPane />);
    await waitFor(() => expect(screen.getByText("Finished")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Finished"));
    fireEvent.click(await screen.findByText("Inspect completed worker"));

    const worker = screen.getByText("completed-worker").closest('[role="button"]');
    expect(worker).not.toBeNull();
    expect(worker).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(worker!);

    expect(worker).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("task-terminal")).toBeInTheDocument();
    expect(screen.getByText("2026-08-16T17:00:00+00:00")).toBeInTheDocument();
  });
});

describe("SwarmPane pin attribution", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("labels explicit_pin routing as Explicit pin · not auto-routed", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        status: "running",
        artifacts_complete: true,
        artifacts: [
          {
            type: "ROUTING",
            headline: "",
            task_id: "task-1",
            model: "agentic/meta/muse-spark-1.1",
            adapter_model_name: "meta/muse-spark-1.1",
            policy: "explicit_pin",
            provider: "openrouter",
            adapter: "agentic",
            created_by: "router",
            est_cost_usd: 0.01,
          },
        ],
        tasks: [
          {
            id: "task-1",
            status: "running",
            role: "Worker",
            instruction: "",
            adapter: "agentic",
          },
        ],
      }),
    );

    render(<SwarmPane />);

    await waitFor(() => {
      expect(screen.getByText("Explicit pin · not auto-routed")).toBeInTheDocument();
    });
    // Collapsed summary keeps the full registry id (not stripped agentic/).
    expect(screen.getAllByText("agentic/meta/muse-spark-1.1").length).toBeGreaterThan(0);
    expect(screen.getAllByText("explicit_pin").length).toBeGreaterThan(0);
    expect(screen.getByText("openrouter · agentic")).toBeInTheDocument();
    expect(screen.queryByText("Router pick")).not.toBeInTheDocument();
  });

  it("does not paint a missing routing estimate as $0", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        status: "running",
        artifacts_complete: true,
        artifacts: [
          {
            type: "ROUTING",
            headline: "",
            task_id: "task-1",
            model: "agentic/meta/muse-spark-1.1",
            policy: "explicit_pin",
            provider: "openrouter",
            adapter: "agentic",
            created_by: "router",
          },
        ],
        tasks: [
          {
            id: "task-1",
            status: "running",
            role: "Worker",
            instruction: "",
            adapter: "agentic",
          },
        ],
      }),
    );

    render(<SwarmPane />);

    await waitFor(() => {
      expect(screen.getByText("Explicit pin · not auto-routed")).toBeInTheDocument();
    });
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.queryByText("$0")).not.toBeInTheDocument();
  });

  it("fail-closes missing policy as Pin attribution unknown (not Router pick)", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        status: "running",
        artifacts_complete: true,
        artifacts: [
          {
            type: "ROUTING",
            headline: "",
            task_id: "task-1",
            model: "mystery-model",
            created_by: "router",
            est_cost_usd: 0.01,
          },
        ],
        tasks: [
          {
            id: "task-1",
            status: "running",
            role: "Worker",
            instruction: "",
            adapter: "agentic",
          },
        ],
      }),
    );

    render(<SwarmPane />);

    await waitFor(() => {
      expect(screen.getAllByText("Pin attribution unknown").length).toBeGreaterThanOrEqual(1);
    });
    expect(screen.queryByText("Router pick")).not.toBeInTheDocument();
  });

  it("warns FINDING headlines that look like prompt echoes without rewriting them", async () => {
    const echoHeadline = "Role: auditor — find auth bypass paths";
    mockSwarmLive.mockResolvedValue(
      liveJob({
        status: "complete",
        goal: "Echo finding warn",
        adapter: "agentic",
        artifacts_complete: true,
        artifacts: [
          { type: "FINDING", headline: echoHeadline, confidence: 0.5 },
        ],
      }),
    );

    render(<SwarmPane />);
    await waitFor(() => expect(screen.getByText("Finished")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Finished"));
    fireEvent.click(await screen.findByText("Echo finding warn"));

    await waitFor(() => {
      expect(screen.getByText("looks like prompt echo")).toBeInTheDocument();
    });
    // Headline stays verbatim — chip is advisory only.
    expect(screen.getByText(echoHeadline)).toBeInTheDocument();
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

describe("SwarmPane mid-run job-row meters", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("shows tokens, compact tokens, cost, and savings chip when savings are positive", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        status: "running",
        tokens: 12_000,
        est_cost_usd: 0.05,
        tokens_cached: 8_000,
        cache_saved_usd: 0.0123,
        routing_saved_usd: 0.04,
        routing_savings_basis: "estimated",
        tool_output_tokens_saved: 1_500,
        tool_output_savings_usd: 0.003,
      }),
    );

    render(<SwarmPane />);

    await waitFor(() => {
      expect(screen.getAllByText("12,000t").length).toBeGreaterThan(0);
      expect(screen.getByText("1,500 compact")).toBeInTheDocument();
      expect(screen.getAllByText("~$0.0500").length).toBeGreaterThan(0);
      expect(screen.getByText("~$0.0553 saved")).toBeInTheDocument();
    });
    expect(screen.queryByText("8,000 cached")).not.toBeInTheDocument();
    expect(screen.getByTitle(/model selection value vs frontier-equivalent list price/)).toBeInTheDocument();
    expect(screen.getByTitle(/prompt-cache value/)).toBeInTheDocument();
    expect(screen.getByTitle(/tool-output compaction/)).toBeInTheDocument();
  });

  it("updates job savings chip when a poll returns new savings totals", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let pollCount = 0;
      mockSwarmLive.mockImplementation(async () => {
        pollCount += 1;
        if (pollCount <= 2) {
          return liveJob({
            status: "running",
            tokens: 1_000,
            est_cost_usd: 0.01,
            routing_saved_usd: 0.02,
          });
        }
        return liveJob({
          status: "running",
          tokens: 1_000,
          est_cost_usd: 0.01,
          routing_saved_usd: 0.08,
          cache_saved_usd: 0.03,
        });
      });

      render(<SwarmPane />);

      await waitFor(() => {
        expect(screen.getByText("$0.0200 saved")).toBeInTheDocument();
      });

      await vi.advanceTimersByTimeAsync(6000);

      await waitFor(() => {
        expect(screen.getByText("$0.1100 saved")).toBeInTheDocument();
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("drops hydrated success artifacts when the same job becomes degraded", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let pollCount = 0;
      mockSwarmLive.mockImplementation(async () => {
        pollCount += 1;
        if (pollCount <= 2) {
          return liveJob({
            id: "job-outcome-flip",
            goal: "Outcome flips",
            status: "complete",
            outcome: { quality: "ok", trustworthy: true, reasons: [] },
            artifacts_complete: true,
            artifacts: [{ type: "finding", headline: "stale success finding" }],
          });
        }
        return liveJob({
          id: "job-outcome-flip",
          goal: "Outcome flips",
          status: "complete",
          outcome: {
            quality: "degraded",
            trustworthy: false,
            reasons: ["only verification artifacts — no findings/decisions/patches"],
          },
          artifacts_complete: false,
          artifacts: [{ type: "verification", headline: "provider failed", result: "failed" }],
        });
      });

      render(<SwarmPane />);
      await waitFor(() => expect(screen.getByText("Finished")).toBeInTheDocument());
      fireEvent.click(screen.getByText("Finished"));
      fireEvent.click(await screen.findByText("Outcome flips"));
      expect(await screen.findByText("stale success finding")).toBeInTheDocument();

      await vi.advanceTimersByTimeAsync(6000);

      await waitFor(() => {
        expect(screen.getByText("degraded")).toBeInTheDocument();
        expect(screen.queryByText("stale success finding")).not.toBeInTheDocument();
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("refuses visibility-only jobs in jobSavings totals", () => {
    const parts = jobSavings({
      id: "j-ext",
      goal: "x",
      status: "running",
      source: "cli",
      accounting_owned: false,
      routing_saved_usd: 1.25,
      routing_savings_basis: "estimated",
      cache_saved_usd: 0.05,
      tool_output_savings_usd: 0.01,
    } as Job);
    expect(parts.total).toBe(0);
  });

  it("treats missing accounting_owned on CLI as visibility-only", () => {
    const parts = jobSavings({
      id: "j-ext",
      goal: "x",
      status: "running",
      source: "cli",
      routing_saved_usd: 1.25,
      routing_savings_basis: "estimated",
    } as Job);
    expect(parts.total).toBe(0);
  });

  it("refuses unknown routing basis in jobSavings totals", () => {
    const parts = jobSavings({
      id: "j1",
      goal: "x",
      status: "complete",
      routing_saved_usd: 1.25,
      routing_savings_basis: "unknown",
      cache_saved_usd: 0.05,
    } as Job);
    expect(parts.routing).toBe(0);
    expect(parts.modelSelection).toBe(0);
    expect(parts.total).toBeCloseTo(0.05, 8);
  });

  it("keeps estimated routing labeled separately from measured cache", () => {
    const parts = jobSavings({
      id: "j1",
      goal: "x",
      status: "complete",
      routing_saved_usd: 0.40,
      routing_savings_basis: "estimated",
      cache_saved_usd: 0.10,
    } as Job);
    expect(parts.routing).toBeCloseTo(0.40, 8);
    expect(parts.modelSelectionEstimated).toBe(true);
    expect(parts.cache).toBeCloseTo(0.10, 8);
    expect(parts.total).toBeCloseTo(0.50, 8);
  });

  it("marks partial cache value when cached tokens are unpriced", () => {
    const parts = jobSavings({
      id: "j1",
      goal: "x",
      status: "complete",
      cache_saved_usd: 0.10,
      swarm_cache_savings_basis: "unknown",
      swarm_cache_unpriced_tokens: 25_000,
    } as Job);

    expect(parts.cache).toBeCloseTo(0.10, 8);
    expect(parts.cachePartial).toBe(true);
    expect(parts.cacheUnpricedTokens).toBe(25_000);
  });

  it("does not replace measured zero delegation with routing estimate", () => {
    const parts = jobSavings({
      id: "j1",
      goal: "x",
      status: "complete",
      delegation_saved_usd: 0,
      delegation_savings_basis: "actual_usage",
      routing_saved_usd: 1.25,
      routing_savings_basis: "estimated",
    } as Job);
    expect(parts.delegation).toBe(0);
    expect(parts.modelSelection).toBe(0);
    expect(parts.routing).toBeCloseTo(1.25, 8);
    expect(parts.total).toBe(0);
  });

  it("renders local-job routing policy label after reload the same as live", async () => {
    // Persisted local-* jobs carry UI-shaped ROUTING rows (with policy) inline;
    // running jobs auto-expand so the chip is visible without a click.
    mockSwarmLive.mockResolvedValue(
      liveJob({
        id: "local-1",
        status: "running",
        artifacts_complete: true,
        artifacts: [
          {
            type: "ROUTING",
            headline: "Routed to cheap-model",
            created_by: "router",
            model: "cheap-model",
            policy: "balanced",
            adapter: "agentic",
            est_cost_usd: 0.02,
            detail: "balanced pick",
            task_id: "local-1-w0",
          },
        ],
        tasks: [
          {
            id: "local-1-w0",
            status: "running",
            role: "implement (agentic)",
            instruction: "",
            adapter: "agentic",
          },
        ],
      }),
    );

    render(<SwarmPane />);
    await waitFor(() => {
      expect(screen.getByText("balanced")).toBeInTheDocument();
    });
    expect(screen.getByText(/Right-sized: cheapest model/)).toBeInTheDocument();
    expect(screen.queryByText("Pin attribution unknown")).not.toBeInTheDocument();
  });
});

describe("SwarmPane truthful failed vs cancelled chrome", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("paints ordinary worker failure as failed, not cancelled", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        id: "job-fail",
        goal: "Ordinary failure",
        status: "failed",
        adapter: "agentic",
      }),
    );

    render(<SwarmPane />);
    await waitFor(() => expect(screen.getByText("Finished")).toBeInTheDocument());
    expect(screen.getByText(/1 failed/)).toBeInTheDocument();
    expect(screen.queryByText(/cancelled/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Finished"));
    await waitFor(() => {
      expect(screen.getByText("Ordinary failure")).toBeInTheDocument();
      expect(screen.getByText("failed")).toBeInTheDocument();
    });
    expect(screen.queryByText("cancelled")).not.toBeInTheDocument();
  });

  it("moves timed-out jobs into Finished with failed worker chrome", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        id: "job-timeout",
        goal: "Timed-out command",
        status: "timeout",
        adapter: "command",
        tasks: [
          {
            id: "job-timeout-w0",
            status: "timeout",
            role: "command",
            instruction: "pytest",
            adapter: "command",
          },
        ],
      }),
    );

    render(<SwarmPane />);

    await waitFor(() => expect(screen.getByText("Finished")).toBeInTheDocument());
    expect(screen.getByText(/1 failed/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Finished"));
    fireEvent.click(await screen.findByText("Timed-out command"));
    expect(screen.getByText("1/1 · 1 failed")).toBeInTheDocument();
    expect(screen.getByTitle("Dismiss from tracker (stays in Puppetmaster history)")).toBeInTheDocument();
  });

  it("moves truncated and interrupted jobs into Finished with failed chrome", async () => {
    const truncated = liveJob({
      id: "job-truncated",
      goal: "Truncated command",
      status: "truncated",
      adapter: "command",
      tasks: [
        { id: "job-truncated-w0", status: "truncated", role: "command", instruction: "cat", adapter: "command" },
      ],
    });
    const interrupted = liveJob({
      id: "job-interrupted",
      goal: "Interrupted command",
      status: "interrupted",
      adapter: "command",
      tasks: [
        { id: "job-interrupted-w0", status: "interrupted", role: "command", instruction: "sleep", adapter: "command" },
      ],
    });
    mockSwarmLive.mockResolvedValue({
      session: truncated.session,
      jobs: [...truncated.jobs, ...interrupted.jobs],
    });

    render(<SwarmPane />);

    await waitFor(() => expect(screen.getByText("Finished")).toBeInTheDocument());
    expect(screen.getByText(/2 failed/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Finished"));
    expect(await screen.findByText("Truncated command")).toBeInTheDocument();
    expect(screen.getByText("Interrupted command")).toBeInTheDocument();
  });

  it("renders Puppetmaster's canonical degraded outcome as amber, never green", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        id: "job-degraded",
        goal: "Provider failed before analysis",
        status: "complete",
        adapter: "agentic",
        outcome: {
          quality: "degraded",
          trustworthy: false,
          reasons: ["only verification artifacts — no findings/decisions/patches"],
        },
        artifacts_complete: false,
        artifacts: [],
      }),
    );

    render(<SwarmPane />);
    await waitFor(() => expect(screen.getByText("Finished")).toBeInTheDocument());
    expect(screen.getByText(/1 untrustworthy/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Finished"));
    await waitFor(() => {
      expect(screen.getByText("degraded")).toBeInTheDocument();
      expect(screen.getByText(/only verification artifacts/)).toBeInTheDocument();
    });
    expect(screen.queryByText("done")).not.toBeInTheDocument();
  });

  it("paints true user cancel as cancelled and does not tally it as failed", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        id: "job-cancel",
        goal: "User aborted swarm",
        status: "cancelled",
        adapter: "agentic",
      }),
    );

    render(<SwarmPane />);
    await waitFor(() => expect(screen.getByText("Finished")).toBeInTheDocument());
    expect(screen.getByText(/1 cancelled/)).toBeInTheDocument();
    expect(screen.queryByText(/failed/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Finished"));
    await waitFor(() => {
      expect(screen.getByText("User aborted swarm")).toBeInTheDocument();
      expect(screen.getByText("cancelled")).toBeInTheDocument();
    });
    expect(screen.queryByText("failed")).not.toBeInTheDocument();
  });
});

describe("SwarmPane cancel Kill contract", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
    mockSwarmCancel.mockReset();
    mockSwarmLive.mockResolvedValue(
      liveJob({
        id: "job-kill",
        goal: "Killable running swarm",
        status: "running",
        adapter: "agentic",
      }),
    );
  });

  it("accepted Kill shows optimistic cancelling then refreshes live", async () => {
    mockSwarmCancel.mockResolvedValue({ ok: true, job_id: "job-kill" });
    let liveCalls = 0;
    mockSwarmLive.mockImplementation(async () => {
      liveCalls += 1;
      if (liveCalls <= 1) {
        return liveJob({
          id: "job-kill",
          goal: "Killable running swarm",
          status: "running",
        });
      }
      return liveJob({
        id: "job-kill",
        goal: "Killable running swarm",
        status: "cancelled",
      });
    });

    render(<SwarmPane />);
    const kill = await screen.findByTitle("Cancel this job");
    fireEvent.click(kill);

    await waitFor(() => {
      expect(mockSwarmCancel).toHaveBeenCalledWith("job-kill");
      expect(screen.getByText("cancelling...")).toBeInTheDocument();
    });
    // Best-effort cooperative cancel only — no force-kill claim in the UI.
    expect(screen.queryByText(/force/i)).not.toBeInTheDocument();

    await waitFor(() => {
      expect(liveCalls).toBeGreaterThan(1);
    });
  });

  it("rejected Kill clears cancelling and leaves Kill retryable", async () => {
    mockSwarmCancel.mockResolvedValue({ ok: false, error: "not running" });

    render(<SwarmPane />);
    const kill = await screen.findByTitle("Cancel this job");
    fireEvent.click(kill);

    await waitFor(() => {
      expect(mockSwarmCancel).toHaveBeenCalledWith("job-kill");
    });
    await waitFor(() => {
      expect(screen.queryByText("cancelling...")).not.toBeInTheDocument();
      expect(screen.getByTitle("Cancel this job")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTitle("Cancel this job"));
    await waitFor(() => {
      expect(mockSwarmCancel).toHaveBeenCalledTimes(2);
    });
  });

  it("stale/404 Kill clears cancelling, refreshes, and allows retry", async () => {
    mockSwarmCancel
      .mockRejectedValueOnce(Object.assign(new Error("Not Found"), { status: 404 }))
      .mockResolvedValueOnce({ ok: true, job_id: "job-kill" });

    render(<SwarmPane />);
    const kill = await screen.findByTitle("Cancel this job");
    fireEvent.click(kill);

    await waitFor(() => {
      expect(mockSwarmCancel).toHaveBeenCalledTimes(1);
      expect(screen.queryByText("cancelling...")).not.toBeInTheDocument();
      expect(screen.getByTitle("Cancel this job")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTitle("Cancel this job"));
    await waitFor(() => {
      expect(mockSwarmCancel).toHaveBeenCalledTimes(2);
      expect(screen.getByText("cancelling...")).toBeInTheDocument();
    });
  });
});

describe("SwarmPane canonical outcome", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("keeps a trustworthy complete job rendered as done", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        status: "complete",
        adapter: "agentic",
        outcome: { quality: "ok", trustworthy: true, reasons: [] },
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
      expect(screen.queryByText(/only verification artifacts/)).not.toBeInTheDocument();
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

describe("SwarmPane worker tokens and cost", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("shows compact tokens and cost on each worker row", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        id: "job-workers",
        status: "running",
        goal: "Multi worker spend",
        tokens: 180_000,
        est_cost_usd: 0.21,
        tasks: [
          {
            id: "t1",
            role: "implement",
            instruction: "build it",
            status: "completed",
            adapter: "agentic",
            tokens: 120_000,
            est_cost_usd: 0.14,
          },
          {
            id: "t2",
            role: "review",
            instruction: "check it",
            status: "running",
            adapter: "openrouter",
            tokens: 60_000,
            est_cost_usd: 0.07,
          },
        ],
      }),
    );

    render(<SwarmPane />);

    // Running jobs auto-expand; do not click the goal (that collapses the card).
    await waitFor(() => {
      expect(screen.getByText("Workers (2)")).toBeInTheDocument();
    });
    expect(screen.getByText("120,000t")).toBeInTheDocument();
    expect(screen.getByText("~$0.1400")).toBeInTheDocument();
    expect(screen.getByText("60,000t")).toBeInTheDocument();
    expect(screen.getByText("~$0.0700")).toBeInTheDocument();
  });
});

describe("SwarmPane worker progress", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("labels failed workers separately from completed ones", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        id: "job-mixed",
        status: "running",
        goal: "Mixed worker outcomes",
        tasks: [
          { id: "t1", role: "a", instruction: "", status: "completed", adapter: "agentic" },
          { id: "t2", role: "b", instruction: "", status: "failed", adapter: "agentic" },
          { id: "t3", role: "c", instruction: "", status: "running", adapter: "agentic" },
        ],
      }),
    );

    render(<SwarmPane />);

    await waitFor(() => {
      expect(screen.getByText("Workers (3)")).toBeInTheDocument();
      expect(screen.getByText("2/3 · 1 failed")).toBeInTheDocument();
    });
  });
});

describe("SwarmPane tracker header", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("keeps the header free of session token/cost rollups", async () => {
    // Process-lifetime / boot-carry session totals used to crowd the header
    // with opaque megatokens ($26 / 28Mt). Per-job spend stays on each row.
    mockSwarmLive.mockResolvedValue(
      liveJob(
        {},
        {
          tokens_used: 28_510_956,
          est_cost_usd: 26.9602,
          routing_saved_usd: 0.12,
        },
      ),
    );

    render(<SwarmPane />);

    await waitFor(() => {
      expect(screen.getByText("Swarm Tracker")).toBeInTheDocument();
    });
    expect(screen.queryByText(/28,510,956t/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\$26\.9602/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^session$/)).not.toBeInTheDocument();
    expect(
      screen.queryByTitle(/Estimated token usage, cost, and savings for this project/),
    ).not.toBeInTheDocument();
  });
});

describe("SwarmPane external (CLI) source badge", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
  });

  it("labels CLI-merged jobs as external", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        id: "job-cli",
        goal: "Cursor MCP implement",
        status: "running",
        adapter: "cursor",
        source: "cli",
        tasks: [
          {
            id: "t1",
            role: "cursor",
            instruction: "do the thing",
            status: "running",
            adapter: "cursor",
          },
        ],
      }),
    );

    render(<SwarmPane />);

    await waitFor(() => {
      expect(
        screen.getByTitle(
          "Started outside Marionette (Cursor MCP or terminal Puppetmaster) for this workspace",
        ),
      ).toHaveTextContent("external");
    });
  });

  it("does not show the external chip for harness jobs", async () => {
    mockSwarmLive.mockResolvedValue(
      liveJob({
        source: "harness",
        adapter: "cursor",
        model: "grok-4-5",
      }),
    );

    render(<SwarmPane />);

    await waitFor(() => {
      expect(screen.getByTitle("Model: grok-4-5")).toBeInTheDocument();
    });
    expect(
      screen.queryByTitle(
        "Started outside Marionette (Cursor MCP or terminal Puppetmaster) for this workspace",
      ),
    ).toBeNull();
  });
});

describe("SwarmPane repo-scoped dismiss", () => {
  const REPO_A = "C:\\Users\\pwall\\Projects\\repo-a";
  const REPO_B = "C:\\Users\\pwall\\Projects\\repo-b";

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    mockArtifacts.mockResolvedValue([]);
    dispatchProjectSelected(REPO_A);
    mockSwarmLive.mockImplementation(async (repo?: string) => {
      if (repo === REPO_B) {
        return finishedJob("shared-job", "Repo B finished swarm");
      }
      return finishedJob("shared-job", "Repo A finished swarm");
    });
  });

  async function openFinishedSection() {
    await waitFor(() => expect(screen.getByText("Finished")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Finished"));
  }

  it("keeps dismiss state scoped to the active repo", async () => {
    render(<SwarmPane />);
    await openFinishedSection();
    await screen.findByText("Repo A finished swarm");

    fireEvent.click(screen.getByTitle("Hide all finished runs from the tracker (stays in Puppetmaster history)"));

    await waitFor(() => {
      expect(screen.getByText("All swarm jobs cleared")).toBeInTheDocument();
    });

    dispatchProjectSelected(REPO_B);
    clearSWRCache();

    await waitFor(() => {
      expect(screen.getByText("Repo B finished swarm")).toBeInTheDocument();
    });
    expect(screen.queryByText("All swarm jobs cleared")).not.toBeInTheDocument();
  });

  it("persists dismissed ids per repo across remounts", async () => {
    const { unmount } = render(<SwarmPane />);
    await openFinishedSection();
    await screen.findByText("Repo A finished swarm");

    fireEvent.click(screen.getByTitle("Hide all finished runs from the tracker (stays in Puppetmaster history)"));
    await waitFor(() => expect(screen.getByText("All swarm jobs cleared")).toBeInTheDocument());

    unmount();
    clearSWRCache();
    dispatchProjectSelected(REPO_A);
    render(<SwarmPane />);

    await waitFor(() => {
      expect(screen.getByText("All swarm jobs cleared")).toBeInTheDocument();
    });

    const stored = JSON.parse(localStorage.getItem("swarm.dismissed.v2") || "{}");
    expect(stored[REPO_A]).toEqual(["shared-job"]);
    expect(stored[REPO_B]).toBeUndefined();
  });

  it("migrates the legacy global dismiss blob into the default view only", async () => {
    localStorage.setItem("swarm.dismissed.v1", JSON.stringify(["legacy-job"]));
    dispatchProjectSelected("");

    mockSwarmLive.mockResolvedValue(finishedJob("legacy-job", "Legacy finished swarm"));

    render(<SwarmPane />);
    await waitFor(() => {
      expect(screen.getByText("All swarm jobs cleared")).toBeInTheDocument();
    });

    expect(localStorage.getItem("swarm.dismissed.v1")).toBeNull();
    const stored = JSON.parse(localStorage.getItem("swarm.dismissed.v2") || "{}");
    expect(stored.__default__).toEqual(["legacy-job"]);
  });

  it("keeps live jobs visible even when their id is in the dismiss store", async () => {
    localStorage.setItem(
      "swarm.dismissed.v2",
      JSON.stringify({ [REPO_A]: ["live-cli-job", "old-finished"] }),
    );
    mockSwarmLive.mockResolvedValue({
      session: { tokens_used: 0, est_cost_usd: 0 },
      jobs: [
        {
          id: "live-cli-job",
          goal: "CLI swarm still running",
          status: "running",
          source: "cli",
        },
        {
          id: "old-finished",
          goal: "Previously cleared finished job",
          status: "complete",
        },
      ],
    });

    render(<SwarmPane />);

    await waitFor(() => {
      expect(screen.getByText("CLI swarm still running")).toBeInTheDocument();
    });
    // Finished accordion may list zero visible rows; dismissed finished stay hidden.
    expect(screen.queryByText("Previously cleared finished job")).not.toBeInTheDocument();
    expect(screen.queryByText("All swarm jobs cleared")).not.toBeInTheDocument();
  });

  it("keeps a previously dismissed job visible after it completes if it was seen live", async () => {
    localStorage.setItem(
      "swarm.dismissed.v2",
      JSON.stringify({ [REPO_A]: ["live-cli-job"] }),
    );
    mockSwarmLive.mockResolvedValue({
      session: { tokens_used: 0, est_cost_usd: 0 },
      jobs: [
        {
          id: "live-cli-job",
          goal: "CLI swarm still running",
          status: "running",
          source: "cli",
        },
      ],
    });
    const { unmount } = render(<SwarmPane />);
    await waitFor(() => {
      expect(screen.getByText("CLI swarm still running")).toBeInTheDocument();
    });
    // Live sighting prunes the id from dismiss so completion cannot re-hide it.
    await waitFor(() => {
      const stored = JSON.parse(localStorage.getItem("swarm.dismissed.v2") || "{}");
      expect(stored[REPO_A] || []).not.toContain("live-cli-job");
    });
    unmount();
    clearSWRCache();

    mockSwarmLive.mockResolvedValue({
      session: { tokens_used: 0, est_cost_usd: 0 },
      jobs: [
        {
          id: "live-cli-job",
          goal: "CLI swarm completed",
          status: "complete",
          source: "cli",
        },
      ],
    });
    render(<SwarmPane />);
    await openFinishedSection();
    await waitFor(() => {
      expect(screen.getByText("CLI swarm completed")).toBeInTheDocument();
    });
  });

  it("surfaces a newly completed job that was never dismissed after Clear", async () => {
    mockSwarmLive.mockResolvedValue(finishedJob("old-a", "Old finished A"));
    const { unmount } = render(<SwarmPane />);
    await openFinishedSection();
    await screen.findByText("Old finished A");
    fireEvent.click(screen.getByTitle("Hide all finished runs from the tracker (stays in Puppetmaster history)"));
    await waitFor(() => expect(screen.getByText("All swarm jobs cleared")).toBeInTheDocument());
    unmount();
    clearSWRCache();

    mockSwarmLive.mockResolvedValue({
      session: { tokens_used: 0, est_cost_usd: 0 },
      jobs: [
        { id: "old-a", goal: "Old finished A", status: "complete" },
        { id: "new-peel", goal: "MCP surface audit peel", status: "complete" },
      ],
    });
    render(<SwarmPane />);
    await openFinishedSection();
    await waitFor(() => {
      expect(screen.getByText("MCP surface audit peel")).toBeInTheDocument();
    });
    expect(screen.queryByText("Old finished A")).not.toBeInTheDocument();
  });
});

describe("SwarmPane harness-open-swarm-job deep-link", () => {
  const REPO = "C:\\Users\\pwall\\Projects\\deep-link";

  beforeEach(async () => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    const { clearPendingSwarmOpenJob } = await import("../lib/pendingSwarmOpenJob");
    clearPendingSwarmOpenJob();
    mockArtifacts.mockResolvedValue([]);
    Element.prototype.scrollIntoView = vi.fn();
    dispatchProjectSelected(REPO);
  });

  it("undismisses, expands, and scrolls to the target job row", async () => {
    localStorage.setItem(
      "swarm.dismissed.v2",
      JSON.stringify({ [REPO]: ["job_abcdef012345"] }),
    );
    mockSwarmLive.mockResolvedValue(
      finishedJob("job_abcdef012345", "Deep-link target swarm", {
        artifacts_complete: false,
        artifacts: [],
      }),
    );

    render(<SwarmPane />);
    await waitFor(() => {
      expect(screen.getByText("All swarm jobs cleared")).toBeInTheDocument();
    });
    expect(screen.queryByText("Deep-link target swarm")).not.toBeInTheDocument();

    window.dispatchEvent(
      new CustomEvent("harness-open-swarm-job", {
        detail: { jobId: "job_abcdef012345" },
      }),
    );

    await waitFor(() => {
      expect(screen.getByText("Deep-link target swarm")).toBeInTheDocument();
    });
    const row = document.querySelector('[data-job-id="job_abcdef012345"]');
    expect(row).toBeTruthy();
    await waitFor(() => {
      expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
    });
    const stored = JSON.parse(localStorage.getItem("swarm.dismissed.v2") || "{}");
    expect(stored[REPO] || []).not.toContain("job_abcdef012345");
    await waitFor(() => {
      expect(mockArtifacts).toHaveBeenCalledWith("job_abcdef012345");
    });
  });

  it("clears filters that hide a deep-link target", async () => {
    mockSwarmLive.mockResolvedValue(
      finishedJob("job_abcdef012345", "Deep-link target behind filter"),
    );
    render(<SwarmPane />);
    await waitFor(() => expect(screen.getByText("Finished")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Filter swarms"), { target: { value: "active" } });
    expect(await screen.findByText("No swarm jobs match this filter")).toBeInTheDocument();

    window.dispatchEvent(
      new CustomEvent("harness-open-swarm-job", {
        detail: { jobId: "job_abcdef012345" },
      }),
    );

    expect(await screen.findByText("Deep-link target behind filter")).toBeInTheDocument();
    expect(screen.getByLabelText("Filter swarms")).toHaveValue("all");
  });

  it("consumes a pending open-job queued before mount", async () => {
    const { queuePendingSwarmOpenJob, peekPendingSwarmOpenJob } = await import(
      "../lib/pendingSwarmOpenJob"
    );
    localStorage.setItem(
      "swarm.dismissed.v2",
      JSON.stringify({ [REPO]: ["job_abcdef012345"] }),
    );
    mockSwarmLive.mockResolvedValue(
      finishedJob("job_abcdef012345", "Late-mount deep-link target", {
        artifacts_complete: false,
        artifacts: [],
      }),
    );

    // openAgentSwarmJob raced ahead of SwarmPane mount — only the queue remains.
    queuePendingSwarmOpenJob("job_abcdef012345");
    expect(peekPendingSwarmOpenJob()).toBe("job_abcdef012345");

    render(<SwarmPane />);

    await waitFor(() => {
      expect(screen.getByText("Late-mount deep-link target")).toBeInTheDocument();
    });
    expect(peekPendingSwarmOpenJob()).toBeNull();
    const row = document.querySelector('[data-job-id="job_abcdef012345"]');
    expect(row).toBeTruthy();
    await waitFor(() => {
      expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
    });
  });
});
