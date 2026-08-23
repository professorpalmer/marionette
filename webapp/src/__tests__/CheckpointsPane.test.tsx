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

    const toggle = await screen.findByTitle("View diff");
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(apiMocks.getCheckpointDiff).toHaveBeenCalledWith("cp-1");
    });

    for (const label of ["added", "modified", "removed"]) {
      const badge = screen.getByLabelText(new RegExp(`^${label}:`, "i"));
      expect(badge).toHaveTextContent(label);
    }
  });
});
