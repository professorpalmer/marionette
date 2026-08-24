import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PluginsLibrary from "../components/PluginsLibrary";
import librarySrc from "../components/PluginsLibrary.tsx?raw";
import modalSrc from "../components/PluginInstallModal.tsx?raw";
import { api, type AgentPlugin } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    plugins: vi.fn(),
    pluginInstall: vi.fn(),
    pluginEnable: vi.fn(),
    pluginDisable: vi.fn(),
  },
}));

const mockPlugins = vi.mocked(api.plugins);
const mockEnable = vi.mocked(api.pluginEnable);

const sample: AgentPlugin = {
  id: "portable-test",
  name: "portable.test",
  version: "1.0.0",
  description: "Summarizes reports.",
  path: "/tmp/plugins/portable-test",
  enabled: false,
  namespace: "agent-plugin-portable-test",
  skill_count: 1,
  mcp_count: 1,
};

describe("PluginsLibrary cards", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPlugins.mockResolvedValue({ plugins: [sample] });
  });

  it("renders cards from GET /api/plugins", async () => {
    render(<PluginsLibrary />);
    expect(await screen.findByTestId("plugins-library")).toBeTruthy();
    expect(await screen.findByTestId("plugin-card-portable-test")).toBeTruthy();
    expect(screen.getByText("portable.test")).toBeTruthy();
    expect(screen.getByText("v1.0.0")).toBeTruthy();
    expect(screen.getByText("Summarizes reports.")).toBeTruthy();
    expect(screen.getByText(/disabled · 1 skill · 1 MCP/)).toBeTruthy();
    expect(mockPlugins).toHaveBeenCalled();
  });

  it("shows empty state when no plugins are installed", async () => {
    mockPlugins.mockResolvedValue({ plugins: [] });
    render(<PluginsLibrary />);
    expect(await screen.findByTestId("plugins-library-empty")).toBeTruthy();
    expect(screen.getByText("No plugins installed.")).toBeTruthy();
    expect(screen.queryByTestId("plugins-library-cards")).toBeNull();
  });

  it("toggles enable from a card", async () => {
    mockEnable.mockResolvedValue({ ok: true, plugin: { ...sample, enabled: true } });
    mockPlugins
      .mockResolvedValueOnce({ plugins: [sample] })
      .mockResolvedValue({ plugins: [{ ...sample, enabled: true }] });
    render(<PluginsLibrary />);
    fireEvent.click(await screen.findByTestId("plugin-toggle-portable-test"));
    await waitFor(() => expect(mockEnable).toHaveBeenCalledWith("portable-test"));
  });

  it("opens the install modal from the library", async () => {
    render(<PluginsLibrary />);
    fireEvent.click(await screen.findByTestId("plugins-install-open"));
    expect(await screen.findByTestId("plugin-install-modal")).toBeTruthy();
  });

  it("stays agent-plugin only: no marketplace, desktop toggle, or opt-ins", () => {
    const blob = `${librarySrc}\n${modalSrc}`;
    expect(blob).not.toMatch(/marketplace/i);
    expect(blob).not.toMatch(/desktop-plugin|desktop plugin/i);
    expect(blob).not.toMatch(/opt-?ins/i);
    expect(librarySrc).toMatch(/api\.plugins\(/);
    expect(librarySrc).toMatch(/PluginInstallModal/);
  });
});
