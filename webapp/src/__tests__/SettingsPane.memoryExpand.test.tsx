import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SettingsPane, {
  clearSettingsSnapshot,
  writeSettingsSnapshot,
} from "../components/SettingsPane";
import { api, type Settings } from "../lib/api";
import { expandAgentMemory, takePendingExpandMemory } from "../lib/memoryDeepLink";
import { runCommandPaletteAction } from "../lib/commandPalette";
import { classifyLocalSlashCommand, localSlashPaletteAction } from "../components/conversation/composerSend";
import { isBuiltInSlashCommand } from "../components/conversation/slashCommands";

vi.mock("../lib/api", () => ({
  api: {
    settings: vi.fn(),
    updateSettings: vi.fn(),
    getUsage: vi.fn().mockResolvedValue(null),
    getWikiConfig: vi.fn().mockResolvedValue({ api_base: "", has_token: false }),
    getHooks: vi.fn().mockResolvedValue({ hooks: [], events: [] }),
    providers: vi.fn().mockResolvedValue([]),
    authPools: vi.fn().mockResolvedValue({ pools: [] }),
    bedrockStatus: vi.fn().mockResolvedValue(null),
    cursorCliStatus: vi.fn().mockResolvedValue(null),
    gitStatus: vi.fn().mockResolvedValue(null),
    platformAdapters: vi.fn().mockResolvedValue([]),
  },
}));

vi.mock("../components/SkillsPane", () => ({ default: () => <div /> }));
vi.mock("../components/MemoryPane", () => ({
  default: () => <div data-testid="memory-pane" />,
}));
vi.mock("../components/SchedulesPane", () => ({ default: () => <div /> }));

const mockSettings = vi.mocked(api.settings);

const sampleSettings: Settings = {
  driver: "cursor",
  reach: "repo",
  budget: 100,
  models: ["anthropic/claude-sonnet"],
  auto_distill: false,
  state_dir: "/tmp/state",
  repo: "/tmp/repo",
  has_api_key: true,
  api_key_masked: "sk-…abcd",
};

describe("SettingsPane Agent Memory deep-link", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearSettingsSnapshot();
    // Drain any leftover latch from a prior test.
    takePendingExpandMemory();
    writeSettingsSnapshot(sampleSettings);
    mockSettings.mockResolvedValue(sampleSettings);
  });

  afterEach(() => {
    clearSettingsSnapshot();
    takePendingExpandMemory();
  });

  it("keeps Agent Memory collapsed by default on Advanced", async () => {
    render(<SettingsPane onOpenWizard={vi.fn()} section="advanced" />);
    expect(await screen.findByText("Agent Memory")).toBeInTheDocument();
    expect(screen.queryByTestId("memory-pane")).toBeNull();
  });

  it("expands Agent Memory when harness-expand-memory fires", async () => {
    render(<SettingsPane onOpenWizard={vi.fn()} section="advanced" />);
    expect(await screen.findByText("Agent Memory")).toBeInTheDocument();
    expect(screen.queryByTestId("memory-pane")).toBeNull();

    act(() => {
      window.dispatchEvent(new Event("harness-expand-memory"));
    });
    await waitFor(() => {
      expect(screen.getByTestId("memory-pane")).toBeInTheDocument();
    });
  });

  it("Cmd-K open-memory expands Agent Memory (not just Advanced)", async () => {
    const focusSettingsPage = vi.fn();

    // Latch path: expand before SettingsPane mounts (closed overlay).
    runCommandPaletteAction("open-memory", {
      toggleLeft: () => {},
      toggleRight: () => {},
      focusSettingsPage,
    });
    expect(focusSettingsPage).toHaveBeenCalledWith("advanced");

    render(<SettingsPane onOpenWizard={vi.fn()} section="advanced" />);
    await waitFor(() => {
      expect(screen.getByTestId("memory-pane")).toBeInTheDocument();
    });
  });

  it("/memory slash expands Agent Memory via the same palette path", async () => {
    const action = classifyLocalSlashCommand({
      message: "/memory",
      isBuiltIn: isBuiltInSlashCommand,
      customNames: [],
    });
    expect(action.kind).toBe("memory");
    const paletteId = localSlashPaletteAction(action);
    expect(paletteId).toBe("open-memory");

    runCommandPaletteAction(paletteId!, {
      toggleLeft: () => {},
      toggleRight: () => {},
      focusSettingsPage: () => {},
    });

    render(<SettingsPane onOpenWizard={vi.fn()} section="advanced" />);
    await waitFor(() => {
      expect(screen.getByTestId("memory-pane")).toBeInTheDocument();
    });
  });

  it("expandAgentMemory latches when SettingsPane is not mounted", () => {
    expandAgentMemory();
    // Event had no listener; latch must survive for the next mount.
    const latched = takePendingExpandMemory();
    expect(latched).toBe(true);
    expect(takePendingExpandMemory()).toBe(false);
  });
});
