import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../App";
import { api } from "../lib/api";

type LeftRailCallbacks = {
  change: (id: string | null) => void;
  created: (id: string) => void;
};

const rail = vi.hoisted<LeftRailCallbacks>(() => ({
  change: () => {},
  created: () => {},
}));

vi.mock("../lib/api", () => ({ api: {
  config: (sessionId?: string | null) => Promise.resolve({
    session_id: sessionId || undefined,
    driver: "anthropic:claude-sonnet-4-6",
    models: ["anthropic:claude-sonnet-4-6", "local:mlx-community/Bonsai2-27B"],
    reach: "cloud",
    budget: 1,
  }),
  diagnostics: () => Promise.resolve({}),
  providers: () => Promise.resolve([{ has_key: true }]),
  swapPilot: vi.fn(),
} }));

vi.mock("../components/LeftRail", () => ({
  default: ({
    onSessionChange,
    onSessionCreated,
  }: {
    onSessionChange: (id: string | null) => void;
    onSessionCreated: (id: string) => void;
  }) => {
    rail.change = onSessionChange;
    rail.created = onSessionCreated;
    return <div />;
  },
}));

vi.mock("../components/Conversation", () => ({
  default: ({
    activeSessionId,
    pendingPilotModel,
    pilotSetupNotice,
    pilotSelectionDisabled,
    onPendingPilotModelChange,
    onSessionPilotModelChange,
  }: {
    activeSessionId: string | null;
    pendingPilotModel?: string;
    pilotSetupNotice?: string;
    pilotSelectionDisabled?: boolean;
    onPendingPilotModelChange: (model: string) => void;
    onSessionPilotModelChange: (sessionId: string, model: string) => Promise<unknown>;
  }) => <div>
    <button onClick={() => onPendingPilotModelChange("local:mlx-community/Bonsai2-27B")}>Choose Bonsai</button>
    <button onClick={() => onPendingPilotModelChange("anthropic:claude-sonnet-4-6")}>Choose Claude</button>
    <button onClick={() => { if (activeSessionId) void onSessionPilotModelChange(activeSessionId, "local:mlx-community/Bonsai2-27B"); }}>Retry succeeded</button>
    <button onClick={() => { if (activeSessionId) void onSessionPilotModelChange(activeSessionId, "local:mlx-community/Bonsai2-27B"); }}>Swap Bonsai</button>
    <button onClick={() => { if (activeSessionId) void onSessionPilotModelChange(activeSessionId, "anthropic:claude-sonnet-4-6"); }}>Swap Claude</button>
    <output data-testid="pending-model">{pendingPilotModel}</output>
    <output data-testid="setup-notice">{pilotSetupNotice}</output>
    <output data-testid="selection-disabled">{String(Boolean(pilotSelectionDisabled))}</output>
    <output data-testid="active-session">{activeSessionId}</output>
  </div>,
}));

vi.mock("../components/RightPane", () => ({ default: () => null }));
vi.mock("../components/RightDock", () => ({ default: () => null }));
vi.mock("../components/StatusBar", () => ({ default: () => null }));
vi.mock("../components/UpdateBanner", () => ({ default: () => null }));
vi.mock("../components/ProviderKeyBanner", () => ({ default: () => null }));
vi.mock("../components/KeyBootstrapBanner", () => ({ default: () => null }));
vi.mock("../components/RegistryWizard", () => ({ default: () => null }));
vi.mock("../components/OnboardingOverlay", () => ({ default: () => null }));
vi.mock("../components/CommandPalette", () => ({ default: () => null }));
vi.mock("../components/SettingsShell", () => ({ focusSettingsPage: vi.fn() }));

function deferred<T>() {
  let resolve: (value: T) => void = () => {};
  let reject: (reason: unknown) => void = () => {};
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(api.swapPilot).mockReset();
});

afterEach(cleanup);

it("binds the latest empty-session model choice exactly once to the created session", async () => {
  const swap = deferred<{ ok: boolean }>();
  vi.mocked(api.swapPilot).mockReturnValue(swap.promise);
  await act(async () => { render(<App />); });

  fireEvent.click(screen.getByRole("button", { name: "Choose Bonsai" }));
  fireEvent.click(screen.getByRole("button", { name: "Choose Claude" }));
  fireEvent.click(screen.getByRole("button", { name: "Choose Bonsai" }));
  expect(screen.getByTestId("pending-model")).toHaveTextContent("local:mlx-community/Bonsai2-27B");

  act(() => {
    rail.created("session-new");
    rail.change("session-new");
  });
  await waitFor(() => expect(api.swapPilot).toHaveBeenCalledExactlyOnceWith("local:mlx-community/Bonsai2-27B", "session-new"));
  expect(screen.getByTestId("setup-notice")).toHaveTextContent("Setting Bonsai2-27B for this session");
  expect(screen.getByTestId("selection-disabled")).toHaveTextContent("true");

  await act(async () => swap.resolve({ ok: true }));
  await waitFor(() => expect(screen.getByTestId("setup-notice")).toBeEmptyDOMElement());
  expect(screen.getByTestId("selection-disabled")).toHaveTextContent("false");
});

it("keeps a failed bind blocked until an explicit session-scoped retry succeeds", async () => {
  vi.mocked(api.swapPilot).mockRejectedValueOnce(new Error("provider unavailable"));
  await act(async () => { render(<App />); });
  fireEvent.click(screen.getByRole("button", { name: "Choose Bonsai" }));

  await act(async () => {
    rail.created("session-new");
    rail.change("session-new");
    await Promise.resolve();
  });
  expect(screen.getByTestId("setup-notice")).toHaveTextContent("Could not set Bonsai2-27B");

  act(() => rail.change("session-other"));
  expect(screen.getByTestId("setup-notice")).toBeEmptyDOMElement();
  act(() => rail.change("session-new"));
  expect(screen.getByTestId("setup-notice")).toHaveTextContent("Could not set Bonsai2-27B");

  fireEvent.click(screen.getByRole("button", { name: "Retry succeeded" }));
  await waitFor(() => expect(screen.getByTestId("setup-notice")).toBeEmptyDOMElement());
});

it("does not apply late setup state to a different active session", async () => {
  const swap = deferred<{ ok: boolean }>();
  vi.mocked(api.swapPilot).mockReturnValue(swap.promise);
  await act(async () => { render(<App />); });
  fireEvent.click(screen.getByRole("button", { name: "Choose Bonsai" }));
  act(() => {
    rail.created("session-new");
    rail.change("session-new");
    rail.change("session-other");
  });

  expect(screen.getByTestId("active-session")).toHaveTextContent("session-other");
  expect(screen.getByTestId("setup-notice")).toBeEmptyDOMElement();
  await act(async () => swap.resolve({ ok: true }));
  expect(screen.getByTestId("active-session")).toHaveTextContent("session-other");
  expect(api.swapPilot).toHaveBeenCalledExactlyOnceWith("local:mlx-community/Bonsai2-27B", "session-new");
});

it("cancels unbound intent when the user switches to an existing session", async () => {
  vi.mocked(api.swapPilot).mockResolvedValue({ ok: true });
  await act(async () => { render(<App />); });
  fireEvent.click(screen.getByRole("button", { name: "Choose Bonsai" }));
  act(() => rail.change("session-existing"));
  act(() => rail.created("session-late"));

  expect(screen.getByTestId("active-session")).toHaveTextContent("session-existing");
  expect(screen.getByTestId("pending-model")).toBeEmptyDOMElement();
  expect(api.swapPilot).not.toHaveBeenCalled();
});

it("retains the model intent until a failed creation is retried successfully", async () => {
  vi.mocked(api.swapPilot).mockResolvedValue({ ok: true });
  await act(async () => { render(<App />); });
  fireEvent.click(screen.getByRole("button", { name: "Choose Bonsai" }));
  expect(screen.getByTestId("pending-model")).toHaveTextContent("local:mlx-community/Bonsai2-27B");
  expect(api.swapPilot).not.toHaveBeenCalled();
  act(() => {
    rail.created("session-retry");
    rail.change("session-retry");
  });
  await waitFor(() => expect(api.swapPilot).toHaveBeenCalledExactlyOnceWith("local:mlx-community/Bonsai2-27B", "session-retry"));
});

it("keeps each session bind gate when another empty view selects a model", async () => {
  const firstSwap = deferred<{ ok: boolean }>();
  vi.mocked(api.swapPilot).mockReturnValue(firstSwap.promise);
  await act(async () => { render(<App />); });
  fireEvent.click(screen.getByRole("button", { name: "Choose Bonsai" }));
  act(() => {
    rail.created("session-a");
    rail.change("session-a");
    rail.change(null);
  });

  fireEvent.click(screen.getByRole("button", { name: "Choose Claude" }));
  act(() => rail.change("session-a"));

  expect(screen.getByTestId("setup-notice")).toHaveTextContent("Setting Bonsai2-27B for this session");
  expect(screen.getByTestId("selection-disabled")).toHaveTextContent("true");
});

it("serializes existing-session selections so an older backend request cannot finish last", async () => {
  const first = deferred<{ ok: boolean }>();
  const second = deferred<{ ok: boolean }>();
  vi.mocked(api.swapPilot)
    .mockReturnValueOnce(first.promise)
    .mockReturnValueOnce(second.promise);
  await act(async () => { render(<App />); });
  act(() => rail.change("session-a"));

  fireEvent.click(screen.getByRole("button", { name: "Swap Bonsai" }));
  fireEvent.click(screen.getByRole("button", { name: "Swap Claude" }));
  await waitFor(() => expect(api.swapPilot).toHaveBeenCalledTimes(1));
  expect(api.swapPilot).toHaveBeenNthCalledWith(1, "local:mlx-community/Bonsai2-27B", "session-a");

  await act(async () => first.resolve({ ok: true }));
  await waitFor(() => expect(api.swapPilot).toHaveBeenCalledTimes(2));
  expect(api.swapPilot).toHaveBeenNthCalledWith(2, "anthropic:claude-sonnet-4-6", "session-a");
  expect(screen.getByTestId("setup-notice")).toHaveTextContent("Setting claude-sonnet-4-6 for this session");

  await act(async () => second.resolve({ ok: true }));
  await waitFor(() => expect(screen.getByTestId("setup-notice")).toBeEmptyDOMElement());
});
