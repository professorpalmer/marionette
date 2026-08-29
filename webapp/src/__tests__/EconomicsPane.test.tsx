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
  },
}));

vi.mock("../lib/usePolling", () => ({
  usePolling: (fn: () => unknown) => { void fn(); },
}));

vi.mock("../lib/agentLinks", () => ({ openAgentSwarmJob: vi.fn() }));

const mockGetUsage = vi.mocked(api.getUsage);
const mockGetEconomics = vi.mocked(api.getEconomics);
const mockOpenAgentSwarmJob = vi.mocked(openAgentSwarmJob);

const usageSession = {
  tokens_used: 8_000,
  est_cost_usd: 0.12,
  driver: "anthropic:claude-sonnet",
  cache_savings_usd: 0.04,
  cache_savings_basis: "catalog" as const,
  tool_output_tokens_saved: 736_100,
  tool_output_savings_usd: 0.02,
} as any;

const durablePayload = {
  available: true,
  scope: "repo",
  window_days: null,
  all_projects: false,
  savings: {
    jobs_considered: 381,
    routing: { saved_usd: 0.62, plan_routed_tasks: 61 },
    codegraph: { dollars_saved_est: 4.54 },
    counterfactual: { reference_model_id: "codex/gpt-5-5" },
  },
  counterfactual: {
    reference_model_id: "codex/gpt-5-5",
    reference_priced: true,
    actual_cost_usd: 0.316217,
    naive_cost_usd: 4.237335,
    avoided_usd: 3.921118,
    tasks: 78,
    jobs: 7,
    measured_cost_usd: 0.316217,
    estimated_cost_usd: 0,
    spend_basis: "measured_usage_x_registry_price",
  },
  counterfactual_source: "job_financial_reports",
  counterfactual_status: "ok",
  recent_jobs_total: 381,
  recent_jobs: [],
};

async function chooseScope(value: "app_run" | "conversation" | "repo" | "all_projects") {
  fireEvent.change(await screen.findByLabelText("Economics ownership"), { target: { value } });
}

describe("EconomicsPane", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearSWRCache();
    mockGetUsage.mockResolvedValue({ session: usageSession, jobs: [] });
    mockGetEconomics.mockImplementation(async (scope = "repo") => ({
      ...durablePayload,
      scope,
      all_projects: scope === "all_projects",
    }));
  });

  it("matches the receipt-first control and disclosure hierarchy", async () => {
    mockGetEconomics.mockResolvedValue({
      ...durablePayload,
      recent_jobs: [{
        job_id: "job_layout_proof",
        status: "complete",
        accounting_owned: true,
        models: [{ model_id: "codex/gpt-5-4-mini" }],
        measured_cost_usd: 0.02,
        counterfactual: { avoided_usd: 1.25 },
      }],
    });

    const { container } = render(<EconomicsPane />);

    expect(await screen.findByText("Economics")).toBeTruthy();
    const scope = screen.getByLabelText("Economics ownership");
    const period = screen.getByLabelText("Economics period");
    expect(scope.parentElement).toBe(period.parentElement);
    expect(container.querySelectorAll(".overflow-y-auto")).toHaveLength(1);
    expect(screen.queryByText("Why you saved")).toBeNull();
    expect(screen.queryByText("Additional estimates")).toBeNull();
    expect(screen.getByText("Recent jobs").closest("details")).toBeNull();
    expect(screen.getByText(/Compared with/).textContent).toContain("codex/gpt-5-5");
    expect(screen.getByText(/Savings are estimates, not cash back/)).toBeTruthy();
    expect(screen.queryByText("Context health")).toBeNull();
  });

  it("shows one full-scope PM receipt", async () => {
    render(<EconomicsPane />);

    expect(await screen.findByText("Measured usage cost")).toBeTruthy();
    expect(screen.getByText("$0.32")).toBeTruthy();
    expect(screen.getByText("Estimated frontier cost")).toBeTruthy();
    expect(screen.getByText("~$4.24")).toBeTruthy();
    expect(screen.getByText("Estimated savings")).toBeTruthy();
    expect(screen.getByText("~$3.92")).toBeTruthy();
    expect(screen.getByText("92.5%")).toBeTruthy();
    expect(screen.getByText("7 jobs considered")).toBeTruthy();
    expect(screen.getByText("78 priced tasks")).toBeTruthy();
    expect(screen.getByText("No recent jobs.")).toBeTruthy();
  });

  it("keeps a zero-dollar plan receipt included instead of measured", async () => {
    mockGetEconomics.mockResolvedValue({
      ...durablePayload,
      counterfactual: {
        ...durablePayload.counterfactual,
        actual_cost_usd: 0,
        avoided_usd: 4.237335,
        measured_cost_usd: 0,
        estimated_cost_usd: 0,
        spend_basis: "plan",
      },
      recent_jobs: [{
        job_id: "job_plan",
        status: "complete",
        accounting_owned: true,
        models: [{ model_id: "codex/gpt-5-5", billing: "plan" }],
        actual_marginal_usd: 0,
        measured_cost_usd: 0,
        estimated_cost_usd: 0,
        cost_basis: "plan",
        counterfactual: {
          reference_model_id: "codex/gpt-5-5",
          reference_priced: true,
          avoided_usd: 4.237335,
        },
      }],
    });

    render(<EconomicsPane />);

    expect(await screen.findAllByText("Included in your plan")).toHaveLength(2);
    expect(screen.queryByText("Measured usage cost")).toBeNull();
    expect(screen.queryByText("Route forecast")).toBeNull();
  });

  it("shows an honest warning when job reports do not reconcile", async () => {
    mockGetEconomics.mockResolvedValue({
      ...durablePayload,
      counterfactual: null,
      counterfactual_source: "unavailable",
      counterfactual_status: "receipt_mismatch",
    });

    render(<EconomicsPane />);

    expect(await screen.findByText(/job reports do not agree/)).toBeTruthy();
    expect(screen.queryByText("Estimated savings")).toBeNull();
  });

  it("switches to one app-run receipt without durable sections", async () => {
    render(<EconomicsPane />);
    await chooseScope("app_run");

    expect(await screen.findByText(/Spend and savings since you opened Marionette/)).toBeTruthy();
    expect(screen.getByText("Spend")).toBeTruthy();
    expect(screen.getByText("~$0.12")).toBeTruthy();
    expect(screen.getByText("Without savings")).toBeTruthy();
    expect(screen.getByText("~$0.18")).toBeTruthy();
    expect(screen.getByText("~$0.06")).toBeTruthy();
    expect(screen.getByText("33.3%")).toBeTruthy();
    expect(screen.queryByText("Why you saved")).toBeNull();
    expect(screen.getByLabelText("Economics period").hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("option", { name: "Since launch" })).toBeTruthy();
    expect(screen.queryByText("Recent jobs")).toBeNull();
  });

  it("requests the chosen period for the selected ownership", async () => {
    render(<EconomicsPane />);
    fireEvent.change(await screen.findByLabelText("Economics period"), { target: { value: "30" } });

    await waitFor(() => {
      expect(mockGetEconomics).toHaveBeenCalledWith("repo", 30);
    });
  });

  it("renders job evidence without meaningless zero rows", async () => {
    mockGetEconomics.mockResolvedValue({
      ...durablePayload,
      recent_jobs: [
        {
          job_id: "job_measured",
          status: "complete",
          accounting_owned: true,
          models: [{ model_id: "codex/gpt-5-4-mini" }],
          measured_cost_usd: 0.018,
          estimated_cost_usd: 0,
          counterfactual: {
            reference_model_id: "codex/gpt-5-5",
            reference_priced: true,
            avoided_usd: 4.78,
          },
        },
        {
          job_id: "job_failed",
          status: "failed",
          accounting_owned: true,
          models: [],
          measured_cost_usd: 0,
          estimated_cost_usd: 0,
          actual_marginal_usd: 0,
          counterfactual: { avoided_usd: 0 },
        },
        {
          job_id: "job_visible",
          status: "complete",
          accounting_owned: false,
          models: [{ model_id: "openai/gpt-5-4-mini" }],
        },
      ],
    });

    render(<EconomicsPane />);

    const measured = within((await screen.findByText("job_measured")).closest(".border-t") as HTMLElement);
    expect(measured.getByText("Measured usage")).toBeTruthy();
    expect(measured.getByText("$0.02").className).toContain("text-warn/90");
    expect(measured.getByText(/Estimated savings ~\$4.78/).className).toContain("text-good/90");
    expect(measured.queryByText("$0.00")).toBeNull();
    expect(measured.getByRole("button", { name: "job_measured" }).className).toContain("text-blue-400");
    expect(screen.getByText("No billable worker ran")).toBeTruthy();
    expect(screen.getByText("Visible only")).toBeTruthy();
    expect(screen.getByText("Showing 3 of 381")).toBeTruthy();
  });

  it("opens a recent job in Swarm Tracker", async () => {
    mockGetEconomics.mockResolvedValue({
      ...durablePayload,
      recent_jobs: [{
        job_id: "job_abcdef012345",
        status: "complete",
        accounting_owned: true,
        measured_cost_usd: 0.02,
      }],
    });

    render(<EconomicsPane />);
    fireEvent.click(await screen.findByRole("button", { name: "job_abcdef012345" }));
    expect(mockOpenAgentSwarmJob).toHaveBeenCalledWith("job_abcdef012345");
  });

  it("keeps conversation scope honest when no full comparison exists", async () => {
    mockGetEconomics.mockImplementation(async (scope = "repo") => scope === "conversation"
      ? {
          available: true,
          scope: "conversation",
          savings: null,
          counterfactual: null,
          recent_jobs: [{
            job_id: "job_conversation",
            status: "complete",
            accounting_owned: true,
            measured_cost_usd: 0.02,
          }],
        }
      : { ...durablePayload, scope });

    render(<EconomicsPane />);
    await chooseScope("conversation");

    expect(await screen.findByText(/full comparison is not available/)).toBeTruthy();
    expect(screen.queryByText("Estimated frontier cost")).toBeNull();
    expect(screen.queryByText("Additional estimates")).toBeNull();
    expect(screen.getByText("Recent jobs")).toBeTruthy();
  });

  it("clears durable state on a soft API rejection", async () => {
    mockGetEconomics.mockImplementation(async (scope = "repo") => scope === "conversation"
      ? { ok: false, error: "unsupported scope" }
      : { ...durablePayload, scope });

    render(<EconomicsPane />);
    expect(await screen.findByText("Measured usage cost")).toBeTruthy();
    await chooseScope("conversation");

    await waitFor(() => expect(screen.queryByText("Measured usage cost")).toBeNull());
  });

  it("refreshes app-run values on harness-usage-refresh", async () => {
    mockGetUsage.mockResolvedValue({
      session: { tokens_used: 1_000, est_cost_usd: 0.05, driver: "anthropic:claude-sonnet" } as any,
      jobs: [],
    });
    render(<EconomicsPane />);
    await chooseScope("app_run");
    expect((await screen.findAllByText("~$0.05")).length).toBeGreaterThanOrEqual(2);

    mockGetUsage.mockResolvedValue({
      session: {
        tokens_used: 4_000,
        est_cost_usd: 0.22,
        driver: "anthropic:claude-sonnet",
        cache_savings_usd: 0.10,
      } as any,
      jobs: [],
    });
    act(() => { window.dispatchEvent(new Event("harness-usage-refresh")); });

    await waitFor(() => expect(screen.getAllByText("~$0.22").length).toBeGreaterThanOrEqual(1));
    expect(screen.queryByText("Why you saved")).toBeNull();
  });
});
