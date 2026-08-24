import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PluginInstallModal from "../components/PluginInstallModal";
import { api, type AgentPlugin } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    pluginInstall: vi.fn(),
    pluginEnable: vi.fn(),
  },
}));

const mockInstall = vi.mocked(api.pluginInstall);
const mockEnable = vi.mocked(api.pluginEnable);

const installed: AgentPlugin = {
  id: "portable-test",
  name: "portable.test",
  version: "1.0.0",
  description: "Summarizes reports.",
  path: "/tmp/plugins/portable-test",
  enabled: false,
  namespace: "agent-plugin-portable-test",
  skill_count: 1,
  mcp_count: 0,
};

function renderModal(overrides: Partial<ComponentProps<typeof PluginInstallModal>> = {}) {
  const onClose = vi.fn();
  const onInstalled = vi.fn();
  const onEnabled = vi.fn();
  const result = render(
    <PluginInstallModal
      open
      onClose={onClose}
      onInstalled={onInstalled}
      onEnabled={onEnabled}
      {...overrides}
    />,
  );
  return { ...result, onClose, onInstalled, onEnabled };
}

describe("PluginInstallModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("rejects an invalid source before calling install", async () => {
    renderModal();
    fireEvent.change(screen.getByTestId("plugin-install-source"), {
      target: { value: "relative-plugin" },
    });
    fireEvent.click(screen.getByTestId("plugin-install-submit"));
    expect(await screen.findByTestId("plugin-install-error")).toBeTruthy();
    expect(screen.getByTestId("plugin-install-error").textContent).toMatch(
      /absolute path|git URL|https URL|GitHub/,
    );
    expect(mockInstall).not.toHaveBeenCalled();
  });

  it("shows install API errors", async () => {
    mockInstall.mockResolvedValue({ ok: false, error: "plugin already installed: portable-test" });
    renderModal();
    fireEvent.change(screen.getByTestId("plugin-install-source"), {
      target: { value: "/abs/plugin" },
    });
    fireEvent.click(screen.getByTestId("plugin-install-submit"));
    expect(await screen.findByTestId("plugin-install-error")).toHaveTextContent(
      "plugin already installed: portable-test",
    );
    expect(mockInstall).toHaveBeenCalledWith("/abs/plugin", { force: false });
  });

  it("sends force-reinstall and prompts enable after success", async () => {
    mockInstall.mockResolvedValue({ ok: true, plugin: installed });
    mockEnable.mockResolvedValue({ ok: true, plugin: { ...installed, enabled: true } });
    const { onInstalled, onEnabled, onClose } = renderModal();
    fireEvent.change(screen.getByTestId("plugin-install-source"), {
      target: { value: "https://github.com/acme/widget" },
    });
    fireEvent.click(screen.getByTestId("plugin-install-force"));
    fireEvent.click(screen.getByTestId("plugin-install-submit"));
    expect(await screen.findByTestId("plugin-install-enable-prompt")).toBeTruthy();
    expect(screen.getByText(/Installed portable.test/)).toBeTruthy();
    expect(mockInstall).toHaveBeenCalledWith("https://github.com/acme/widget", { force: true });
    expect(onInstalled).toHaveBeenCalledWith(installed);

    fireEvent.click(screen.getByTestId("plugin-install-enable"));
    await waitFor(() => expect(mockEnable).toHaveBeenCalledWith("portable-test"));
    expect(onEnabled).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });
});
