import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import EconomicsPane from "../components/EconomicsPane";
import { api } from "../lib/api";
import { openAgentSwarmJob } from "../lib/agentLinks";
import { clearSWRCache } from "../lib/useStaleWhileRevalidate";

vi.mock("../lib/api", () => ({
  api: {
    getUsage: vi.fn(),
    getEconomics: vi.fn(),
    compactSession: vi.fn(),
  },
}));

vi.mock("../lib/usePolling", () => ({
  usePolling: (fn: () => unknown) => {
    void fn();
  },
}));

vi.mock("../lib/agentLinks", () => ({
  openAgentSwarmJob: vi.fn(),
}));

const mockGetUsage = vi.mocked(api.getUsage);
const mockGetEconomics = vi.mocked(api.getEconomics);
const mockOpenAgentSwarmJob = vi.mocked(openAgentSwarmJob);

const usageSession = {
  tokens_used: 8000,
  est_cost_usd: 0.12,
  driver: "anthropic:claude-sonnet",
  price_in: 3,
  price_out: 15,
  cache_savings_usd: 0.04,
  tool_output_savings_usd: 0.02,
};

const durablePayload = {
  available: true,
  scope: "repo",
  window_days: null,
  all_projects: false,
  savings: {
    routing: { saved_usd: 1.5, plan_routed_tasks: 1 },
    codegraph: { dollars_saved_est: 0.4 },
    counterfactual: { reference_model_id: "anthropic/claude-opus-4" },
  },
  counterfactual: {
    reference_model_id: "anthropic/claude-opus-4",
    avoided_usd: 3.2,
    label: "list-price vs the named reference model, not a cash refund",
  },
  recent_jobs: [] as Array<Record<string, unknown>>,
};

describe("EconomicsPane", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearSWRCache();
    mockGetEconomics.mockResolvedValue(durablePayload);
  });

  it("owns a persistent header and height-constrained scroll viewport", async () => {
    mockGetUsage.mockResolvedValue({
      session: usageSession,
      jobs: [],
    });

    const { container } = render(<EconomicsPane />);

    expect(await screen.findByText("This app run")).toBeInTheDocument();
    expect(screen.getByText("Economics")).toBeInTheDocument();
    expect(container.firstElementChild).toHaveClass("flex", "flex-col", "h-full", "overflow-hidden");
    const appRun = screen.getByText("This app run");
    const econ = screen.getByText("Economics");
    expect(appRun.compareDocumentPosition(econ) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(econ.closest(".shrink-0")).toBeTruthy();
    expect(screen.getByText("Durable").closest(".overflow-y-auto")).toHaveClass(
      "flex-1",
      "min-h-0",
      "overflow-y-auto",
    );
    expect(screen.getByText(/not Swarm Tracker receipt savings/)).toBeInTheDocument();
  });

  it("keeps the last Economics data visible across card remounts", async () => {
    mockGetUsage.mockResolvedValue({ session: usageSession, jobs: [] });

    const first = render(<EconomicsPane />);
    expect(await screen.findByText("This app run")).toBeInTheDocument();
    first.unmount();
    mockGetUsage.mockImplementation(() => new Promise(() => {}));

    render(<EconomicsPane />);

    expect(screen.getByText("This app run")).toBeInTheDocument();
    expect(screen.queryByText("Loading this app run…")).not.toBeInTheDocument();
  });

  it("shows spend and list-price value from getUsage", async () => {
    mockGetUsage.mockResolvedValue({
      session: usageSession,
      jobs: [],
    });

    render(<EconomicsPane />);

    expect(await screen.findByText("This app run")).toBeInTheDocument();
    expect(screen.getByText("Estimated spend")).toBeInTheDocument();
    expect(screen.getByText("~$0.12")).toBeInTheDocument();
    expect(screen.getByText("Prompt-cache value")).toBeInTheDocument();
    expect(screen.getByText("List-price value (est.)")).toBeInTheDocument();
    expect(screen.queryByText("Session cost")).not.toBeInTheDocument();
  });

  it("shows durable heading, reference model, and scope control", async () => {
    mockGetUsage.mockResolvedValue({
      session: usageSession,
      jobs: [],
    });

    render(<EconomicsPane />);

    expect(await screen.findByText("Durable")).toBeInTheDocument();
    expect(screen.getAllByText(/anthropic\/claude-opus-4/).length).toBeGreaterThan(0);
    expect(screen.getByLabelText("Economics scope")).toBeInTheDocument();
    expect(screen.getByText("This repo")).toBeInTheDocument();
    expect(screen.getByText("Routing saved (measured)")).toBeInTheDocument();
    expect(screen.getByText("CodeGraph (estimated)")).toBeInTheDocument();
    expect(screen.queryByText("Session cost")).not.toBeInTheDocument();
  });

  it("labels visibility-only jobs without treating their dollars as owned spend", async () => {
    mockGetUsage.mockResolvedValue({
      session: usageSession,
      jobs: [],
    });
    mockGetEconomics.mockResolvedValue({
      ...durablePayload,
      owned_jobs_considered: 1,
      owned_actual_marginal_usd: 1.25,
      recent_jobs: [
        {
          job_id: "owned-1",
          accounting_owned: true,
          accounting_scope: "marionette",
          models: [{ model_id: "composer-2" }],
          tokens: 800,
          typed_artifacts: 2,
          tokens_per_typed_artifact: 400,
          degraded_rate: 0,
          actual_marginal_usd: 1.25,
          measured_cost_usd: 1.25,
          cost_basis: "measured_usage_x_registry_price",
          counterfactual: { avoided_usd: 2.0 },
        },
        {
          job_id: "vis-1",
          accounting_owned: false,
          accounting_scope: "visibility_only",
          models: [{ model_id: "foreign-model" }],
          tokens: 9000,
          typed_artifacts: 1,
          actual_marginal_usd: null,
          counterfactual: null,
        },
      ],
    });

    render(<EconomicsPane />);

    expect(await screen.findByText("vis-1")).toBeInTheDocument();
    expect(screen.getByText(/visible only/)).toBeInTheDocument();
    expect(screen.queryByText("$9.99")).not.toBeInTheDocument();
    expect(screen.queryByText("~$9.99")).not.toBeInTheDocument();
    const ownedRow = screen.getByText("owned-1").closest(".mb-2");
    expect(ownedRow).toBeTruthy();
    const ownedScope = within(ownedRow as HTMLElement);
    expect(ownedScope.getByText("Measured Cost")).toBeInTheDocument();
    expect(ownedScope.getByText("Estimated Cost")).toBeInTheDocument();
    expect(ownedScope.getByText("Vs reference").parentElement).toHaveTextContent("Vs reference$2.00");
    expect(ownedScope.getAllByText("$1.25").length).toBeGreaterThanOrEqual(1);
    expect(ownedScope.getAllByText("—").length).toBeGreaterThanOrEqual(1);
  });

  it("opens a recent PM job in the Swarm Tracker", async () => {
    mockGetUsage.mockResolvedValue({ session: usageSession, jobs: [] });
    mockGetEconomics.mockResolvedValue({
      ...durablePayload,
      recent_jobs: [
        {
          job_id: "job_abcdef012345",
          accounting_owned: true,
          models: [{ model_id: "agentic/gpt-5.6-luna" }],
          tokens: 800,
          typed_artifacts: 4,
          tokens_per_typed_artifact: 200,
          degraded_rate: 0,
          actual_marginal_usd: 1.25,
          counterfactual: { avoided_usd: 2.0 },
        },
      ],
    });

    render(<EconomicsPane />);
    fireEvent.click(await screen.findByRole("button", { name: "job_abcdef012345" }));

    expect(mockOpenAgentSwarmJob).toHaveBeenCalledWith("job_abcdef012345");
    expect(screen.getByText("agentic/gpt-5.6-luna")).toBeInTheDocument();
    expect(screen.queryByText(/typed/)).not.toBeInTheDocument();
    expect(screen.queryByText(/degraded/)).not.toBeInTheDocument();
  });

  it("shows headline sum and measured/estimated lines for mixed job cost", async () => {
    mockGetUsage.mockResolvedValue({ session: usageSession, jobs: [] });
    mockGetEconomics.mockResolvedValue({
      ...durablePayload,
      recent_jobs: [
        {
          job_id: "mixed-1",
          accounting_owned: true,
          models: [{ model_id: "composer-2" }],
          tokens: 1200,
          measured_cost_usd: 1.25,
          estimated_cost_usd: 0.25,
          actual_marginal_usd: 1.25,
          cost_basis: "mixed",
          counterfactual: { avoided_usd: 3.0 },
        },
      ],
    });

    render(<EconomicsPane />);

    expect(await screen.findByText("mixed-1")).toBeInTheDocument();
    expect(screen.getByText("Measured Cost")).toBeInTheDocument();
    expect(screen.getByText("Estimated Cost")).toBeInTheDocument();
    expect(screen.getAllByText("$1.25").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("$0.25")).toBeInTheDocument();
    const mixedRow = within(screen.getByText("mixed-1").closest(".mb-2") as HTMLElement);
    expect(mixedRow.getByText("Vs reference").parentElement).toHaveTextContent("Vs reference$3.00");
    expect(mixedRow.getByText("$1.25")).toHaveClass("text-warn/90");
    expect(mixedRow.getByText("$0.25")).toHaveClass("text-warn/90");
    expect(mixedRow.getByText("$3.00")).toHaveClass("text-good/90");
  });

  it("shows measured-only headline without faking a zero estimated line", async () => {
    mockGetUsage.mockResolvedValue({ session: usageSession, jobs: [] });
    mockGetEconomics.mockResolvedValue({
      ...durablePayload,
      recent_jobs: [
        {
          job_id: "meas-1",
          accounting_owned: true,
          models: [{ model_id: "composer-2" }],
          tokens: 800,
          measured_cost_usd: 2.0,
          actual_marginal_usd: 2.0,
          cost_basis: "measured_usage_x_registry_price",
        },
      ],
    });

    render(<EconomicsPane />);

    expect(await screen.findByText("meas-1")).toBeInTheDocument();
    const row = within(screen.getByText("meas-1").closest(".mb-2") as HTMLElement);
    expect(row.getByText("Measured Cost").parentElement).toHaveTextContent("Measured Cost$2.00");
    expect(row.getByText("Estimated Cost").parentElement).toHaveTextContent("Estimated Cost—");
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("shows estimated-only headline without faking a zero measured line", async () => {
    mockGetUsage.mockResolvedValue({ session: usageSession, jobs: [] });
    mockGetEconomics.mockResolvedValue({
      ...durablePayload,
      recent_jobs: [
        {
          job_id: "est-1",
          accounting_owned: true,
          models: [{ model_id: "composer-2" }],
          tokens: 500,
          estimated_cost_usd: 0.75,
          actual_marginal_usd: null,
          cost_basis: "estimated",
        },
      ],
    });

    render(<EconomicsPane />);

    expect(await screen.findByText("est-1")).toBeInTheDocument();
    const row = within(screen.getByText("est-1").closest(".mb-2") as HTMLElement);
    expect(row.getByText("Measured Cost").parentElement).toHaveTextContent("Measured Cost—");
    expect(row.getByText("Estimated Cost").parentElement).toHaveTextContent("Estimated Cost$0.75");
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("falls back to actual_marginal_usd for headline when split fields are missing", async () => {
    mockGetUsage.mockResolvedValue({ session: usageSession, jobs: [] });
    mockGetEconomics.mockResolvedValue({
      ...durablePayload,
      recent_jobs: [
        {
          job_id: "legacy-1",
          accounting_owned: true,
          models: [{ model_id: "composer-2" }],
          tokens: 600,
          actual_marginal_usd: 1.1,
          cost_basis: "measured",
        },
      ],
    });

    render(<EconomicsPane />);

    expect(await screen.findByText("legacy-1")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });

  it("clears durable state when getEconomics returns a soft 400", async () => {
    mockGetUsage.mockResolvedValue({
      session: usageSession,
      jobs: [],
    });
    mockGetEconomics.mockImplementation(async (nextScope?: string) => {
      if (nextScope === "conversation") {
        return { ok: false, error: "scope must be repo, window30, all_projects, or conversation" };
      }
      return durablePayload;
    });

    render(<EconomicsPane />);
    expect(await screen.findByText("Routing saved (measured)")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Economics scope"), {
      target: { value: "conversation" },
    });
    await waitFor(() => {
      expect(screen.queryByText("Routing saved (measured)")).not.toBeInTheDocument();
    });
  });

  it("does not present repo savings as conversation spend", async () => {
    mockGetUsage.mockResolvedValue({
      session: usageSession,
      jobs: [],
    });
    mockGetEconomics.mockResolvedValue({
      ...durablePayload,
      scope: "conversation",
      savings_scope: "repo",
    });

    render(<EconomicsPane />);
    expect(await screen.findByText("Durable")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Economics scope"), {
      target: { value: "conversation" },
    });
    expect(
      await screen.findByText(/Owned jobs for this conversation/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Routing saved (measured)")).not.toBeInTheDocument();
    expect(screen.queryByText("CodeGraph (estimated)")).not.toBeInTheDocument();
  });

  it("refetches on harness-usage-refresh", async () => {
    mockGetUsage
      .mockResolvedValueOnce({
        session: {
          tokens_used: 1000,
          est_cost_usd: 0.05,
          driver: "anthropic:claude-sonnet",
          price_in: 3,
          price_out: 15,
        },
        jobs: [],
      })
      .mockResolvedValue({
        session: {
          tokens_used: 4000,
          est_cost_usd: 0.22,
          driver: "anthropic:claude-sonnet",
          price_in: 3,
          price_out: 15,
          cache_savings_usd: 0.10,
        },
        jobs: [],
      });

    render(<EconomicsPane />);
    expect(await screen.findByText("~$0.05")).toBeInTheDocument();

    act(() => {
      window.dispatchEvent(new Event("harness-usage-refresh"));
    });

    await waitFor(() => {
      expect(screen.getByText("~$0.22")).toBeInTheDocument();
    });
    expect(screen.getByText("List-price value")).toBeInTheDocument();
    expect(mockGetUsage.mock.calls.length).toBeGreaterThanOrEqual(2);
  });
});
