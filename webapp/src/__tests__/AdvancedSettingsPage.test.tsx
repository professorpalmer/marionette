import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AdvancedSettingsPage from "../components/AdvancedSettingsPage";
import { api } from "../lib/api";
import { expandAgentMemory, expandSkills, takePendingExpandMemory, takePendingExpandSkills } from "../lib/memoryDeepLink";
import { runCommandPaletteAction } from "../lib/commandPalette";
import { classifyLocalSlashCommand, localSlashPaletteAction } from "../components/conversation/composerSend";
import { isBuiltInSlashCommand } from "../components/conversation/slashCommands";

vi.mock("../lib/api", async (importOriginal) => {
  const real = await importOriginal<typeof import("../lib/api")>();
  return {
    ...real,
    api: {
      getHooks: vi.fn(),
      addHook: vi.fn(),
      updateHook: vi.fn(),
      removeHook: vi.fn(),
      getWikiConfig: vi.fn(),
      setWikiConfig: vi.fn(),
      disconnectWiki: vi.fn(),
      archiveStatus: vi.fn(),
      ingestChatArchive: vi.fn(),
      pruneChatArchive: vi.fn(),
    },
  };
});
vi.mock("../components/MemoryPane", () => ({ default: () => <div data-testid="memory-pane" /> }));
vi.mock("../components/SkillsPane", () => ({ default: () => <div data-testid="skills-pane" /> }));
vi.mock("../components/SchedulesPane", () => ({ default: () => <div data-testid="schedules-pane" /> }));

const m = vi.mocked(api);
const hook = { id: "h1", event: "sessionStart", command: ["echo", "start"], enabled: true };

async function mount() {
  await act(async () => { render(<AdvancedSettingsPage />); });
  await screen.findByTestId("advanced-settings");
}

beforeEach(() => {
  vi.clearAllMocks();
  takePendingExpandMemory();
  takePendingExpandSkills();
  delete (window as { harnessIPC?: unknown }).harnessIPC;
  m.getHooks.mockResolvedValue({ hooks: [hook], events: ["sessionStart", "sessionEnd", "preRun", "postRun"] });
  m.getWikiConfig.mockResolvedValue({ api_base: "", has_token: false });
  m.archiveStatus.mockResolvedValue({ ok: true, chats: 3, vault_present: true, backup_dir: "", archive_db: "" });
});

afterEach(() => {
  takePendingExpandMemory();
  takePendingExpandSkills();
  delete (window as { harnessIPC?: unknown }).harnessIPC;
});

describe("Advanced settings page", () => {
  it("renders its groups, and the long lists stay closed", async () => {
    await mount();
    for (const name of ["Memory and skills", "Automation", "Lifecycle hooks", "Wiki", "Chat archive"]) {
      expect(screen.getByRole("heading", { name })).toBeInTheDocument();
    }
    expect(screen.queryByTestId("memory-pane")).toBeNull();
    expect(screen.queryByTestId("skills-pane")).toBeNull();
    expect(screen.queryByTestId("schedules-pane")).toBeNull();
    // App controls exist only in the desktop app.
    expect(screen.queryByTestId("advanced-app")).toBeNull();
    expect(await within(screen.getByTestId("advanced-archive")).findByText("3 chats")).toBeInTheDocument();
  });

  it("opens memory, skills, and schedules from their rows", async () => {
    await mount();
    fireEvent.click(within(screen.getByTestId("advanced-memory")).getByRole("button", { name: "Show" }));
    fireEvent.click(within(screen.getByTestId("advanced-skills")).getByRole("button", { name: "Show" }));
    fireEvent.click(within(screen.getByTestId("advanced-schedules")).getByRole("button", { name: "Show" }));
    expect(screen.getByTestId("memory-pane")).toBeInTheDocument();
    expect(screen.getByTestId("skills-pane")).toBeInTheDocument();
    expect(screen.getByTestId("schedules-pane")).toBeInTheDocument();
  });

  it("opens Agent memory when harness-expand-memory fires on the page", async () => {
    await mount();
    act(() => { expandAgentMemory(); });
    await waitFor(() => expect(screen.getByTestId("memory-pane")).toBeInTheDocument());
    expect(takePendingExpandMemory()).toBe(false);
  });

  it("opens Skills and rules from a latch set before the page mounts", async () => {
    expandSkills();
    await mount();
    expect(screen.getByTestId("skills-pane")).toBeInTheDocument();
  });

  it("Cmd-K open-memory goes to Advanced and opens Agent memory", async () => {
    const focusSettingsPage = vi.fn();
    runCommandPaletteAction("open-memory", { toggleLeft: () => {}, toggleRight: () => {}, focusSettingsPage });
    expect(focusSettingsPage).toHaveBeenCalledWith("advanced");
    await mount();
    expect(screen.getByTestId("memory-pane")).toBeInTheDocument();
  });

  it("/memory takes the same palette path", async () => {
    const action = classifyLocalSlashCommand({ message: "/memory", isBuiltIn: isBuiltInSlashCommand, customNames: [] });
    const paletteId = localSlashPaletteAction(action);
    expect(paletteId).toBe("open-memory");
    runCommandPaletteAction(paletteId!, { toggleLeft: () => {}, toggleRight: () => {}, focusSettingsPage: () => {} });
    await mount();
    expect(screen.getByTestId("memory-pane")).toBeInTheDocument();
  });

  it("lists hooks and turns one off, removes it, and adds one", async () => {
    m.updateHook.mockResolvedValue({ ...hook, enabled: false });
    m.removeHook.mockResolvedValue({ ok: true });
    m.addHook.mockResolvedValue({ id: "h2", event: "postRun", command: ["make", "lint"], enabled: true });
    await mount();
    const hookRow = await screen.findByTestId("hook-row");
    expect(within(hookRow).getByText("echo start")).toBeInTheDocument();

    fireEvent.click(within(hookRow).getByRole("switch", { name: "Run hook sessionStart" }));
    await waitFor(() => expect(m.updateHook).toHaveBeenCalledWith("h1", { enabled: false }));
    await waitFor(() => expect(within(hookRow).getByRole("switch")).toHaveAttribute("aria-checked", "false"));

    fireEvent.click(within(hookRow).getByRole("button", { name: "Remove hook sessionStart" }));
    await waitFor(() => expect(m.removeHook).toHaveBeenCalledWith("h1"));
    await waitFor(() => expect(screen.queryByTestId("hook-row")).toBeNull());

    fireEvent.change(screen.getByRole("combobox", { name: "Hook event" }), { target: { value: "postRun" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Hook command" }), { target: { value: " make lint " } });
    fireEvent.click(screen.getByRole("button", { name: "Add hook" }));
    await waitFor(() => expect(m.addHook).toHaveBeenCalledWith("postRun", "make lint"));
    expect(await screen.findByText("Hook added.")).toBeInTheDocument();
    expect(screen.getByText("make lint")).toBeInTheDocument();
  });

  it("refuses an empty hook command without a call", async () => {
    await mount();
    await screen.findByTestId("hook-row");
    fireEvent.click(screen.getByRole("button", { name: "Add hook" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter a command.");
    expect(m.addHook).not.toHaveBeenCalled();
  });

  it("saves and disconnects the wiki", async () => {
    m.setWikiConfig.mockResolvedValue({ api_base: "https://api.portablellm.wiki/t/me", has_token: true });
    m.disconnectWiki.mockResolvedValue({ api_base: "", has_token: false });
    await mount();
    const wiki = within(screen.getByTestId("advanced-wiki"));
    expect(await wiki.findByText("Not connected")).toBeInTheDocument();
    fireEvent.change(wiki.getByRole("textbox", { name: "Wiki URL" }), { target: { value: "https://api.portablellm.wiki/t/me" } });
    fireEvent.change(wiki.getByLabelText("Owner token"), { target: { value: "fixture-token" } });
    fireEvent.click(wiki.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(m.setWikiConfig).toHaveBeenCalledWith("https://api.portablellm.wiki/t/me", "fixture-token"));
    expect(await wiki.findByText("Token set")).toBeInTheDocument();
    expect(wiki.getByLabelText("Owner token")).toHaveValue("");

    fireEvent.click(wiki.getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(m.disconnectWiki).toHaveBeenCalled());
    expect(await wiki.findByText("Not connected")).toBeInTheDocument();
    expect(wiki.getByRole("textbox", { name: "Wiki URL" })).toHaveValue("");
  });

  it("saves the wiki URL without a token when the token field is empty", async () => {
    m.setWikiConfig.mockResolvedValue({ api_base: "https://x.test", has_token: false });
    await mount();
    const wiki = within(screen.getByTestId("advanced-wiki"));
    fireEvent.change(wiki.getByRole("textbox", { name: "Wiki URL" }), { target: { value: "https://x.test" } });
    fireEvent.click(wiki.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(m.setWikiConfig).toHaveBeenCalledWith("https://x.test", undefined));
  });

  it("moves archived chats into the vault with ingest, then prune", async () => {
    m.ingestChatArchive.mockResolvedValue({ ok: true, ingested: 2, skipped_unchanged: 1, errors: 0, backup_dir: "", archive_db: "" });
    m.pruneChatArchive.mockResolvedValue({ ok: true, pruned: 2, skipped: 1, archive_db: "" });
    await mount();
    m.archiveStatus.mockResolvedValue({ ok: true, chats: 5, vault_present: true, backup_dir: "", archive_db: "" });
    const archive = within(screen.getByTestId("advanced-archive"));
    fireEvent.click(archive.getByRole("button", { name: "Move now" }));
    expect(await archive.findByRole("status")).toHaveTextContent("Saved 2 chats to the vault. Replaced 2 transcripts with a stub.");
    expect(m.ingestChatArchive.mock.invocationCallOrder[0]).toBeLessThan(m.pruneChatArchive.mock.invocationCallOrder[0]);
    expect(await archive.findByText("5 chats")).toBeInTheDocument();
  });

  it("shows the app group in the desktop app and relaunches after the Vite switch", async () => {
    let enabled = false;
    const selfDev = {
      get: vi.fn(async () => ({ enabled, viable: true })),
      set: vi.fn(async (on: boolean) => { enabled = on; return true; }),
    };
    const restart = vi.fn(async () => {});
    (window as { harnessIPC?: unknown }).harnessIPC = { selfDev, restart };
    await mount();
    const app = within(screen.getByTestId("advanced-app"));
    const vite = await app.findByRole("switch", { name: "Serve the UI from Vite" });
    await waitFor(() => expect(vite).toBeEnabled());
    fireEvent.click(vite);
    await waitFor(() => expect(selfDev.set).toHaveBeenCalledWith(true));
    await waitFor(() => expect(restart).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(app.getByRole("switch", { name: "Serve the UI from Vite" })).toHaveAttribute("aria-checked", "true"));

    fireEvent.click(app.getByRole("button", { name: "Relaunch" }));
    await waitFor(() => expect(restart).toHaveBeenCalledTimes(2));
  });

  it("disables the Vite switch when the dev server is not available", async () => {
    (window as { harnessIPC?: unknown }).harnessIPC = {
      selfDev: { get: async () => ({ enabled: false, viable: false }), set: vi.fn() },
    };
    await mount();
    const app = within(screen.getByTestId("advanced-app"));
    expect(await app.findByText(/Vite dev server is not available/)).toBeInTheDocument();
    expect(app.getByRole("switch", { name: "Serve the UI from Vite" })).toBeDisabled();
    expect(app.queryByRole("button", { name: "Relaunch" })).toBeNull();
  });
});
