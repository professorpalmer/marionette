import { StrictMode } from "react";
import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import EconomicsPane from "../components/EconomicsPane";
import { api } from "../lib/api";
import { dispatchProjectSelected } from "../lib/panelTransition";
import { clearSWRCache } from "../lib/useStaleWhileRevalidate";

vi.mock("../lib/api", () => ({
  api: {
    getEconomics: vi.fn(),
  },
}));

vi.mock("../lib/agentLinks", () => ({ openAgentSwarmJob: vi.fn() }));

const mockGetEconomics = vi.mocked(api.getEconomics);

describe("EconomicsPane startup requests", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearSWRCache();
    dispatchProjectSelected("/repo-a");
  });

  it("starts exactly one Economics request under the mounted StrictMode contract", async () => {
    mockGetEconomics.mockResolvedValue({
      available: true,
      repo: "/repo-a",
      scope: "repo",
      window_days: null,
      recent_jobs: [],
    });

    render(
      <StrictMode>
        <EconomicsPane />
      </StrictMode>,
    );
    await waitFor(() => expect(mockGetEconomics).toHaveBeenCalledTimes(1));
    expect(mockGetEconomics).toHaveBeenCalledWith("repo", "all");
  });
});
