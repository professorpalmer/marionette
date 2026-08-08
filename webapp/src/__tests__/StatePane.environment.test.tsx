import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import StatePane from "../components/StatePane";
import { api } from "../lib/api";
import { clearSWRCache } from "../lib/useStaleWhileRevalidate";

vi.mock("../lib/api", () => ({
  api: {
    getCodegraph: vi.fn().mockResolvedValue({ status: "missing" }),
    getWikiStatus: vi.fn().mockResolvedValue({ status: "not_configured", configured: false }),
    getWikiGraph: vi.fn().mockResolvedValue({ status: "not_configured", nodes: [], edges: [] }),
    mcp: vi.fn().mockResolvedValue({ servers: [], tools: [] }),
    environmentReadiness: vi.fn(),
  },
}));

vi.mock("../components/McpPane", () => ({
  default: () => <div data-testid="mcp-pane" />,
}));

const mockEnv = vi.mocked(api.environmentReadiness);

const readyPayload = {
  browser: {
    available: true,
    path: "/usr/bin/google-chrome",
    remedy: "",
  },
  python_analyzer: {
    available: false,
    path: null,
    remedy: "Python analyzer unavailable: install pyright on PATH",
  },
  typescript_analyzer: {
    available: false,
    path: null,
    remedy: "TypeScript analyzer unavailable: install typescript in the workspace",
  },
  workspace_root: "/tmp/ws",
};

describe("StatePane environment readiness", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    clearSWRCache();
    localStorage.setItem("pmharness.statePane.envOpen", "1");
    // Environment is hidden in the calm default — force it on for these tests.
    localStorage.setItem(
      "pmharness.statePane.visibleCards.v1",
      JSON.stringify({
        codegraph: true,
        wiki: true,
        environment: true,
        mcp: true,
      }),
    );
    mockEnv.mockResolvedValue(readyPayload);
  });

  it("loads cached readiness on mount (no refresh=true) and shows available/missing remedies", async () => {
    render(<StatePane artifacts={[]} />);

    await waitFor(() => {
      expect(mockEnv).toHaveBeenCalled();
    });
    expect(mockEnv.mock.calls[0]?.[0]).toBeUndefined();

    await waitFor(() => {
      expect(screen.getByText("available")).toBeInTheDocument();
    });
    expect(screen.getByText("/usr/bin/google-chrome")).toBeInTheDocument();
    expect(screen.getAllByText("unavailable").length).toBeGreaterThanOrEqual(2);
    expect(
      screen.getByText(/Python analyzer unavailable: install pyright/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/TypeScript analyzer unavailable: install typescript/),
    ).toBeInTheDocument();
  });

  it("reserves refresh=true for the explicit Refresh button", async () => {
    render(<StatePane artifacts={[]} />);
    await waitFor(() => expect(screen.getByText("Refresh")).toBeInTheDocument());

    mockEnv.mockClear();
    fireEvent.click(screen.getByText("Refresh"));
    await waitFor(() => {
      expect(mockEnv).toHaveBeenCalledWith({ refresh: true });
    });
  });

  it("sets aria-expanded on the Environment toggle", async () => {
    render(<StatePane artifacts={[]} />);
    const toggle = await screen.findByTitle("Hide environment readiness");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("surfaces readiness fetch failures with a visible role=alert", async () => {
    mockEnv.mockRejectedValue(new Error("readiness probe down"));
    render(<StatePane artifacts={[]} />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("readiness probe down");
    });
  });

  it("does not force refresh=true on config/session events", async () => {
    render(<StatePane artifacts={[]} />);
    await waitFor(() => expect(mockEnv).toHaveBeenCalled());
    mockEnv.mockClear();

    window.dispatchEvent(new Event("harness-config-changed"));
    window.dispatchEvent(new Event("harness-new-session"));

    await waitFor(() => {
      expect(mockEnv.mock.calls.length).toBeGreaterThanOrEqual(1);
    });
    for (const args of mockEnv.mock.calls) {
      expect(args[0]).toBeUndefined();
    }
  });
});
