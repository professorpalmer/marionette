import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import EconomicsPane from "../components/EconomicsPane";
import { api } from "../lib/api";
import { openAgentSwarmJob } from "../lib/agentLinks";
import { clearSWRCache } from "../lib/useStaleWhileRevalidate";
import { dispatchProjectSelected } from "../lib/panelTransition";

vi.mock("../lib/api", () => ({
  api: {
    getEconomics: vi.fn(),
  },
}));

vi.mock("../lib/agentLinks", () => ({ openAgentSwarmJob: vi.fn() }));

const mockGetEconomics = vi.mocked(api.getEconomics);
const mockOpenAgentSwarmJob = vi.mocked(openAgentSwarmJob);

const durablePayload = {
  available: true,
  repo: "/repo-a",
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

async function chooseScope(value: "conversation" | "repo" | "all_projects") {
  fireEvent.change(await screen.findByLabelText("Economics ownership"), { target: { value } });
}

describe("EconomicsPane", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearSWRCache();
    dispatchProjectSelected("/repo-a");
    mockGetEconomics.mockImplementation(async (scope = "repo") => ({
      ...durablePayload,
      scope,
      all_projects: scope === "all_projects",
    }));
  });

  it("removes the previous repo receipt immediately while the selected repo loads", async () => {
    let selectedRepo = "/repo-a";
    let resolveRepoB: ((value: typeof durablePayload) => void) | undefined;
    mockGetEconomics.mockImplementation(async () => {
      if (selectedRepo === "/repo-b") {
        return new Promise<typeof durablePayload>((resolve) => { resolveRepoB = resolve; });
      }
      return durablePayload;
    });

    render(<EconomicsPane />);
    expect(await screen.findByText("$0.32")).toBeTruthy();

    selectedRepo = "/repo-b";
    act(() => dispatchProjectSelected("/repo-b"));

    expect(await screen.findByText("Updating repo-b…")).toBeTruthy();
    expect(screen.queryByText("$0.32")).toBeNull();

    await act(async () => {
      resolveRepoB?.({
        ...durablePayload,
        repo: "/repo-b",
        counterfactual: {
          ...durablePayload.counterfactual,
          actual_cost_usd: 0.5,
        },
      });
    });
    expect(await screen.findByText("$0.50")).toBeTruthy();
  });

  it("ignores a previous report response that arrives after the selected scope", async () => {
    let resolveConversation: ((value: typeof durablePayload) => void) | undefined;
    mockGetEconomics.mockImplementation(async (scope = "repo") => {
      if (scope === "conversation") {
        return new Promise<typeof durablePayload>((resolve) => { resolveConversation = resolve; });
      }
      const actualCost = scope === "all_projects" ? 0.75 : 0.316217;
      return {
        ...durablePayload,
        scope,
        all_projects: scope === "all_projects",
        counterfactual: { ...durablePayload.counterfactual, actual_cost_usd: actualCost },
      };
    });

    render(<EconomicsPane />);
    expect(await screen.findByText("$0.32")).toBeTruthy();

    await chooseScope("conversation");
    await waitFor(() => expect(resolveConversation).toBeTypeOf("function"));

    await chooseScope("all_projects");
    expect(await screen.findByText("$0.75")).toBeTruthy();

    await act(async () => {
      resolveConversation?.({ ...durablePayload, scope: "conversation" });
    });
    expect(screen.getByText("$0.75")).toBeTruthy();
    expect(screen.queryByText("Updating repo-a…")).toBeNull();
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
    expect(screen.queryByRole("option", { name: "This app run" })).toBeNull();
    expect(screen.getByRole("option", { name: "This session" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "This conversation" })).toBeNull();
    expect(container.querySelectorAll(".overflow-y-auto")).toHaveLength(1);
    expect(screen.queryByText("Why you saved")).toBeNull();
    expect(screen.queryByText("Additional estimates")).toBeNull();
    expect(screen.queryByText("This repo · all time")).toBeNull();
    expect((await screen.findByText("Job receipts")).closest("details")).toBeNull();
    expect(screen.queryByText("Recent jobs")).toBeNull();
    const comparator = screen.getByText(/Compared with/);
    expect(comparator.textContent).toContain("codex/gpt-5-5");
    expect(screen.getByText("Measured usage cost").closest("section")?.contains(comparator)).toBe(true);
    expect(comparator.textContent).toContain("Savings are estimates, not cash back.");
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
    expect(screen.getByText("No job receipts in this scope.")).toBeTruthy();
  });

  it("keeps a routing-only forecast outside the terminal spend hero", async () => {
    mockGetEconomics.mockResolvedValue({
      ...durablePayload,
      counterfactual_source: "routing_report",
      counterfactual_status: "routing_report",
      counterfactual: {
        ...durablePayload.counterfactual,
        spend_basis: "preflight_routing_estimate",
      },
    });

    render(<EconomicsPane />);

    expect(await screen.findByText("Cost unavailable")).toBeTruthy();
    expect(screen.getByText("No terminal job receipts for this scope.")).toBeTruthy();
    expect(screen.getByText("Route forecast")).toBeTruthy();
    expect(screen.getByText("Estimated frontier forecast")).toBeTruthy();
    expect(screen.getByText("Estimated difference")).toBeTruthy();
    expect(screen.getByText(/Forecasts are predictions, not spend/)).toBeTruthy();
    expect(screen.queryByText("Measured usage cost")).toBeNull();
    expect(screen.queryByText("Estimated savings")).toBeNull();
    expect(screen.queryByText("Less than frontier")).toBeNull();
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

  it("shows PM measured and estimated buckets separately for mixed usage", async () => {
    mockGetEconomics.mockResolvedValue({
      ...durablePayload,
      counterfactual: {
        ...durablePayload.counterfactual,
        actual_cost_usd: 0.3,
        naive_cost_usd: 0.5,
        avoided_usd: 0.2,
        measured_cost_usd: 0.2,
        estimated_cost_usd: 0.1,
        spend_basis: "mixed",
      },
      recent_jobs_total: 1,
      recent_jobs: [{
        job_id: "job_mixed",
        status: "complete",
        accounting_owned: true,
        models: [{ model_id: "codex/gpt-5-5" }],
        actual_marginal_usd: 0.3,
        measured_cost_usd: 0.2,
        estimated_cost_usd: 0.1,
        cost_basis: "mixed",
        counterfactual: {
          reference_model_id: "codex/gpt-5-5",
          reference_priced: true,
          avoided_usd: 0.2,
        },
      }],
    });

    render(<EconomicsPane />);

    const hero = (await screen.findByText("Usage cost")).closest("section") as HTMLElement;
    const heroMeasured = within(hero).getByText("Measured usage");
    const heroEstimated = within(hero).getByText("Estimated usage");
    expect(within(heroMeasured.parentElement as HTMLElement).getByText("$0.20")).toBeTruthy();
    expect(within(heroEstimated.parentElement as HTMLElement).getByText("~$0.10")).toBeTruthy();
    expect(within(hero).getByText("~$0.30")).toBeTruthy();

    const row = within((screen.getByText("job_mixed")).closest(".border-t") as HTMLElement);
    const rowMeasured = row.getByText("Measured usage");
    const rowEstimated = row.getByText("Estimated usage");
    expect(within(rowMeasured.parentElement as HTMLElement).getByText("$0.20")).toBeTruthy();
    expect(within(rowEstimated.parentElement as HTMLElement).getByText("~$0.10")).toBeTruthy();
  });

  it("renders a completed measured zero-token PM receipt as known zero", async () => {
    mockGetEconomics.mockResolvedValue({
      ...durablePayload,
      counterfactual: {
        ...durablePayload.counterfactual,
        actual_cost_usd: 0,
        naive_cost_usd: 0,
        avoided_usd: 0,
        jobs: 1,
        tasks: 1,
        measured_cost_usd: 0,
        estimated_cost_usd: 0,
        spend_basis: "measured_usage_x_registry_price",
      },
      recent_jobs_total: 1,
      recent_jobs: [{
        job_id: "job_measured_zero",
        status: "complete",
        accounting_owned: true,
        models: [{
          model_id: "openai/gpt-5-4-mini",
          billing: "api",
          calls: 1,
          tokens_in: 0,
          tokens_out: 0,
        }],
        actual_marginal_usd: 0,
        measured_cost_usd: 0,
        estimated_cost_usd: 0,
        cost_basis: "measured",
        priced_tasks: 1,
        measured_runs: 1,
        estimated_runs: 0,
        counterfactual: {
          reference_model_id: "codex/gpt-5-5",
          reference_priced: true,
          naive_cost_usd: 0,
          actual_cost_usd: 0,
          avoided_usd: 0,
        },
      }],
    });

    render(<EconomicsPane />);

    expect(await screen.findByText("1 jobs considered")).toBeTruthy();
    expect(screen.getByText("1 priced tasks")).toBeTruthy();
    const row = within(screen.getByText("job_measured_zero").closest(".border-t") as HTMLElement);
    expect(row.getByText("Measured usage")).toBeTruthy();
    expect(row.getByText("$0.00")).toBeTruthy();
    expect(row.queryByText("Cost unavailable")).toBeNull();
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


  it("requests the chosen period for the selected ownership", async () => {
    render(<EconomicsPane />);
    fireEvent.change(await screen.findByLabelText("Economics period"), { target: { value: "30" } });

    await waitFor(() => {
      expect(mockGetEconomics).toHaveBeenCalledWith("repo", 30);
    });
  });

  it("opens the status-bar destination at This session and All time", async () => {
    render(<EconomicsPane />);
    await chooseScope("all_projects");
    fireEvent.change(screen.getByLabelText("Economics period"), { target: { value: "30" } });

    act(() => {
      window.dispatchEvent(new CustomEvent("harness-economics-selection", {
        detail: { scope: "conversation", period: "all" },
      }));
    });

    await waitFor(() => {
      expect((screen.getByLabelText("Economics ownership") as HTMLSelectElement).value).toBe("conversation");
      expect((screen.getByLabelText("Economics period") as HTMLSelectElement).value).toBe("all");
      expect(mockGetEconomics).toHaveBeenCalledWith("conversation", "all");
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
    expect(measured.getByText("$0.02").className).toContain("text-txt");
    expect(measured.getByText(/Estimated savings ~\$4.78/).className).toContain("text-good/65");
    expect(measured.queryByText("$0.00")).toBeNull();
    expect(measured.getByRole("button", { name: "job_measured" }).className).toContain("text-accent");
    expect(screen.getByText("No billable worker ran")).toBeTruthy();
    expect(screen.getByText("Visible only")).toBeTruthy();
    expect(screen.getByText("Showing 3 of 381 jobs in this scope")).toBeTruthy();
  });

  it("opens a job receipt in Swarm Tracker", async () => {
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
          repo: "/repo-a",
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

    expect(await screen.findByText(/full comparison is not available for this session/)).toBeTruthy();
    expect(screen.queryByText("Estimated frontier cost")).toBeNull();
    expect(screen.queryByText("Additional estimates")).toBeNull();
    expect(screen.getByText("Job receipts")).toBeTruthy();
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


});
