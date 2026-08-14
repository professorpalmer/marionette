/**
 * P0 daily-driver: Files / SCM / open editor refresh on workspace mutation events.
 */
import { act, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import FileTree from "../components/FileTree";
import SourceControl from "../components/SourceControl";
import FileEditorPane from "../components/FileEditorPane";
import { createApplyStreamEvent } from "../components/conversation/streamEventHandler";
import type { Item } from "../components/TranscriptList";
import { api } from "../lib/api";
import { nativeGit } from "../lib/transport";
import {
  HARNESS_FILE_EDITED,
  HARNESS_REPO_MUTATED,
  mutationEventPath,
  notifyWorkspaceMutated,
} from "../lib/workspaceMutationEvents";

vi.mock("../lib/api", () => ({
  api: {
    config: vi.fn(),
    getWorkspaceFiles: vi.fn(),
    readFile: vi.fn(),
    writeFile: vi.fn(),
    fileRawUrl: vi.fn((p: string) => `raw:${p}`),
  },
}));

vi.mock("@uiw/react-codemirror", () => ({
  default: ({
    value,
    onChange,
  }: {
    value?: string;
    onChange?: (val: string) => void;
  }) => (
    <div>
      <pre data-testid="cm-value">{value || ""}</pre>
      <button
        type="button"
        data-testid="cm-dirty"
        onClick={() => onChange?.(`${value || ""}local-edit`)}
      >
        dirty
      </button>
    </div>
  ),
}));

vi.mock("../lib/transport", async () => {
  const actual = await vi.importActual<typeof import("../lib/transport")>(
    "../lib/transport",
  );
  return {
    ...actual,
    nativeGit: {
      status: vi.fn(),
      branches: vi.fn(),
      diff: vi.fn(),
      diffStaged: vi.fn(),
      stage: vi.fn(),
      unstage: vi.fn(),
      commit: vi.fn(),
      discard: vi.fn(),
    },
    gitWritesAvailable: () => false,
  };
});

describe("workspace freshness fan-out", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(api.config).mockResolvedValue({ repo: "/repo" } as any);
    vi.mocked(api.getWorkspaceFiles).mockResolvedValue({
      files: ["src/a.ts"],
    } as any);
    vi.mocked(nativeGit.status).mockResolvedValue({ ok: true, files: [] } as any);
    vi.mocked(nativeGit.branches).mockResolvedValue({
      ok: true,
      branches: [{ name: "main", active: true }],
    } as any);
    vi.mocked(api.readFile).mockResolvedValue({
      ok: true,
      content: "v1\n",
    } as any);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it("FileTree refreshes on harness-repo-mutated (checkpoint path)", async () => {
    render(<FileTree />);
    await waitFor(() => {
      expect(api.getWorkspaceFiles).toHaveBeenCalled();
    });
    const callsAfterMount = vi.mocked(api.getWorkspaceFiles).mock.calls.length;

    await act(async () => {
      window.dispatchEvent(new Event(HARNESS_REPO_MUTATED));
      await vi.advanceTimersByTimeAsync(200);
    });

    expect(vi.mocked(api.getWorkspaceFiles).mock.calls.length).toBeGreaterThan(
      callsAfterMount,
    );
  });

  it("SourceControl refreshes on harness-file-edited and harness-repo-mutated", async () => {
    render(<SourceControl />);
    await waitFor(() => {
      expect(nativeGit.status).toHaveBeenCalled();
    });
    const callsAfterMount = vi.mocked(nativeGit.status).mock.calls.length;

    await act(async () => {
      window.dispatchEvent(
        new CustomEvent(HARNESS_FILE_EDITED, { detail: { path: "src/a.ts" } }),
      );
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(vi.mocked(nativeGit.status).mock.calls.length).toBeGreaterThan(
      callsAfterMount,
    );

    const afterFileEdited = vi.mocked(nativeGit.status).mock.calls.length;
    await act(async () => {
      window.dispatchEvent(new Event(HARNESS_REPO_MUTATED));
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(vi.mocked(nativeGit.status).mock.calls.length).toBeGreaterThan(
      afterFileEdited,
    );
  });

  it("FileEditorPane reloads disk contents when a matching path mutates", async () => {
    vi.mocked(api.readFile)
      .mockResolvedValueOnce({ ok: true, content: "v1\n" } as any)
      .mockResolvedValueOnce({ ok: true, content: "v2-from-agent\n" } as any);

    const { getByTestId } = render(
      <FileEditorPane
        path="src/a.ts"
        onClose={() => {}}
        onDirtyChange={() => {}}
      />,
    );

    await waitFor(() => {
      expect(api.readFile).toHaveBeenCalledWith("src/a.ts");
      expect(getByTestId("cm-value").textContent).toContain("v1");
    });

    await act(async () => {
      notifyWorkspaceMutated("src/a.ts");
    });

    await waitFor(() => {
      expect(api.readFile).toHaveBeenCalledTimes(2);
      expect(getByTestId("cm-value").textContent).toContain("v2-from-agent");
    });
  });

  it("FileEditorPane reloads a clean buffer on pathless workspace mutation", async () => {
    vi.mocked(api.readFile)
      .mockResolvedValueOnce({ ok: true, content: "v1\n" } as any)
      .mockResolvedValueOnce({ ok: true, content: "v2-from-restore\n" } as any);

    const { getByTestId } = render(
      <FileEditorPane
        path="src/a.ts"
        onClose={() => {}}
        onDirtyChange={() => {}}
      />,
    );

    await waitFor(() => {
      expect(getByTestId("cm-value").textContent).toContain("v1");
    });

    await act(async () => {
      notifyWorkspaceMutated();
    });

    await waitFor(() => {
      expect(api.readFile).toHaveBeenCalledTimes(2);
      expect(getByTestId("cm-value").textContent).toContain("v2-from-restore");
    });
  });

  it("FileEditorPane dirty buffer conflicts on pathless workspace mutation", async () => {
    const onDirtyChange = vi.fn();
    const { getByTestId } = render(
      <FileEditorPane
        path="src/a.ts"
        onClose={() => {}}
        onDirtyChange={onDirtyChange}
      />,
    );

    await waitFor(() => {
      expect(getByTestId("cm-value").textContent).toContain("v1");
    });

    await act(async () => {
      getByTestId("cm-dirty").click();
    });
    expect(onDirtyChange).toHaveBeenCalledWith(true);

    await act(async () => {
      notifyWorkspaceMutated();
    });

    expect(getByTestId("disk-conflict-banner")).toBeTruthy();
    expect(api.readFile).toHaveBeenCalledTimes(1);
    expect(getByTestId("cm-value").textContent).toContain("local-edit");
  });

  it("FileEditorPane ignores mutation events for other paths", async () => {
    render(
      <FileEditorPane
        path="src/a.ts"
        onClose={() => {}}
        onDirtyChange={() => {}}
      />,
    );
    await waitFor(() => {
      expect(api.readFile).toHaveBeenCalledTimes(1);
    });

    await act(async () => {
      notifyWorkspaceMutated("src/other.ts");
    });

    // No second read — path did not match the open tab.
    expect(api.readFile).toHaveBeenCalledTimes(1);
  });

  it("FileEditorPane dirty buffer shows conflict notice on matching mutation", async () => {
    const onDirtyChange = vi.fn();
    const { getByTestId, queryByTestId } = render(
      <FileEditorPane
        path="src/a.ts"
        onClose={() => {}}
        onDirtyChange={onDirtyChange}
      />,
    );

    await waitFor(() => {
      expect(getByTestId("cm-value").textContent).toContain("v1");
    });

    await act(async () => {
      getByTestId("cm-dirty").click();
    });
    expect(onDirtyChange).toHaveBeenCalledWith(true);
    expect(queryByTestId("disk-conflict-banner")).toBeNull();

    await act(async () => {
      notifyWorkspaceMutated("src/a.ts");
    });

    expect(getByTestId("disk-conflict-banner")).toBeTruthy();
    // Dirty path must not silently reload.
    expect(api.readFile).toHaveBeenCalledTimes(1);
    expect(getByTestId("cm-value").textContent).toContain("local-edit");
  });

  it("FileEditorPane conflict Reload from disk replaces buffer and clears dirty", async () => {
    const onDirtyChange = vi.fn();
    vi.mocked(api.readFile)
      .mockResolvedValueOnce({ ok: true, content: "v1\n" } as any)
      .mockResolvedValueOnce({ ok: true, content: "v2-from-agent\n" } as any);

    const { getByTestId, queryByTestId } = render(
      <FileEditorPane
        path="src/a.ts"
        onClose={() => {}}
        onDirtyChange={onDirtyChange}
      />,
    );

    await waitFor(() => {
      expect(getByTestId("cm-value").textContent).toContain("v1");
    });

    await act(async () => {
      getByTestId("cm-dirty").click();
    });
    await act(async () => {
      notifyWorkspaceMutated("src/a.ts");
    });
    expect(getByTestId("disk-conflict-banner")).toBeTruthy();

    await act(async () => {
      getByTestId("disk-conflict-reload").click();
    });

    await waitFor(() => {
      expect(api.readFile).toHaveBeenCalledTimes(2);
      expect(getByTestId("cm-value").textContent).toContain("v2-from-agent");
    });
    expect(queryByTestId("disk-conflict-banner")).toBeNull();
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
  });

  it("FileEditorPane conflict Keep mine dismisses notice and preserves edits", async () => {
    const onDirtyChange = vi.fn();
    const { getByTestId, queryByTestId } = render(
      <FileEditorPane
        path="src/a.ts"
        onClose={() => {}}
        onDirtyChange={onDirtyChange}
      />,
    );

    await waitFor(() => {
      expect(getByTestId("cm-value").textContent).toContain("v1");
    });

    await act(async () => {
      getByTestId("cm-dirty").click();
    });
    await act(async () => {
      notifyWorkspaceMutated("src/a.ts");
    });
    expect(getByTestId("disk-conflict-banner")).toBeTruthy();

    await act(async () => {
      getByTestId("disk-conflict-keep").click();
    });

    expect(queryByTestId("disk-conflict-banner")).toBeNull();
    expect(getByTestId("cm-value").textContent).toContain("local-edit");
    expect(api.readFile).toHaveBeenCalledTimes(1);
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
  });

  it("action_result with types=file + path notifies workspace mutation bus", () => {
    const seen: Array<{ type: string; path: string | null }> = [];
    const onAny = (e: Event) => {
      seen.push({ type: e.type, path: mutationEventPath(e) });
    };
    window.addEventListener(HARNESS_FILE_EDITED, onAny);
    window.addEventListener(HARNESS_REPO_MUTATED, onAny);

    const items: Item[] = [];
    const itemsRef = { current: items };
    const apply = createApplyStreamEvent({
      setCompactingStatus: () => {},
      setItems: (updater) => {
        const next = typeof updater === "function" ? updater(items) : updater;
        items.length = 0;
        items.push(...next);
        itemsRef.current = items;
      },
      setDistillNotice: () => {},
      setWikiPrepared: () => {},
      setMemoryProposals: () => {},
      setWaitHint: () => {},
      setStatus: () => {},
      setTurnOpen: () => {},
      setPendingJobIds: () => {},
      pendingJobIdsRef: { current: [] },
      setSafeTimeout: () => {},
      itemsRef,
      planTurnRef: { current: false },
      turnSettledRef: { current: false },
      resumeQueuedRef: { current: false },
      typeBufRef: { current: "" },
      flushTypewriter: () => {},
      startTypewriter: () => {},
      appendStreamingText: () => {},
      setCard: () => {},
      onArtifacts: () => {},
      onJobChange: () => {},
      handleSwarmResult: () => {},
      refreshQueue: () => {},
      fetchContextUsage: () => {},
    });

    try {
      apply({
        kind: "action_result",
        data: {
          id: "w1",
          types: ["file"],
          path: "harness/foo.py",
          artifacts: [{ type: "file", headline: "Wrote 3 bytes" }],
        },
      });
      expect(seen).toEqual([
        { type: HARNESS_FILE_EDITED, path: "harness/foo.py" },
        { type: HARNESS_REPO_MUTATED, path: "harness/foo.py" },
      ]);

      seen.length = 0;
      apply({
        kind: "action_result",
        data: {
          id: "w2",
          types: ["workspace"],
          path: "/repo",
        },
      });
      expect(seen).toEqual([]);
    } finally {
      window.removeEventListener(HARNESS_FILE_EDITED, onAny);
      window.removeEventListener(HARNESS_REPO_MUTATED, onAny);
    }
  });
});
