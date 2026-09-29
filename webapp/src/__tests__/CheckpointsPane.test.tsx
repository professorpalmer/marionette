import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import CheckpointsPane from "../components/CheckpointsPane";

vi.mock("../lib/panelTransition", () => ({
  lastSelectedProjectRoot: "/repo",
}));

vi.mock("../lib/useOperationalDiagnostic", () => ({
  usePanelNotice: (value: string | null) => value,
}));

const apiMocks = vi.hoisted(() => ({
  getCheckpoints: vi.fn(),
  getCheckpointDiff: vi.fn(),
  getWorkspace: vi.fn(),
  sessions: vi.fn(),
}));

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      getCheckpoints: apiMocks.getCheckpoints,
      getCheckpointDiff: apiMocks.getCheckpointDiff,
      getWorkspace: apiMocks.getWorkspace,
      sessions: apiMocks.sessions,
    },
  };
});

describe("CheckpointsPane diff badges", () => {
  beforeEach(() => {
    apiMocks.getCheckpoints.mockReset();
    apiMocks.getCheckpointDiff.mockReset();
    apiMocks.getWorkspace.mockReset();
    apiMocks.sessions.mockReset();
    apiMocks.getWorkspace.mockResolvedValue({ repo: "/repo" });
    apiMocks.sessions.mockResolvedValue([{ id: "s1", active: true }]);
    apiMocks.getCheckpoints.mockResolvedValue([
      {
        id: "cp-1",
        label: "before edits",
        timestamp: 1,
        files: [],
      },
    ]);
    apiMocks.getCheckpointDiff.mockResolvedValue({
      ok: true,
      diff: "",
      truncated: false,
      files: [
        { path: "src/new.ts", status: "added" },
        { path: "src/old.ts", status: "modified" },
        { path: "src/gone.ts", status: "removed" },
      ],
    });
  });

  it("renders added/modified/removed badges with visible text and aria labels", async () => {
    render(<CheckpointsPane />);

    await screen.findByText("before edits");
    // refreshScope sets activeSessionId after first paint; that changes
    // scopeKey and clearLocalState() wipes expanded diffs. Click Diff only
    // after the second checkpoints fetch.
    await waitFor(() => {
      expect(apiMocks.sessions).toHaveBeenCalled();
      expect(apiMocks.getCheckpoints.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
    await screen.findByText("before edits");
    fireEvent.click(screen.getByTitle("View diff"));

    await waitFor(() => {
      expect(apiMocks.getCheckpointDiff).toHaveBeenCalledWith("cp-1");
    });

    const addedBadge = await screen.findByLabelText("added: src/new.ts");
    const modifiedBadge = await screen.findByLabelText("modified: src/old.ts");
    const removedBadge = await screen.findByLabelText("removed: src/gone.ts");

    expect(addedBadge).toHaveTextContent("added");
    expect(modifiedBadge).toHaveTextContent("modified");
    expect(removedBadge).toHaveTextContent("removed");
    expect(screen.getByText("src/new.ts")).toBeTruthy();
    expect(screen.getByText("src/old.ts")).toBeTruthy();
    expect(screen.getByText("src/gone.ts")).toBeTruthy();
  });
});

describe("CheckpointsPane diff freshness", () => {
  beforeEach(() => {
    apiMocks.getCheckpoints.mockReset();
    apiMocks.getCheckpointDiff.mockReset();
    apiMocks.getWorkspace.mockReset();
    apiMocks.sessions.mockReset();
    apiMocks.getWorkspace.mockResolvedValue({ repo: "/repo" });
    apiMocks.sessions.mockResolvedValue([{ id: "s1", active: true }]);
    apiMocks.getCheckpoints.mockResolvedValue([{ id: "cp-1", label: "before edits", timestamp: 1, files: [] }]);
  });

  it("refreshes an open diff after the repo changes", async () => {
    apiMocks.getCheckpointDiff
      .mockResolvedValueOnce({ ok: true, diff: "", truncated: false, files: [] })
      .mockResolvedValue({ ok: true, diff: "", truncated: false, files: [{ path: "a.py", status: "modified" }] });
    render(<CheckpointsPane />);
    await waitFor(() => expect(apiMocks.getCheckpoints.mock.calls.length).toBeGreaterThanOrEqual(2));
    await screen.findByText("before edits");
    fireEvent.click(screen.getByTitle("View diff"));
    await waitFor(() => expect(apiMocks.getCheckpointDiff).toHaveBeenCalledTimes(1));
    window.dispatchEvent(new Event("harness-repo-mutated"));
    expect(await screen.findByText("a.py")).toBeTruthy();
    expect(apiMocks.getCheckpointDiff).toHaveBeenCalledTimes(2);
  });
});

describe("CheckpointsPane startup race", () => {
  beforeEach(() => {
    apiMocks.getCheckpoints.mockReset();
    apiMocks.getWorkspace.mockReset();
    apiMocks.sessions.mockReset();
    apiMocks.getWorkspace.mockResolvedValue({ repo: "/repo" });
    apiMocks.sessions.mockResolvedValue([{ id: "s1", active: true }]);
  });

  const refused = () => Object.assign(new Error("connect ECONNREFUSED 127.0.0.1:8799"), { code: "ECONNREFUSED" });

  it("retries a refused connection quietly instead of showing a red banner", async () => {
    apiMocks.getCheckpoints
      .mockRejectedValueOnce(refused())
      .mockRejectedValueOnce(refused())
      .mockResolvedValue([{ id: "cp-1", label: "before edits", timestamp: 1, files: [] }]);
    render(<CheckpointsPane />);
    expect(await screen.findByText("before edits", {}, { timeout: 4000 })).toBeTruthy();
    expect(screen.queryByText(/retrying|unavailable|ECONNREFUSED/i)).toBeNull();
  });

  it("asks for a manual refresh once the retries are spent", async () => {
    apiMocks.getCheckpoints.mockRejectedValue(refused());
    render(<CheckpointsPane />);
    expect(await screen.findByText(/briefly unavailable/i, {}, { timeout: 8000 })).toBeTruthy();
  }, 10000);
});

it("paints Loading, never the empty state, before the first load", async () => {
  // The first paint is the pre-effect state; server rendering runs no effects.
  const { renderToString } = await import("react-dom/server");
  const html = renderToString(<CheckpointsPane />);
  expect(html).not.toContain("No restore points");
  expect(html).toContain("Loading restore points");
});

it("keeps the list through a turn-end config event and shows the new checkpoint", async () => {
  apiMocks.getWorkspace.mockResolvedValue({ repo: "/repo" });
  apiMocks.sessions.mockResolvedValue([{ id: "s1", active: true }]);
  apiMocks.getCheckpoints.mockResolvedValue([{ id: "cp-1", label: "before edits", timestamp: 1, files: [] }]);
  render(<CheckpointsPane />);
  await screen.findByText("before edits");
  apiMocks.getCheckpoints.mockResolvedValue([
    { id: "cp-2", label: "after turn", timestamp: 2, files: [] },
    { id: "cp-1", label: "before edits", timestamp: 1, files: [] },
  ]);
  window.dispatchEvent(new Event("harness-config-changed"));
  expect(screen.queryByText("No restore points available yet.")).toBeNull();
  await screen.findByText("after turn");
  expect(screen.getByText("before edits")).toBeTruthy();
});
