import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import {
  clearTranscriptCache,
  captureTranscriptRead,
  peekTranscriptCache,
  setShownTranscriptSession,
  writeTranscriptCache,
} from "../components/conversation/transcriptCache";
import {
  prefetchSessionTranscript,
  prefetchSessionTranscripts,
} from "../components/conversation/transcriptPrefetch";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, sessionTranscript: vi.fn() } };
});

beforeEach(() => {
  clearTranscriptCache();
  setShownTranscriptSession(null);
  vi.clearAllMocks();
});

describe("prefetchSessionTranscript", () => {
  it("fills a cold cache from sessionTranscript", async () => {
    vi.mocked(api.sessionTranscript).mockResolvedValue({
      display: [{ type: "msg", role: "user", text: "hi" }],
    });
    await expect(prefetchSessionTranscript("sess-a")).resolves.toBe(true);
    expect(peekTranscriptCache("sess-a")?.length).toBeGreaterThan(0);
    expect(api.sessionTranscript).toHaveBeenCalledWith("sess-a");
  });

  it("no-ops on warm hit", async () => {
    writeTranscriptCache("sess-b", [
      { kind: "msg", msg: { role: "user", text: "cached" } },
    ] as any);
    await expect(prefetchSessionTranscript("sess-b")).resolves.toBe(false);
    expect(api.sessionTranscript).not.toHaveBeenCalled();
  });
});

describe("shown session ownership", () => {
  it("never writes the session on screen, even when opened mid-flight", async () => {
    // Opened with no cache entry: its hydrate captured "no entry" as baseline.
    setShownTranscriptSession("sess-open");
    const read = captureTranscriptRead("sess-open", { current: [] }, { current: 0 });
    await expect(prefetchSessionTranscript("sess-open")).resolves.toBe(false);
    expect(api.sessionTranscript).not.toHaveBeenCalled();

    let resolve!: (v: unknown) => void;
    vi.mocked(api.sessionTranscript).mockReturnValue(new Promise(r => { resolve = r; }) as any);
    const inFlight = prefetchSessionTranscript("sess-next");
    setShownTranscriptSession("sess-next");
    resolve({ display: [{ type: "msg", role: "user", text: "hi" }] });
    await expect(inFlight).resolves.toBe(false);
    expect(peekTranscriptCache("sess-next")).toBeUndefined();
    // The hydrate's baseline stays valid, so its load can clear the stale flag.
    expect(read()).toBe(true);
  });
});

describe("prefetchSessionTranscripts", () => {
  it("bounds work and skips warm ids", async () => {
    writeTranscriptCache("warm", [
      { kind: "msg", msg: { role: "user", text: "x" } },
    ] as any);
    vi.mocked(api.sessionTranscript).mockResolvedValue({
      display: [{ type: "msg", role: "user", text: "n" }],
    });
    await prefetchSessionTranscripts(["warm", "c1", "c2", "c3"], 2);
    expect(api.sessionTranscript).toHaveBeenCalledTimes(2);
  });
});

describe("bounded warm cache", () => {
  it("evicts the least recently written session, never the shown one", async () => {
    const { TRANSCRIPT_CACHE_MAX } = await import("../components/conversation/transcriptCache");
    setShownTranscriptSession("shown");
    writeTranscriptCache("shown", []);
    for (let i = 0; i < TRANSCRIPT_CACHE_MAX + 5; i++) writeTranscriptCache(`s${i}`, []);
    expect(peekTranscriptCache("shown")).toBeDefined();
    expect(peekTranscriptCache("s0")).toBeUndefined();
    expect(peekTranscriptCache(`s${TRANSCRIPT_CACHE_MAX + 4}`)).toBeDefined();
  });

  it("prefetch only fills free room", async () => {
    const { TRANSCRIPT_CACHE_MAX } = await import("../components/conversation/transcriptCache");
    for (let i = 0; i < TRANSCRIPT_CACHE_MAX; i++) writeTranscriptCache(`v${i}`, []);
    vi.mocked(api.sessionTranscript).mockResolvedValue({ display: [] });
    await expect(prefetchSessionTranscript("new-one")).resolves.toBe(false);
    expect(api.sessionTranscript).not.toHaveBeenCalled();
    expect(peekTranscriptCache("v0")).toBeDefined();
  });
});
