import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import StatePane from "../components/StatePane";
import { api } from "../lib/api";
import {
  DEFAULT_STATE_PANE_VISIBLE_CARDS,
  STATE_PANE_VISIBLE_CARDS_KEY,
} from "../lib/statePaneVisibility";
import { clearSWRCache } from "../lib/useStaleWhileRevalidate";

vi.mock("../lib/api", () => ({
  api: {
    getCodegraph: vi.fn().mockResolvedValue({ status: "none" }),
    getWikiStatus: vi.fn().mockResolvedValue({ status: "not_configured", configured: false }),
    getWikiGraph: vi.fn().mockResolvedValue({ status: "not_configured", nodes: [], edges: [] }),
    mcp: vi.fn().mockResolvedValue({ servers: [], tools: [] }),
    environmentReadiness: vi.fn().mockResolvedValue({
      browser: { available: false, path: null, remedy: "" },
      python_analyzer: { available: false, path: null, remedy: "" },
      typescript_analyzer: { available: false, path: null, remedy: "" },
      workspace_root: "",
    }),
  },
}));

vi.mock("../components/McpPane", () => ({
  default: () => <div data-testid="mcp-pane" />,
}));

describe("StatePane card visibility selector", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    clearSWRCache();
  });

  it("renders the compact view toolbar with calm defaults", async () => {
    render(<StatePane artifacts={[]} />);

    const toolbar = screen.getByRole("toolbar", { name: "Status card visibility" });
    expect(toolbar).toBeInTheDocument();

    expect(screen.getByTestId("state-card-codegraph")).toBeInTheDocument();
    expect(screen.getByTestId("state-card-wiki")).toBeInTheDocument();
    expect(screen.queryByTestId("state-card-environment")).toBeNull();
    expect(screen.getByTestId("state-card-mcp")).toBeInTheDocument();

    expect(screen.getByRole("button", { name: "Hide CodeGraph" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Show Environment" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );

    await waitFor(() => {
      expect(api.environmentReadiness).toHaveBeenCalled();
    });
  });

  it("toggles a card and persists the choice under the versioned key", async () => {
    render(<StatePane artifacts={[]} />);

    fireEvent.click(screen.getByRole("button", { name: "Show Environment" }));
    expect(screen.getByTestId("state-card-environment")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide Environment" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    const stored = JSON.parse(localStorage.getItem(STATE_PANE_VISIBLE_CARDS_KEY) || "{}");
    expect(stored).toMatchObject({
      ...DEFAULT_STATE_PANE_VISIBLE_CARDS,
      environment: true,
    });

    fireEvent.click(screen.getByRole("button", { name: "Hide Wiki" }));
    expect(screen.queryByTestId("state-card-wiki")).toBeNull();
  });

  it("restores a saved visibility map on mount", () => {
    localStorage.setItem(
      STATE_PANE_VISIBLE_CARDS_KEY,
      JSON.stringify({
        codegraph: true,
        wiki: false,
        environment: true,
        mcp: false,
      }),
    );
    render(<StatePane artifacts={[]} />);

    expect(screen.getByTestId("state-card-codegraph")).toBeInTheDocument();
    expect(screen.queryByTestId("state-card-wiki")).toBeNull();
    expect(screen.getByTestId("state-card-environment")).toBeInTheDocument();
    expect(screen.queryByTestId("state-card-mcp")).toBeNull();
  });

  it("reveals MCP when harness-expand-mcp fires", async () => {
    localStorage.setItem(
      STATE_PANE_VISIBLE_CARDS_KEY,
      JSON.stringify({
        codegraph: true,
        wiki: true,
        environment: false,
        mcp: false,
      }),
    );
    render(<StatePane artifacts={[]} />);
    expect(screen.queryByTestId("state-card-mcp")).toBeNull();

    window.dispatchEvent(new Event("harness-expand-mcp"));
    await waitFor(() => {
      expect(screen.getByTestId("state-card-mcp")).toBeInTheDocument();
    });
    const stored = JSON.parse(localStorage.getItem(STATE_PANE_VISIBLE_CARDS_KEY) || "{}");
    expect(stored.mcp).toBe(true);
  });
});
