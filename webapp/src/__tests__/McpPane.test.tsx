import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import McpPane from "../components/McpPane";
import { api } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    mcp: vi.fn(),
    mcpCatalog: vi.fn().mockResolvedValue({ catalog: {} }),
    mcpStart: vi.fn(),
    mcpStop: vi.fn(),
    mcpRefresh: vi.fn(),
    mcpRemove: vi.fn(),
    mcpAdd: vi.fn(),
  },
}));

const mockMcp = vi.mocked(api.mcp);
const mockMcpStart = vi.mocked(api.mcpStart);

describe("McpPane last_invocation and action errors", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    mockMcp.mockResolvedValue({
      servers: [
        {
          name: "github",
          command: "npx github",
          transport: "stdio",
          running: true,
          tools: 2,
          error: "",
          last_invocation: {
            tool: "search",
            ok: false,
            error: "rate limited",
            at: "2026-07-29T12:00:00Z",
          },
        },
      ],
      tools: [],
    });
  });

  it("renders last_invocation separately from running lifecycle health", async () => {
    render(<McpPane embedded />);

    await waitFor(() => {
      expect(screen.getByText("github")).toBeInTheDocument();
    });
    expect(screen.getByText("2 tools")).toBeInTheDocument();
    expect(screen.queryByText(/Server:/)).toBeNull();
    expect(
      screen.getByText(/Last call: search failed — rate limited/),
    ).toBeInTheDocument();
    expect(
      screen.getByTitle("Last actual tool call (not server lifecycle health)"),
    ).toBeInTheDocument();
  });

  it("keeps lifecycle Server error distinct from last invocation", async () => {
    mockMcp.mockResolvedValue({
      servers: [
        {
          name: "aws",
          command: "uvx aws",
          running: false,
          tools: 0,
          error: "spawn failed",
          last_invocation: {
            tool: "list",
            ok: true,
            error: "",
            at: "2026-07-29T12:00:00Z",
          },
        },
      ],
      tools: [],
    });

    render(<McpPane embedded />);
    await waitFor(() => expect(screen.getByText("aws")).toBeInTheDocument());
    expect(screen.getByText("Server: spawn failed")).toBeInTheDocument();
    expect(screen.getByText(/Last call: list ok/)).toBeInTheDocument();
    expect(screen.getByText("stopped")).toBeInTheDocument();
  });

  it("surfaces HTTP 200 {ok:false} start failures as role=alert", async () => {
    mockMcp.mockResolvedValue({
      servers: [
        {
          name: "github",
          command: "npx github",
          running: false,
          tools: 0,
          error: "",
        },
      ],
      tools: [],
    });
    mockMcpStart.mockResolvedValue({ ok: false, error: "handshake timed out" });

    render(<McpPane embedded />);
    await waitFor(() => expect(screen.getByTitle("Start")).toBeInTheDocument());

    fireEvent.click(screen.getByTitle("Start"));
    await waitFor(() => {
      const alert = screen.getByRole("alert");
      expect(alert).toHaveTextContent("Action failed: handshake timed out");
    });
    expect(mockMcpStart).toHaveBeenCalledWith("github");
  });

  it("never renders raw secrets from lifecycle Server error or action failures", async () => {
    mockMcp.mockResolvedValue({
      servers: [
        {
          name: "github",
          command: "npx github",
          running: false,
          tools: 0,
          error:
            "auth failed: Bearer REDACTED token=REDACTED ghp_REDACTED",
          last_invocation: {
            tool: "search",
            ok: false,
            error: "Authorization REDACTED api_key=REDACTED",
            at: "2026-07-29T12:00:00Z",
          },
        },
      ],
      tools: [],
    });
    mockMcpStart.mockResolvedValue({
      ok: false,
      error:
        "handshake: Bearer REDACTED sk-REDACTED github_pat_REDACTED token=REDACTED",
    });

    render(<McpPane embedded />);
    await waitFor(() => expect(screen.getByText("github")).toBeInTheDocument());

    expect(screen.getByText(/Server:/)).toBeInTheDocument();
    expect(screen.getByText(/Last call: search failed/)).toBeInTheDocument();
    for (const needle of [
      "sk-or-v1-",
      "ghp_abcdefghijklmnopqrstuv",
      "github_pat_11",
      "super-secret",
      "Bearer sk-",
    ]) {
      expect(document.body.textContent).not.toContain(needle);
    }

    fireEvent.click(screen.getByTitle("Start"));
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/Action failed:/);
    });
    for (const needle of [
      "sk-or-v1-",
      "ghp_abcdefghijklmnopqrstuv",
      "github_pat_11",
      "Bearer sk-",
    ]) {
      expect(screen.getByRole("alert").textContent).not.toContain(needle);
    }
    expect(screen.getByRole("alert").textContent).toContain("REDACTED");
  });
});
