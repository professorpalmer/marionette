import { afterEach, expect, it, vi } from "vitest";
import { createGitRefreshCoordinator } from "../lib/gitRefreshCoordinator";
import { nativeGit } from "../lib/transport";
import { withDesktopEndpointDiscovery } from "./endpointFixture";

afterEach(() => { Reflect.deleteProperty(window, "harnessIPC"); vi.useRealTimers(); });
it("keeps the status lane occupied through IPC failure and deferred HTTP fallback", async () => {
  vi.useFakeTimers();
  let resolve!: (value: { kind: string; status: number; correlationId: string; text: string }) => void;
  const http = new Promise<{ kind: string; status: number; correlationId: string; text: string }>(r => { resolve = r; });
  const ipc = vi.fn().mockResolvedValue({ ok: false, error: "ipc unavailable" });
  const requestJSON = vi.fn().mockReturnValue(http);
  Object.defineProperty(window, "harnessIPC", { configurable: true, value: {
    git: { status: ipc }, endpointHeaders: true, requestJSON: withDesktopEndpointDiscovery(requestJSON),
  } });
  const results: unknown[] = [];
  const c = createGitRefreshCoordinator({
    run: async (_lane, context) => { results.push(await nativeGit.status(context.path)); },
    onError: error => { throw error; }, onBusy: () => {},
  });
  const context = c.activate("/a");
  const first = c.request(context, ["status"]);
  const trailing = c.request(context, ["status"]);
  await vi.advanceTimersByTimeAsync(0);
  expect(ipc).toHaveBeenCalledTimes(1);
  expect(requestJSON).toHaveBeenCalledTimes(1);
  expect(results).toHaveLength(0);
  resolve({ kind: "response", status: 200, correlationId: "git-trace", text: JSON.stringify({ ok: true, files: [], branch: "main" }) });
  await Promise.all([first, trailing]);
  expect(ipc).toHaveBeenCalledTimes(2);
  expect(results).toEqual([{ ok: true, files: [], branch: "main" }, { ok: true, files: [], branch: "main" }]);
});
