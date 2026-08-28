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

  it("puts the persistent header first and owns one scroll viewport", async () => {
    mockGetUsage.mockResolvedValue({
      session: usageSession,
      jobs: [],
    });

    const { container } = render(<EconomicsPane />);

    expect(await screen.findByText("Spend and savings")).toBeInTheDocument();
    expect(screen.getByText("Economics")).toBeInTheDocument();
    expect(container.firstElementChild).toHaveClass("flex", "flex-col", "h-full", "overflow-hidden");
    const durable = screen.getByText("Spend and savings");
    const econ = screen.getByText("Economics");
    expect(econ.compareDocumentPosition(durable) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(econ.closest(".shrink-0")).toBeTruthy();
    const scrollViews = container.querySelectorAll(".overflow-y-auto");
    expect(scrollViews).toHaveLength(1);
    expect(scrollViews[0]?.contains(durable)).toBe(true);
    expect(screen.queryByText(/Spend and savings since you opened Marionette/)).not.toBeInTheDocument();
    expect(screen.getByText(/Worker spend compared with the selected frontier model/)).toBeInTheDocument();

    fireEvent.change(await screen.findByLabelText("Economics ownership"), { target: { value: "app_run" } });
    expect(await screen.findByText(/Spend and savings since you opened Marionette/)).toBeInTheDocument();
    expect(screen.queryByText("Spend and savings")).not.toBeInTheDocument();
  });

  it("keeps the last Economics data visible across card remounts", async () => {
    mockGetUsage.mockResolvedValue({ session: usageSession, jobs: [] });

    const first = render(<EconomicsPane />);
    fireEvent.change(await screen.findByLabelText("Economics ownership"), { target: { value: "app_run" } });
    expect(await screen.findByText(/Spend and savings since you opened Marionette/)).toBeInTheDocument();
    first.unmount();
    mockGetUsage.mockImplementation(() => new Promise(() => {}));

    render(<EconomicsPane />);
    fireEvent.change(await screen.findByLabelText("Economics ownership"), { target: { value: "app_run" } });

    expect(screen.getByText(/Spend and savings since you opened Marionette/)).toBeInTheDocument();
    expect(screen.queryByText("Loading this app run…")).not.toBeInTheDocument();
  });

  it("shows one app-run receipt and keeps context diagnostics out", async () => {
    mockGetUsage.mockResolvedValue({
      session: usageSession,
      jobs: [],
    });

    render(<EconomicsPane />);
    fireEvent.change(await screen.findByLabelText("Economics ownership"), { target: { value: "app_run" } });

    expect(await screen.findByText("Spend")).toBeInTheDocument();
    expect(screen.getByText("~$0.12")).toBeInTheDocument();
    expect(screen.getByText("Without savings")).toBeInTheDocument();
    expect(screen.getByText("~$0.18")).toBeInTheDocument();
    expect(screen.getByText("Estimated savings")).toBeInTheDocument();
    expect(screen.getByText("~$0.06")).toBeInTheDocument();
    expect(screen.getByText("33.3%")).toBeInTheDocument();
    expect(screen.getByText("Why you saved")).toBeInTheDocument();
    expect(screen.queryByText("Context health")).not.toBeInTheDocument();
    expect(screen.queryByText("Memory layers")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Compact now" })).not.toBeInTheDocument();
  });

  it("shows durable heading, reference model, and scope control", async () => {
    mockGetUsage.mockResolvedValue({
      session: usageSession,
      jobs: [],
    });

    render(<EconomicsPane />);

    expect(await screen.findByText("Spend and savings")).toBeInTheDocument();
    expect(screen.getAllByText(/anthropic\/claude-opus-4/).length).toBeGreaterThan(0);
    expect(screen.getByLabelText("Economics ownership")).toBeInTheDocument();
    expect(screen.getByLabelText("Economics period")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "This repo" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Last 30 days" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Last 30 days" }).closest("select")).toHaveAttribute(
      "aria-label",
      "Economics period",
    );
    expect(screen.getByText("Routing saved (measured)")).toBeInTheDocument();
    expect(screen.getByText("CodeGraph (estimated)")).toBeInTheDocument();
    expect(screen.queryByText("Session cost")).not.toBeInTheDocument();
  });

  it("leads durable economics with one full-scope PM receipt", async () => {
    mockGetUsage.mockResolvedValue({ session: usageSession, jobs: [] });
    mockGetEconomics.mockResolvedValue({
      ...durablePayload,
      savings: {
        ...durablePayload.savings,
        jobs_considered: 381,
      },
      counterfactual: {
        reference_model_id: "codex/gpt-5-5",
        reference_priced: true,
        actual_cost_usd: 0.316217,
        naive_cost_usd: 4.237335,
        avoided_usd: 3.921118,
        tasks: 78,
      },
      owned_jobs_considered: 7,
      owned_actual_marginal_usd: 0.042856,
      owned_avoided_usd: 11.341534,
    });

    render(<EconomicsPane />);

    expect(await screen.findByText("Worker spend")).toBeTruthy();
    expect(screen.getByText("$0.32")).toBeTruthy();
    expect(screen.getByText("Frontier equivalent")).toBeTruthy();
    expect(screen.getByText("$4.24")).toBeTruthy();
    expect(screen.getByText("Estimated savings")).toBeTruthy();
    expect(screen.getByText("$3.92")).toBeTruthy();
    expect(screen.getByText("92.5%")).toBeTruthy();
    expect(screen.getByText(/381 jobs considered/).textContent).toContain("78 priced tasks");
    expect(screen.queryByText("$11.34")).toBeNull();
  });

  it("requests Last 30 days as a period on the selected ownership", async () => {
    mockGetUsage.mockResolvedValue({
      session: usageSession,
      jobs: [],
    });

    render(<EconomicsPane />);
    expect(await screen.findByText("Spend and savings")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Economics period"), {
      target: { value: "30" },
    });
    await waitFor(() => {
      expect(mockGetEconomics).toHaveBeenCalledWith("repo", 30);
    });
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
    expect(ownedScope.getByText("Measured usage cost")).toBeInTheDocument();
    expect(ownedScope.queryByText("Estimated cost")).not.toBeInTheDocument();
    expect(ownedScope.getByText("Vs reference").parentElement).toHaveTextContent("Vs reference$2.00");
    expect(ownedScope.getAllByText("$1.25").length).toBeGreaterThanOrEqual(1);
    expect(ownedScope.queryByText("—")).toBeNull();
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
    expect(screen.getByText("Measured usage cost")).toBeInTheDocument();
    expect(screen.getByText("Estimated cost")).toBeInTheDocument();
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
    expect(row.getByText("Measured usage cost").parentElement).toHaveTextContent("Measured usage cost$2.00");
    expect(row.queryByText("Estimated cost")).not.toBeInTheDocument();
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
    expect(row.queryByText("Measured usage cost")).not.toBeInTheDocument();
    expect(row.getByText("Estimated cost").parentElement).toHaveTextContent("Estimated cost$0.75");
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
    const row = within(screen.getByText("legacy-1").closest(".mb-2") as HTMLElement);
    expect(row.getByText("Measured usage cost").parentElement).toHaveTextContent("Measured usage cost$1.10");
    expect(row.queryByText("Estimated cost")).not.toBeInTheDocument();
  });

  it("explains a failed zero-work job instead of rendering three zero dollars", async () => {
    mockGetUsage.mockResolvedValue({ session: usageSession, jobs: [] });
    mockGetEconomics.mockResolvedValue({
      ...durablePayload,
      recent_jobs: [{
        job_id: "failed-before-worker",
        status: "failed",
        accounting_owned: true,
        models: [],
        measured_cost_usd: 0,
        estimated_cost_usd: 0,
        actual_marginal_usd: 0,
        cost_basis: "measured",
        counterfactual: { avoided_usd: 0 },
      }],
    });

    render(<EconomicsPane />);

    const row = within((await screen.findByText("failed-before-worker")).closest(".mb-2") as HTMLElement);
    expect(row.getByText("No billable worker ran")).toBeTruthy();
    expect(row.queryByText("$0.00")).toBeNull();
    expect(row.queryByText("Vs reference")).toBeNull();
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

    fireEvent.change(screen.getByLabelText("Economics ownership"), {
      target: { value: "conversation" },
    });
    await waitFor(() => {
      expect(screen.queryByText("Routing saved (measured)")).not.toBeInTheDocument();
    });
    expect(
      mockGetEconomics.mock.calls.some((call) => call[0] === "conversation"),
    ).toBe(true);
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
    expect(await screen.findByText("Spend and savings")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Economics ownership"), {
      target: { value: "conversation" },
    });
    expect(
      await screen.findByText(/Jobs started from this conversation/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Routing saved (measured)")).not.toBeInTheDocument();
    expect(screen.queryByText("CodeGraph (estimated)")).not.toBeInTheDocument();
  });

  it("refetches on harness-usage-refresh", async () => {
    mockGetUsage.mockResolvedValue({
      session: {
        tokens_used: 1000,
        est_cost_usd: 0.05,
        driver: "anthropic:claude-sonnet",
        price_in: 3,
        price_out: 15,
      },
      jobs: [],
    });

    render(<EconomicsPane />);
    fireEvent.change(await screen.findByLabelText("Economics ownership"), { target: { value: "app_run" } });
    expect((await screen.findAllByText("~$0.05")).length).toBeGreaterThanOrEqual(2);

    mockGetUsage.mockResolvedValue({
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
    act(() => {
      window.dispatchEvent(new Event("harness-usage-refresh"));
    });

    await waitFor(() => {
      expect(screen.getAllByText("~$0.22").length).toBeGreaterThanOrEqual(1);
    });
    expect(screen.getByText("Why you saved")).toBeInTheDocument();
    expect(mockGetUsage.mock.calls.length).toBeGreaterThanOrEqual(2);
  });
});
