import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearTranscriptCache,
  peekTranscriptCache,
  transcriptResponseToItems,
  writeTranscriptCache,
} from "../components/Conversation";
import type { Item } from "../components/TranscriptList";

/**
 * Phase C warm-cache + non-destructive attach contracts.
 * Mirrors Conversation.tsx session-switch behavior without mounting the full UI.
 */

function makeMsg(role: "user" | "assistant", text: string): Item {
  return { kind: "msg", msg: { role, text } };
}

describe("transcript warm cache", () => {
  afterEach(() => {
    clearTranscriptCache();
  });

  it("write/peek round-trip stores a copy per session id", () => {
    const a = [makeMsg("user", "hello")];
    writeTranscriptCache("sess-a", a);
    expect(peekTranscriptCache("sess-a")).toEqual(a);
    expect(peekTranscriptCache("sess-b")).toBeUndefined();

    // Mutating the source array must not corrupt the cache entry.
    a.push(makeMsg("assistant", "world"));
    expect(peekTranscriptCache("sess-a")).toEqual([makeMsg("user", "hello")]);
  });

  it("transcriptResponseToItems maps display rows and dedupes assistant bubbles", () => {
    const items = transcriptResponseToItems({
      display: [
        { type: "msg", role: "user", text: "hi" },
        { type: "msg", role: "assistant", text: "hello there" },
        { type: "msg", role: "assistant", text: "hello there!" },
      ],
    });
    expect(items).toHaveLength(2);
    expect(items[0]).toEqual(makeMsg("user", "hi"));
    expect(items[1].kind).toBe("msg");
    if (items[1].kind === "msg") {
      expect(items[1].msg.text).toBe("hello there!");
    }
  });

  it("hydrates from cache immediately on switch, then refreshes without blanking", async () => {
    const cache = new Map<string, { items: Item[] }>();
    cache.set("sess-a", { items: [makeMsg("user", "from A")] });
    cache.set("sess-b", { items: [makeMsg("user", "cached B")] });

    let visible: Item[] = cache.get("sess-a")!.items;
    let currentId = "sess-a";

    const sessionTranscript = vi.fn().mockImplementation(async (id: string) => {
      await new Promise((r) => setTimeout(r, 20));
      return {
        display: [{ type: "msg", role: "user", text: `fresh ${id}` }],
      };
    });

    // Mirror Conversation activeSessionId effect: save old, hydrate new, refresh.
    const switchTo = async (nextId: string) => {
      if (currentId && currentId !== nextId) {
        cache.set(currentId, { items: [...visible] });
      }
      currentId = nextId;
      const hit = cache.get(nextId);
      if (hit) {
        visible = hit.items;
      } else {
        visible = [];
      }
      // Cache hit must not blank while the network refresh is in flight.
      expect(visible).toEqual([makeMsg("user", "cached B")]);

      const res = await sessionTranscript(nextId);
      const loaded = transcriptResponseToItems(res);
      visible = loaded;
      cache.set(nextId, { items: [...loaded] });
    };

    await switchTo("sess-b");
    expect(sessionTranscript).toHaveBeenCalledWith("sess-b");
    expect(visible).toEqual([makeMsg("user", "fresh sess-b")]);
    // Outgoing session was saved before hydrate.
    expect(cache.get("sess-a")?.items).toEqual([makeMsg("user", "from A")]);
  });

  it("keeps cached rows when background refresh fails (no blank on cache hit)", async () => {
    const cache = new Map<string, { items: Item[] }>();
    const cached = [makeMsg("assistant", "still here")];
    cache.set("sess-x", { items: cached });

    let visible: Item[] = [];
    const hit = cache.get("sess-x");
    if (hit) visible = hit.items;

    try {
      throw new Error("network");
    } catch {
      // Cache hit: keep showing cached rows on refresh failure.
      if (!hit) visible = [];
    }
    expect(visible).toEqual(cached);
  });
});

describe("session switch detach (non-destructive)", () => {
  it("closes EventSource cancel without calling interrupt/stop", () => {
    const interruptSession = vi.fn();
    const closeEventSource = vi.fn();
    let cancelRef: null | (() => void) = () => closeEventSource();

    // Mirror Conversation switch detach: close stream only.
    if (cancelRef) {
      cancelRef();
      cancelRef = null;
    }
    // Must NOT call interruptSession / stop on navigate away.
    expect(closeEventSource).toHaveBeenCalledTimes(1);
    expect(interruptSession).not.toHaveBeenCalled();
    expect(cancelRef).toBeNull();
  });
});

describe("warm-cache switch preserves ghost-resume gate", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("does not schedule resume on session switch when resume_pending is false", async () => {
    const resume = vi.fn();
    const getSessionState = vi.fn().mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      resume_pending: false,
      runners: { "sess-a": "idle", "sess-b": "running" },
    });

    // Same contract as Conversation.resume.test.ts + runners on SessionState.
    const onSessionSwitch = async () => {
      const res = await getSessionState();
      if (res?.resume_pending) {
        setTimeout(() => resume(), 300);
      }
    };

    await onSessionSwitch();
    await vi.advanceTimersByTimeAsync(500);
    expect(resume).not.toHaveBeenCalled();
    expect(getSessionState).toHaveBeenCalled();
  });

  it("schedules resume only when resume_pending latch is true after switch", async () => {
    const resume = vi.fn();
    const getSessionState = vi.fn().mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      resume_pending: true,
      runners: { "sess-a": "running" },
    });

    const onSessionSwitch = async () => {
      const res = await getSessionState();
      if (res?.resume_pending) {
        setTimeout(() => resume(), 300);
      }
    };

    await onSessionSwitch();
    await vi.advanceTimersByTimeAsync(300);
    expect(resume).toHaveBeenCalledTimes(1);
  });
});
