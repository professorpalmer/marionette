// @vitest-environment jsdom
import { useEffect } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../App";
import { api, type Config } from "../lib/api";
import { withEndpointDiscovery } from "./endpointFixture";
import { ADD_TERMINAL_SELECTION_EVENT } from "../lib/terminalSelection";
import { clearComposerAttachmentCache } from "../components/conversation/composerAttachmentCache";
import { clearComposerDraftCache, peekComposerDraft } from "../components/conversation/composerDraftCache";
import { clearTranscriptCache, writeTranscriptCache } from "../components/conversation/transcriptCache";

const order: string[] = [];

vi.mock("../components/LeftRail", () => ({
  default: function InitialPilotLeftRail({
    onSessionChange,
    onSessionCreated,
  }: {
    onSessionChange: (id: string | null) => void;
    onSessionCreated: (id: string) => void;
  }) {
    useEffect(() => {
      const create = () => {
        void api.createSession().then(session => {
          writeTranscriptCache(session.id, [], { seededEmpty: true });
          onSessionCreated(session.id);
          onSessionChange(session.id);
        }).catch(() => {
          window.dispatchEvent(new CustomEvent("harness-toast", { detail: "Could not create session -- try again" }));
        });
      };
      window.addEventListener("harness-new-session", create);
      return () => window.removeEventListener("harness-new-session", create);
    }, [onSessionChange, onSessionCreated]);
    return <>
      <button type="button" onClick={() => onSessionChange("session-other")}>Open other session</button>
      <button type="button" onClick={() => onSessionChange("session-new")}>Return to new session</button>
    </>;
  },
}));

vi.mock("../components/RightPane", () => ({ default: () => null }));
vi.mock("../components/RightDock", () => ({ default: () => null }));
vi.mock("../components/StatusBar", () => ({ default: () => null }));
vi.mock("../components/UpdateBanner", () => ({ default: () => null }));
vi.mock("../components/ComputerAccess", () => ({ default: () => null }));
vi.mock("../components/ProviderKeyBanner", () => ({ default: () => null }));
vi.mock("../components/KeyBootstrapBanner", () => ({ default: () => null }));
vi.mock("../components/RegistryWizard", () => ({ default: () => null }));
vi.mock("../components/OnboardingOverlay", () => ({ default: () => null }));
vi.mock("../components/CommandPalette", () => ({ default: () => null }));
vi.mock("../components/Resizer", () => ({ default: () => null }));
vi.mock("../components/SwarmReasoningPicker", () => ({ default: () => null }));
vi.mock("../components/conversation/WorkspaceChip", () => ({ default: () => null }));
vi.mock("../components/SettingsShell", () => ({ focusSettingsPage: vi.fn() }));

const config: Config = {
  driver: "anthropic:claude-sonnet-4-6",
  models: ["anthropic:claude-sonnet-4-6", "local:mlx-community/Bonsai2-27B"],
  reach: "cloud",
  budget: 1,
};

beforeEach(() => {
  order.length = 0;
  localStorage.clear();
  clearComposerAttachmentCache();
  clearComposerDraftCache();
  clearTranscriptCache();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.stubGlobal("fetch", withEndpointDiscovery(async input => {
    const path = String(input).split("?")[0];
    const payload = path === "/api/session/queue"
      ? { ok: true, session_id: "session-new", items: [], held_items: [], receipts: [], recovery: [] }
      : path === "/api/sessions/transcript"
        ? { history: [], display: [] }
        : path === "/api/session/state"
          ? { state: "idle", runners: [], pending_swarms: false }
          : path === "/api/commands"
            ? { commands: [] }
            : path === "/api/swarm/live"
              ? []
              : path === "/api/workspace/files"
                ? { files: [], folders: [] }
                : {};
    return Response.json(payload);
  }));
  vi.spyOn(api, "config").mockImplementation(async sessionId => ({
    ...config,
    session_id: sessionId || undefined,
    driver: sessionId ? "local:mlx-community/Bonsai2-27B" : config.driver,
  }));
  vi.spyOn(api, "diagnostics").mockResolvedValue({});
  vi.spyOn(api, "providers").mockResolvedValue([{ has_key: true }]);
  vi.spyOn(api, "createSession").mockImplementation(async () => {
    order.push("create");
    return { id: "session-new", title: "New session", created: 1 };
  });
  vi.spyOn(api, "swapPilot").mockImplementation(async (_model, sessionId) => {
    order.push(`bind:${sessionId}`);
    return { ok: true };
  });
  vi.spyOn(api, "chat").mockImplementation((_message, _event, _done, _error, _auto, _images, submission) => {
    order.push(`chat:${submission?.session_id || "none"}`);
    return () => {};
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("creates and binds the selected pilot before the first send without a manual New session click", async () => {
  await act(async () => { render(<App />); });
  const picker = await screen.findByRole("button", { name: "claude-sonnet-4-6" });
  fireEvent.click(picker);
  fireEvent.click(screen.getByText(/Bonsai2-27B/i));

  const input = screen.getByPlaceholderText("Message the pilot...");
  fireEvent.change(input, { target: { value: "use bonsai" } });
  fireEvent.click(screen.getByRole("button", { name: "Send", exact: true }));

  await waitFor(() => expect(api.chat).toHaveBeenCalledTimes(1));
  expect(order).toEqual(["create", "bind:session-new", "chat:session-new"]);
  expect(vi.mocked(api.chat).mock.calls[0][0]).toBe("use bonsai");
  expect(input).toHaveValue("use bonsai");
});

it("shows the acknowledged pilot while session config is pending, then accepts a fresh external change", async () => {
  let finishBinding: ((result: { ok: boolean }) => void) | undefined;
  const sessionConfigResolvers: Array<(value: Config) => void> = [];
  vi.mocked(api.config).mockImplementation(sessionId => {
    if (!sessionId) {
      return Promise.resolve({ ...config, driver: "openrouter:deepseek-v4-flash" });
    }
    return new Promise(resolve => sessionConfigResolvers.push(resolve));
  });
  vi.mocked(api.swapPilot).mockImplementationOnce(() => new Promise(resolve => {
    finishBinding = resolve;
  }));

  await act(async () => { render(<App />); });
  fireEvent.click(await screen.findByRole("button", { name: "deepseek-v4-flash" }));
  fireEvent.click(screen.getByText(/Bonsai2-27B/i));
  const input = screen.getByPlaceholderText("Message the pilot...");
  fireEvent.change(input, { target: { value: "use acknowledged bonsai" } });
  fireEvent.click(screen.getByRole("button", { name: "Send", exact: true }));
  await waitFor(() => expect(api.swapPilot).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(sessionConfigResolvers.length).toBeGreaterThanOrEqual(1));

  await act(async () => {
    finishBinding?.({ ok: true });
    await Promise.resolve();
  });
  await waitFor(() => expect(api.chat).toHaveBeenCalledTimes(1));
  await waitFor(() => {
    expect(screen.getAllByText(/Waiting on Bonsai2-27B/i)).toHaveLength(2);
  });
  expect(screen.queryByText(/Waiting on deepseek-v4-flash/i)).toBeNull();

  await act(async () => {
    sessionConfigResolvers[0]?.({
      ...config,
      session_id: "session-new",
      driver: "openrouter:deepseek-v4-flash",
    });
    await Promise.resolve();
  });
  expect(screen.getAllByText(/Waiting on Bonsai2-27B/i)).toHaveLength(2);

  await waitFor(() => expect(sessionConfigResolvers.length).toBeGreaterThanOrEqual(2));
  const newestConfig = sessionConfigResolvers[sessionConfigResolvers.length - 1];
  await act(async () => {
    newestConfig?.({
      ...config,
      session_id: "session-new",
      driver: "openrouter:google/gemini-3.7-flash",
    });
    await Promise.resolve();
  });
  await waitFor(() => {
    expect(screen.getAllByText(/Waiting on gemini-3.7-flash/i)).toHaveLength(2);
  });
});

it("does not publish an acknowledged pilot into a different active session", async () => {
  let finishBinding: ((result: { ok: boolean }) => void) | undefined;
  vi.mocked(api.config).mockImplementation(async sessionId => ({
    ...config,
    session_id: sessionId || undefined,
    driver: sessionId === "session-new"
      ? "local:mlx-community/Bonsai2-27B"
      : "openrouter:deepseek-v4-flash",
  }));
  vi.mocked(api.swapPilot).mockImplementationOnce(() => new Promise(resolve => {
    finishBinding = resolve;
  }));

  await act(async () => { render(<App />); });
  fireEvent.click(await screen.findByRole("button", { name: "deepseek-v4-flash" }));
  fireEvent.click(screen.getByText(/Bonsai2-27B/i));
  const input = screen.getByPlaceholderText("Message the pilot...");
  fireEvent.change(input, { target: { value: "stay with this session" } });
  fireEvent.click(screen.getByRole("button", { name: "Send", exact: true }));
  await waitFor(() => expect(api.swapPilot).toHaveBeenCalledTimes(1));

  fireEvent.click(screen.getByRole("button", { name: "Open other session" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "deepseek-v4-flash" })).toBeEnabled());
  await act(async () => {
    finishBinding?.({ ok: true });
    await Promise.resolve();
  });

  expect(screen.getByRole("button", { name: "deepseek-v4-flash" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "Bonsai2-27B" })).toBeNull();
  expect(api.chat).not.toHaveBeenCalled();
});

it("keeps the draft and does not dispatch when creation fails, then retries the same selection", async () => {
  vi.mocked(api.createSession).mockRejectedValueOnce(new Error("offline"));
  await act(async () => { render(<App />); });
  fireEvent.click(await screen.findByRole("button", { name: "claude-sonnet-4-6" }));
  fireEvent.click(screen.getByText(/Bonsai2-27B/i));
  const input = screen.getByPlaceholderText("Message the pilot...");
  fireEvent.change(input, { target: { value: "keep this exact draft" } });

  fireEvent.click(screen.getByRole("button", { name: "Send", exact: true }));
  await act(async () => { await Promise.resolve(); });
  expect(api.chat).not.toHaveBeenCalled();
  expect(api.swapPilot).not.toHaveBeenCalled();
  expect(input).toHaveValue("keep this exact draft");

  fireEvent.click(screen.getByRole("button", { name: "Send", exact: true }));
  await waitFor(() => expect(api.chat).toHaveBeenCalledTimes(1));
  expect(order).toEqual(["create", "bind:session-new", "chat:session-new"]);
});

it("keeps the draft and blocks dispatch when the selected pilot cannot bind", async () => {
  vi.mocked(api.swapPilot).mockRejectedValueOnce(new Error("provider unavailable"));
  await act(async () => { render(<App />); });
  fireEvent.click(await screen.findByRole("button", { name: "claude-sonnet-4-6" }));
  fireEvent.click(screen.getByText(/Bonsai2-27B/i));
  const input = screen.getByPlaceholderText("Message the pilot...");
  fireEvent.change(input, { target: { value: "do not lose this" } });

  fireEvent.click(screen.getByRole("button", { name: "Send", exact: true }));
  await screen.findByText(/Could not set Bonsai2-27B/);

  expect(api.chat).not.toHaveBeenCalled();
  expect(input).toHaveValue("do not lose this");
  expect(screen.getByRole("button", { name: "Send", exact: true })).toBeEnabled();
});

it("does not submit text edited after Send while the selected pilot is binding", async () => {
  let finishBinding: ((result: { ok: boolean }) => void) | undefined;
  vi.mocked(api.swapPilot).mockImplementationOnce(() => new Promise(resolve => {
    finishBinding = resolve;
  }));
  await act(async () => { render(<App />); });
  fireEvent.click(await screen.findByRole("button", { name: "claude-sonnet-4-6" }));
  fireEvent.click(screen.getByText(/Bonsai2-27B/i));
  const input = screen.getByPlaceholderText("Message the pilot...");
  fireEvent.change(input, { target: { value: "original requested message" } });
  fireEvent.click(screen.getByRole("button", { name: "Send", exact: true }));
  await waitFor(() => expect(api.swapPilot).toHaveBeenCalledTimes(1));
  expect(api.chat).not.toHaveBeenCalled();
  expect(input).toBeEnabled();
  fireEvent.change(input, { target: { value: "edited after send" } });
  await act(async () => {
    finishBinding?.({ ok: true });
    await new Promise(resolve => setTimeout(resolve, 25));
  });
  await waitFor(() => expect(screen.getByRole("button", { name: "Bonsai2-27B" })).toBeEnabled());
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 25)); });
  expect(api.chat).not.toHaveBeenCalled();
  expect(input).toHaveValue("edited after send");

  fireEvent.click(screen.getByRole("button", { name: "Send", exact: true }));
  await waitFor(() => expect(api.chat).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.chat).mock.calls[0][0]).toBe("edited after send");
  expect(vi.mocked(api.chat).mock.calls[0][6]?.session_id).toBe("session-new");
});

it("cancels the deferred send after navigating away and preserves the new-session draft on return", async () => {
  let finishBinding: ((result: { ok: boolean }) => void) | undefined;
  vi.mocked(api.swapPilot).mockImplementationOnce(() => new Promise(resolve => {
    finishBinding = resolve;
  }));
  await act(async () => { render(<App />); });
  fireEvent.click(await screen.findByRole("button", { name: "claude-sonnet-4-6" }));
  fireEvent.click(screen.getByText(/Bonsai2-27B/i));
  const input = screen.getByPlaceholderText("Message the pilot...");
  fireEvent.change(input, { target: { value: "draft owned by the new session" } });
  fireEvent.click(screen.getByRole("button", { name: "Send", exact: true }));
  await waitFor(() => expect(api.swapPilot).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(document.querySelector('[data-session-pane="session-new"]')).toBeTruthy());
  await waitFor(() => expect(input).toHaveValue("draft owned by the new session"));

  fireEvent.click(screen.getByRole("button", { name: "Open other session" }));
  await waitFor(() => expect(input).toHaveValue(""));
  expect(peekComposerDraft("session-new")).toBe("draft owned by the new session");
  await act(async () => {
    finishBinding?.({ ok: true });
    await new Promise(resolve => setTimeout(resolve, 25));
  });
  expect(api.chat).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Return to new session" }));
  await waitFor(() => expect(input).toHaveValue("draft owned by the new session"));
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 25)); });
  expect(api.chat).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: "Send", exact: true }));
  await waitFor(() => expect(api.chat).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.chat).mock.calls[0][0]).toBe("draft owned by the new session");
  expect(vi.mocked(api.chat).mock.calls[0][6]?.session_id).toBe("session-new");
});

it("cancels the deferred send when an image is attached while the selected pilot is binding", async () => {
  let finishBinding: ((result: { ok: boolean }) => void) | undefined;
  vi.mocked(api.swapPilot).mockImplementationOnce(() => new Promise(resolve => {
    finishBinding = resolve;
  }));
  vi.spyOn(api, "uploadImage").mockResolvedValue({ path: "/uploads/after-send.png", name: "after-send.png" });
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:after-send.png");
  await act(async () => { render(<App />); });
  fireEvent.click(await screen.findByRole("button", { name: "claude-sonnet-4-6" }));
  fireEvent.click(screen.getByText(/Bonsai2-27B/i));
  const input = screen.getByPlaceholderText("Message the pilot...");
  fireEvent.change(input, { target: { value: "send after checking the attachment" } });
  fireEvent.click(screen.getByRole("button", { name: "Send", exact: true }));
  await waitFor(() => expect(api.swapPilot).toHaveBeenCalledTimes(1));

  fireEvent.paste(input, {
    clipboardData: {
      items: [{
        type: "image/png",
        getAsFile: () => new File(["image"], "after-send.png", { type: "image/png" }),
      }],
    },
  });
  await screen.findByTitle("Remove image");
  await act(async () => {
    finishBinding?.({ ok: true });
    await new Promise(resolve => setTimeout(resolve, 25));
  });
  expect(api.chat).not.toHaveBeenCalled();
  expect(screen.getByTitle("Remove image")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Send", exact: true }));
  await waitFor(() => expect(api.chat).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.chat).mock.calls[0][0]).toBe("send after checking the attachment");
  expect(vi.mocked(api.chat).mock.calls[0][6]?.session_id).toBe("session-new");
});

it("does not auto-send terminal text inserted after Send while the selected pilot is binding", async () => {
  let finishBinding: ((result: { ok: boolean }) => void) | undefined;
  vi.mocked(api.swapPilot).mockImplementationOnce(() => new Promise(resolve => {
    finishBinding = resolve;
  }));
  await act(async () => { render(<App />); });
  fireEvent.click(await screen.findByRole("button", { name: "claude-sonnet-4-6" }));
  fireEvent.click(screen.getByText(/Bonsai2-27B/i));
  const input = screen.getByPlaceholderText("Message the pilot...");
  fireEvent.change(input, { target: { value: "review this" } });
  fireEvent.click(screen.getByRole("button", { name: "Send", exact: true }));
  await waitFor(() => expect(api.swapPilot).toHaveBeenCalledTimes(1));
  act(() => {
    window.dispatchEvent(new CustomEvent(ADD_TERMINAL_SELECTION_EVENT, {
      detail: { text: "later terminal output", label: "zsh:42" },
    }));
  });
  await waitFor(() => expect(input).toHaveValue("review this @terminal:zsh:42 "));
  await act(async () => {
    finishBinding?.({ ok: true });
    await new Promise(resolve => setTimeout(resolve, 25));
  });
  expect(api.chat).not.toHaveBeenCalled();
  expect(input).toHaveValue("review this @terminal:zsh:42 ");
  fireEvent.click(screen.getByRole("button", { name: "Send", exact: true }));
  await waitFor(() => expect(api.chat).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.chat).mock.calls[0][0]).toContain("later terminal output");
  expect(vi.mocked(api.chat).mock.calls[0][6]?.session_id).toBe("session-new");
});
