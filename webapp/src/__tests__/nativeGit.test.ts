import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  const w = window as any;
  delete w.harnessIPC;
  delete w.__HARNESS_TOKEN__;
});

describe("nativeGit HTTP fallback", () => {
  it("falls back to /api/git/status when IPC git bridge is missing", async () => {
    const getJSON = vi.fn().mockResolvedValue({
      ok: true,
      branch: "main",
      files: [{ status: " M", path: "a.ts" }],
    });
    (window as any).harnessIPC = { getJSON };
    (window as any).__HARNESS_TOKEN__ = "tok";

    const { nativeGit } = await import("../lib/transport");
    const res = await nativeGit.status(".");
    expect(res.ok).toBe(true);
    expect(res.files).toHaveLength(1);
    expect(getJSON).toHaveBeenCalledWith(expect.stringContaining("/api/git/status?"));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("uses fetch when no harnessIPC bridge exists (web build)", async () => {
    (window as any).__HARNESS_TOKEN__ = "tok";
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true, branch: "main", files: [{ status: " M", path: "a.ts" }] }),
    });

    const { nativeGit } = await import("../lib/transport");
    const res = await nativeGit.status(".");
    expect(res.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/git/status?"),
      expect.objectContaining({ headers: expect.objectContaining({ "X-Harness-Token": "tok" }) }),
    );
  });

  it("uses IPC when native git succeeds", async () => {
    const status = vi.fn().mockResolvedValue({ ok: true, files: [], branch: "dev" });
    (window as any).harnessIPC = { git: { status } };

    const { nativeGit } = await import("../lib/transport");
    const res = await nativeGit.status("/repo");
    expect(res.ok).toBe(true);
    expect(status).toHaveBeenCalledWith("/repo");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("falls back to HTTP when IPC git returns ok:false", async () => {
    const status = vi.fn().mockResolvedValue({ ok: false, error: "ipc fail" });
    (window as any).harnessIPC = { git: { status } };
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true, branch: "main", files: [] }),
    });

    const { nativeGit } = await import("../lib/transport");
    const res = await nativeGit.status(".");
    expect(res.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalled();
  });
});

describe("gitWritesAvailable", () => {
  it("is false without git stageFile IPC", async () => {
    (window as any).harnessIPC = { git: { status: vi.fn() } };
    const { gitWritesAvailable } = await import("../lib/transport");
    expect(gitWritesAvailable()).toBe(false);
  });

  it("is true when full git IPC bridge is present", async () => {
    (window as any).harnessIPC = {
      git: { status: vi.fn(), stageFile: vi.fn() },
    };
    const { gitWritesAvailable } = await import("../lib/transport");
    expect(gitWritesAvailable()).toBe(true);
  });
});
