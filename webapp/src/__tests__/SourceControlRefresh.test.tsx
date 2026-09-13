import { act, render, screen, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import SourceControl from "../components/SourceControl";
import { api } from "../lib/api";
import { nativeGit } from "../lib/transport";
import { notifyWorkspaceMutated } from "../lib/workspaceMutationEvents";

vi.mock("../lib/api", () => ({ api: { config: vi.fn() } }));
vi.mock("../lib/transport", () => ({
  nativeGit: { status: vi.fn(), branches: vi.fn(), stageAll: vi.fn(), unstageAll: vi.fn(), stageFile: vi.fn(), unstageFile: vi.fn(), commit: vi.fn(), diff: vi.fn(), diffStaged: vi.fn(), applyHunk: vi.fn() },
  gitWritesAvailable: () => true,
}));
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(r => { resolve = r; });
  return { promise, resolve };
}
const status = { ok: true, branch: "main", files: [] };
const branches = { ok: true, branches: [{ name: "main", active: true }] };
beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(api.config).mockResolvedValue({ repo: "/a" });
  vi.mocked(nativeGit.stageAll).mockResolvedValue({ ok: true });
  vi.mocked(nativeGit.unstageAll).mockResolvedValue({ ok: true });
  vi.mocked(nativeGit.stageFile).mockResolvedValue({ ok: true });
  vi.mocked(nativeGit.unstageFile).mockResolvedValue({ ok: true });
  vi.mocked(nativeGit.commit).mockResolvedValue({ ok: true });
  vi.mocked(nativeGit.diff).mockResolvedValue({ ok: true, out: "diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -1 +1 @@\n-old\n+new" });
  vi.mocked(nativeGit.diffStaged).mockResolvedValue({ ok: true, out: "staged diff" });
  vi.mocked(nativeGit.applyHunk).mockResolvedValue({ ok: true });
  vi.mocked(nativeGit.status).mockResolvedValue(status);
  vi.mocked(nativeGit.branches).mockResolvedValue(branches);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.resetAllMocks(); });
async function mount() { await act(async () => { render(<SourceControl />); }); }
it("five paired mutation bursts cause three status reads and no branches reads", async () => {
  await mount();
  vi.mocked(nativeGit.status).mockClear();
  vi.mocked(nativeGit.branches).mockClear();
  for (let i = 0; i < 5; i++) {
    await act(async () => {
      notifyWorkspaceMutated("a.ts");
      await vi.advanceTimersByTimeAsync(200);
    });
  }
  await act(async () => { await vi.advanceTimersByTimeAsync(500); });
  expect(nativeGit.status).toHaveBeenCalledTimes(3);
  expect(nativeGit.branches).not.toHaveBeenCalled();
});
it("status paints before a slow branches request settles", async () => {
  const pending = deferred<typeof branches>();
  vi.mocked(nativeGit.branches).mockReturnValue(pending.promise);
  vi.mocked(nativeGit.status).mockResolvedValue({ ...status, files: [{ status: " M", path: "visible.ts" }] });
  await mount();
  expect(screen.getByText("visible.ts")).toBeInTheDocument();
  await act(async () => { pending.resolve(branches); });
});
async function select(path: string) {
  await act(async () => { window.dispatchEvent(new CustomEvent("harness-project-selected", { detail: path })); });
}
it("an automatic reported branch change refreshes branches once", async () => {
  await mount();
  vi.mocked(nativeGit.status).mockResolvedValue({ ...status, branch: "dev" });
  await act(async () => { notifyWorkspaceMutated(); await vi.advanceTimersByTimeAsync(1000); });
  expect(nativeGit.branches).toHaveBeenCalledTimes(2);
});
it("A-B-A suppresses old paints, retains last good lists, and never overlaps status", async () => {
  vi.mocked(nativeGit.status).mockResolvedValue({ ...status, files: [{ status: " M", path: "good.ts" }] });
  await mount();
  const old = deferred<{ ok: boolean; branch: string; files: { status: string; path: string }[] }>();
  vi.mocked(nativeGit.status).mockReturnValueOnce(old.promise);
  await act(async () => { notifyWorkspaceMutated(); });
  await select("/b");
  await select("/a");
  expect(nativeGit.status).toHaveBeenCalledTimes(2);
  expect(screen.getByText("good.ts")).toBeInTheDocument();
  vi.mocked(nativeGit.status).mockResolvedValue({ ok: false, error: "new failure" });
  await act(async () => { old.resolve({ ...status, files: [{ status: " M", path: "stale.ts" }] }); });
  expect(nativeGit.status).toHaveBeenCalledTimes(3);
  expect(nativeGit.status).toHaveBeenLastCalledWith("/a");
  expect(screen.queryByText("stale.ts")).not.toBeInTheDocument();
  expect(screen.getByText("good.ts")).toBeInTheDocument();
  expect(screen.getByText("new failure")).toBeInTheDocument();
});
it("late mount config cannot replace a selected project", async () => {
  const config = deferred<{ repo: string }>();
  vi.mocked(api.config).mockReturnValue(config.promise);
  await mount();
  await select("/b");
  await act(async () => { config.resolve({ repo: "/a" }); });
  expect(nativeGit.status).toHaveBeenCalledTimes(1);
  expect(nativeGit.status).toHaveBeenLastCalledWith("/b");
});
it("new config invalidates older config responses before the debounce expires", async () => {
  await mount();
  const old = deferred<{ repo: string }>();
  const next = deferred<{ repo: string }>();
  vi.mocked(api.config).mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise);
  await act(async () => {
    window.dispatchEvent(new Event("harness-config-changed"));
    await vi.advanceTimersByTimeAsync(180);
    window.dispatchEvent(new Event("harness-config-changed"));
    old.resolve({ repo: "/old" });
  });
  expect(nativeGit.status).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(180); next.resolve({ repo: "/new" }); });
  expect(nativeGit.status).toHaveBeenLastCalledWith("/new");
  expect(nativeGit.branches).toHaveBeenLastCalledWith("/new");
});
it("unmount cancels scheduled work and ignores late config", async () => {
  await mount();
  await act(async () => { notifyWorkspaceMutated(); });
  const config = deferred<{ repo: string }>();
  vi.mocked(api.config).mockReturnValue(config.promise);
  await act(async () => {
    notifyWorkspaceMutated();
    window.dispatchEvent(new Event("harness-config-changed"));
    await vi.advanceTimersByTimeAsync(180);
  });
  const reads = vi.mocked(nativeGit.status).mock.calls.length;
  cleanup();
  await act(async () => { config.resolve({ repo: "/late" }); await vi.advanceTimersByTimeAsync(1000); });
  expect(nativeGit.status).toHaveBeenCalledTimes(reads);
});
it("status failure does not discard successful branches", async () => {
  vi.mocked(nativeGit.status).mockRejectedValue(new Error("status unavailable"));
  await mount();
  expect(screen.getByText("main")).toBeInTheDocument();
  expect(screen.getByText("status unavailable")).toBeInTheDocument();
  expect(screen.getByTitle("Refresh Git status")).not.toBeDisabled();
});
it("stage, unstage, and hunk actions request status only; commit requests both", async () => {
  vi.mocked(nativeGit.status).mockResolvedValue({ ...status, files: [{ status: "MM", path: "a.ts" }] });
  await mount();
  vi.mocked(nativeGit.status).mockClear();
  vi.mocked(nativeGit.branches).mockClear();
  for (const title of ["Stage file", "Unstage file"]) {
    await act(async () => { fireEvent.click(screen.getByTitle(title)); });
  }
  for (const name of ["Stage all", "Unstage all"]) {
    await act(async () => { fireEvent.click(screen.getByRole("button", { name, exact: true })); });
  }
  await act(async () => { fireEvent.click(screen.getAllByText("a.ts")[1]); });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Stage hunk", exact: true })); });
  expect(nativeGit.status).toHaveBeenCalledTimes(5);
  expect(nativeGit.branches).not.toHaveBeenCalled();
  await act(async () => { fireEvent.change(screen.getByPlaceholderText("Commit message... (no emojis)"), { target: { value: "test" } }); });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Commit", exact: true })); });
  expect(nativeGit.status).toHaveBeenCalledTimes(6);
  expect(nativeGit.branches).toHaveBeenCalledTimes(1);
});
it("manual refresh promotes scheduled mutation work and preserves button busy state", async () => {
  await mount();
  await act(async () => { notifyWorkspaceMutated(); });
  expect(screen.getByTitle("Refresh Git status")).not.toBeDisabled();
  const pending = deferred<typeof status>();
  vi.mocked(nativeGit.status).mockReturnValueOnce(pending.promise);
  await act(async () => { fireEvent.click(screen.getByTitle("Refresh Git status")); });
  expect(nativeGit.status).toHaveBeenCalledTimes(3);
  expect(nativeGit.branches).toHaveBeenCalledTimes(2);
  expect(screen.getByTitle("Refresh Git status")).toBeDisabled();
  await act(async () => { pending.resolve(status); await vi.advanceTimersByTimeAsync(1000); });
  expect(nativeGit.status).toHaveBeenCalledTimes(3);
  expect(screen.getByTitle("Refresh Git status")).not.toBeDisabled();
});
it("a stage finishing after A-B-A cannot request or paint new work", async () => {
  vi.mocked(nativeGit.status).mockResolvedValue({ ...status, files: [{ status: " M", path: "a.ts" }] });
  await mount();
  const pending = deferred<{ ok: boolean }>();
  vi.mocked(nativeGit.stageAll).mockReturnValueOnce(pending.promise);
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Stage all", exact: true })); });
  await select("/b"); await select("/a");
  const count = vi.mocked(nativeGit.status).mock.calls.length;
  await act(async () => { pending.resolve({ ok: true }); });
  expect(nativeGit.status).toHaveBeenCalledTimes(count);
});
it("an automatic status resolving after unmount cannot trigger branches or trailing status", async () => {
  await mount();
  const pending = deferred<typeof status>();
  vi.mocked(nativeGit.status).mockReturnValueOnce(pending.promise);
  await act(async () => { notifyWorkspaceMutated(); });
  cleanup();
  await act(async () => { pending.resolve({ ...status, branch: "dev" }); await vi.advanceTimersByTimeAsync(1000); });
  expect(nativeGit.status).toHaveBeenCalledTimes(2);
  expect(nativeGit.branches).toHaveBeenCalledTimes(1);
});
it("late branches cannot repaint a new context", async () => {
  const pending = deferred<typeof branches>();
  vi.mocked(nativeGit.branches).mockReturnValueOnce(pending.promise).mockResolvedValue({ ok: false });
  await mount();
  await select("/b");
  expect(nativeGit.branches).toHaveBeenCalledTimes(1);
  await act(async () => { pending.resolve(branches); });
  expect(nativeGit.branches).toHaveBeenCalledTimes(2);
  expect(screen.queryByText("main")).not.toBeInTheDocument();
});
