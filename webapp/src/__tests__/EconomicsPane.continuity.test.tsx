import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import EconomicsPane from "../components/EconomicsPane";
import { api } from "../lib/api";
import { _resetProcessUsageForTests } from "../lib/processUsage";
import { clearSWRCache } from "../lib/useStaleWhileRevalidate";
import { dispatchProjectSelected } from "../lib/panelTransition";

vi.mock("../lib/api", () => ({
  api: {
    getUsage: vi.fn(),
    getEconomics: vi.fn(),
  },
}));

const mockGetEconomics = vi.mocked(api.getEconomics);

const emptyUsage = {
  session: { tokens_used: 0, est_cost_usd: 0, driver: "", price_in: 0, price_out: 0 },
  jobs: [],
  session_total: { session_id: "", est_cost_usd: 0, input_tokens: 0, output_tokens: 0 },
};

const conversationPayload = {
  available: true,
  repo: "/repo-a",
  scope: "conversation",
  counterfactual: null,
  recent_jobs: [],
};

describe("EconomicsPane read failures and session switches", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _resetProcessUsageForTests();
    clearSWRCache();
    dispatchProjectSelected("/repo-a");
    vi.mocked(api.getUsage).mockResolvedValue(emptyUsage as any);
  });

  afterEach(() => {
    delete (window as any).__pmPendingEconomicsSelection;
    _resetProcessUsageForTests();
  });

  it("a failed read offers Retry instead of updating forever", async () => {
    mockGetEconomics.mockRejectedValue(new Error("404"));
    render(<EconomicsPane />);

    const retry = await screen.findByRole("button", { name: "Couldn't load economics for repo-a. Retry" });
    expect(screen.queryByText("Updating repo-a…")).toBeNull();

    mockGetEconomics.mockResolvedValue({ ...conversationPayload, scope: "repo" } as any);
    fireEvent.click(retry);
    const report = await screen.findByTestId("economics-report");
    expect(report.getAttribute("aria-busy")).toBe("false");
    expect(screen.queryByRole("button", { name: /Couldn't load economics/ })).toBeNull();
  });

  it("a session switch keeps the report on screen, dimmed, until the new session lands", async () => {
    mockGetEconomics.mockResolvedValue(conversationPayload as any);
    (window as any).__pmPendingEconomicsSelection = { scope: "conversation", period: "all" };
    render(<EconomicsPane />);
    expect(await screen.findByText("No owned jobs for this session.")).toBeTruthy();

    let resolveNext: (v: unknown) => void = () => {};
    mockGetEconomics.mockReturnValueOnce(new Promise((r) => { resolveNext = r; }) as any);
    act(() => {
      window.dispatchEvent(new Event("harness-session-changed"));
    });

    const report = screen.getByTestId("economics-report");
    expect(report.getAttribute("aria-busy")).toBe("true");
    expect(screen.getByText("Updating repo-a…")).toBeTruthy();

    await act(async () => {
      resolveNext(conversationPayload);
    });
    await waitFor(() => expect(screen.getByTestId("economics-report").getAttribute("aria-busy")).toBe("false"));
    expect(screen.queryByText("Updating repo-a…")).toBeNull();
  });
});
