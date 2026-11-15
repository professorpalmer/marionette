import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PluginsPane from "../components/PluginsPane";
import paneSrc from "../components/PluginsPane.tsx?raw";
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
const mockInstall = vi.mocked(api.pluginInstall);
const mockEnable = vi.mocked(api.pluginEnable);
const mockDisable = vi.mocked(api.pluginDisable);

const sample: AgentPlugin = {
  id: "portable-test",
  name: "portable.test",
  version: "1.0.0",
  description: "Summarizes reports.",
  path: "/tmp/plugins/portable-test",
  enabled: false,
  namespace: "agent-plugin-portable-test",
  skill_count: 1,
  mcp_count: 2,
};

function openInstall(source: string) {
  fireEvent.click(screen.getByTestId("plugins-install-open"));
  fireEvent.change(screen.getByTestId("plugin-install-source"), { target: { value: source } });
}

describe("Plugins page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPlugins.mockResolvedValue({ plugins: [sample] });
  });

  it("shows one row per plugin from GET /api/plugins", async () => {
    render(<PluginsPane />);
    const row = await screen.findByTestId("plugin-row-portable-test");
    expect(screen.getByRole("heading", { name: "Plugins" })).toBeInTheDocument();
    expect(within(row).getByText("portable.test")).toBeInTheDocument();
    expect(within(row).getByText("v1.0.0")).toBeInTheDocument();
    expect(within(row).getByText("Summarizes reports.")).toBeInTheDocument();
    expect(within(row).getByText("Off · 1 skill, 2 MCP servers")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Turn on" })).toBeInTheDocument();
    expect(mockPlugins).toHaveBeenCalled();
  });

  it("shows the plugin error as its state", async () => {
    mockPlugins.mockResolvedValue({ plugins: [{ ...sample, error: "manifest missing" }] });
    render(<PluginsPane />);
    expect(await screen.findByText("Error: manifest missing")).toBeInTheDocument();
  });

  it("says so when no plugin is installed", async () => {
    mockPlugins.mockResolvedValue({ plugins: [] });
    render(<PluginsPane />);
    expect(await screen.findByTestId("plugins-empty")).toHaveTextContent("No plugins installed.");
  });

  it("turns a plugin on through the enable API", async () => {
    mockEnable.mockResolvedValue({ ok: true, plugin: { ...sample, enabled: true } });
    mockPlugins
      .mockResolvedValueOnce({ plugins: [sample] })
      .mockResolvedValue({ plugins: [{ ...sample, enabled: true }] });
    render(<PluginsPane />);
    fireEvent.click(await screen.findByRole("button", { name: "Turn on" }));
    await waitFor(() => expect(mockEnable).toHaveBeenCalledWith("portable-test"));
    expect(await screen.findByText("Turned on portable.test.")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Turn off" })).toBeInTheDocument();
  });

  it("turns a plugin off through the disable API", async () => {
    mockPlugins.mockResolvedValue({ plugins: [{ ...sample, enabled: true }] });
    mockDisable.mockResolvedValue({ ok: true });
    render(<PluginsPane />);
    fireEvent.click(await screen.findByRole("button", { name: "Turn off" }));
    await waitFor(() => expect(mockDisable).toHaveBeenCalledWith("portable-test"));
  });

  it("shows the API error when a toggle fails", async () => {
    mockEnable.mockResolvedValue({ ok: false, error: "plugin manifest is invalid" });
    render(<PluginsPane />);
    fireEvent.click(await screen.findByRole("button", { name: "Turn on" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("plugin manifest is invalid");
  });

  it("refuses a relative source before it calls install", async () => {
    render(<PluginsPane />);
    await screen.findByTestId("plugin-row-portable-test");
    openInstall("relative-plugin");
    fireEvent.click(screen.getByTestId("plugin-install-submit"));
    expect(await screen.findByRole("alert")).toHaveTextContent(/absolute path|git URL|https URL|GitHub/);
    expect(mockInstall).not.toHaveBeenCalled();
  });

  it("installs with force, closes the form and refreshes the list", async () => {
    mockInstall.mockResolvedValue({ ok: true, plugin: sample });
    render(<PluginsPane />);
    await screen.findByTestId("plugin-row-portable-test");
    openInstall("https://github.com/acme/widget");
    fireEvent.click(screen.getByTestId("plugin-install-force"));
    const reads = mockPlugins.mock.calls.length;
    await act(async () => { fireEvent.click(screen.getByTestId("plugin-install-submit")); });
    expect(mockInstall).toHaveBeenCalledWith("https://github.com/acme/widget", { force: true });
    expect(screen.getByText("Installed portable.test. It stays off until you turn it on.")).toBeInTheDocument();
    expect(screen.queryByTestId("plugins-install-form")).toBeNull();
    expect(mockPlugins.mock.calls.length).toBeGreaterThan(reads);
  });

  it("keeps the form open and shows the install error", async () => {
    mockInstall.mockResolvedValue({ ok: false, error: "plugin already installed: portable-test" });
    render(<PluginsPane />);
    await screen.findByTestId("plugin-row-portable-test");
    openInstall("/abs/plugin");
    await act(async () => { fireEvent.click(screen.getByTestId("plugin-install-submit")); });
    expect(mockInstall).toHaveBeenCalledWith("/abs/plugin", { force: false });
    expect(screen.getByRole("alert")).toHaveTextContent("plugin already installed: portable-test");
    expect(screen.getByTestId("plugins-install-form")).toBeInTheDocument();
  });

  it("stays agent-plugin only: no marketplace, desktop toggle, or opt-ins", () => {
    expect(paneSrc).not.toMatch(/marketplace/i);
    expect(paneSrc).not.toMatch(/desktop-plugin|desktop plugin/i);
    expect(paneSrc).not.toMatch(/opt-?ins/i);
    expect(paneSrc).toMatch(/api\.plugins\(/);
  });
});
