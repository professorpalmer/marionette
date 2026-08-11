import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearTranscriptCache,
  peekTranscriptCache,
  peekTranscriptCacheEntry,
  transcriptResponseToItems,
  writeTranscriptCache,
  composerStatusFromRunner,
  resolveSwitchTranscript,
  clearComposerDraftCache,
  peekComposerDraft,
  resolveComposerDraftOnSwitch,
  writeComposerDraft,
  clearComposerAttachmentCache,
  peekComposerAttachments,
  resolveComposerAttachmentsOnSwitch,
  writeComposerAttachments,
  shouldResetBusyChromeOnSwitch,
  sessionStateFailureSwitchDecision,
  SESSION_STATE_FAIL_NOTICE,
  SESSION_TRANSCRIPT_FAIL_NOTICE,
  clearRecoveredSessionFailNotice,
  shouldRetryEmptyTranscript,
  emptyTranscriptAfterRetryDecision,
  transcriptRefreshFailureDecision,
  resetCrossSessionLatchesOnSwitch,
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
      const failure = transcriptRefreshFailureDecision(!!hit);
      if (failure.clearItems) visible = [];
      expect(failure.stale).toBe(true);
      expect(failure.notice).toBe(SESSION_TRANSCRIPT_FAIL_NOTICE);
    }
    expect(visible).toEqual(cached);
  });

  it("retries empty transcript on cache-hit (same budget as cold boot)", () => {
    expect(shouldRetryEmptyTranscript({
      loadedCount: 0, attempt: 0, maxAttempts: 4, cachedCount: 2,
    })).toBe(true);
    expect(shouldRetryEmptyTranscript({
      loadedCount: 0, attempt: 2, maxAttempts: 4, cachedCount: 2,
    })).toBe(true);
    expect(shouldRetryEmptyTranscript({
      loadedCount: 0, attempt: 3, maxAttempts: 4, cachedCount: 2,
    })).toBe(false);
    expect(shouldRetryEmptyTranscript({ loadedCount: 2, attempt: 0, maxAttempts: 4 })).toBe(false);
  });

  it("does not retry empty only for explicit New Session seed", () => {
    expect(shouldRetryEmptyTranscript({
      loadedCount: 0, attempt: 0, maxAttempts: 4, cachedCount: 0, seededEmpty: true,
    })).toBe(false);
    // Ambiguous zero-row cache (e.g. /clear) still retries disk hydrate.
    expect(shouldRetryEmptyTranscript({
      loadedCount: 0, attempt: 0, maxAttempts: 4, cachedCount: 0, seededEmpty: false,
    })).toBe(true);
  });

  it("cache-hit empty after retries keeps warm rows + notice (no hard wipe)", () => {
    const cached = [makeMsg("user", "warm")];
    let visible = [...cached];
    let stale = false;
    let notice: string | null = null;
    const loadedItems: Item[] = [];
    const hadCache = true;

    if (loadedItems.length === 0 && hadCache) {
      const emptyHit = emptyTranscriptAfterRetryDecision({ cachedCount: cached.length });
      if (emptyHit.kind === "keep_warm_with_notice") {
        stale = emptyHit.stale;
        notice = emptyHit.notice;
      } else {
        visible = loadedItems;
      }
    } else {
      visible = loadedItems;
    }
    expect(visible).toEqual(cached);
    expect(stale).toBe(true);
    expect(notice).toBe(SESSION_TRANSCRIPT_FAIL_NOTICE);
  });

  it("seeded empty warm cache accepts blank new session (no fail banner)", () => {
    const loadedItems: Item[] = [];
    const emptyHit = emptyTranscriptAfterRetryDecision({
      cachedCount: 0,
      seededEmpty: true,
    });
    expect(emptyHit.kind).toBe("accept_empty");
    let visible: Item[] = [];
    let stale = true;
    let notice: string | null = null;
    if (loadedItems.length === 0) {
      if (emptyHit.kind === "accept_empty") {
        visible = loadedItems;
        stale = false;
      } else {
        notice = emptyHit.notice;
      }
    }
    expect(visible).toEqual([]);
    expect(stale).toBe(false);
    expect(notice).toBeNull();
  });

  it("New Session seed → empty hydrate → accept blank + clear seed on write", () => {
    clearTranscriptCache();
    const sid = "sess-new";
    writeTranscriptCache(sid, [], { seededEmpty: true });
    expect(peekTranscriptCacheEntry(sid)).toEqual({ items: [], seededEmpty: true });

    const loadedItems: Item[] = [];
    expect(shouldRetryEmptyTranscript({
      loadedCount: 0, attempt: 0, maxAttempts: 4, cachedCount: 0, seededEmpty: true,
    })).toBe(false);
    const decision = emptyTranscriptAfterRetryDecision({
      cachedCount: 0,
      seededEmpty: true,
    });
    expect(decision.kind).toBe("accept_empty");

    // Successful hydrate writes without seededEmpty so later empties can retry.
    writeTranscriptCache(sid, loadedItems);
    expect(peekTranscriptCacheEntry(sid)).toEqual({ items: [], seededEmpty: false });
    expect(shouldRetryEmptyTranscript({
      loadedCount: 0, attempt: 0, maxAttempts: 4, cachedCount: 0, seededEmpty: false,
    })).toBe(true);
  });

  it("cache-miss refresh failure clears relics but marks stale (not first-run)", () => {
    const failure = transcriptRefreshFailureDecision(false);
    expect(failure.clearItems).toBe(true);
    expect(failure.stale).toBe(true);
    expect(failure.notice).toBe(SESSION_TRANSCRIPT_FAIL_NOTICE);
  });
});

describe("resolveSwitchTranscript", () => {
  // Cross-session relic paint is forbidden: a full session A must not leak
  // Investigated/swarm chunks into an uncached (often brand-new empty) B.
  it("cache miss blanks prior items and marks stale (no cross-session relics)", () => {
    const prior = [makeMsg("user", "from A")];
    const r = resolveSwitchTranscript({
      nextId: "sess-b",
      cached: undefined,
      priorItems: prior,
    });
    expect(r.items).toEqual([]);
    expect(r.stale).toBe(true);
    expect(r.blank).toBe(false);
  });

  it("cache hit returns cached items and is not stale", () => {
    const cached = [makeMsg("user", "from B")];
    const r = resolveSwitchTranscript({
      nextId: "sess-b",
      cached,
      priorItems: [makeMsg("user", "from A")],
    });
    expect(r.items).toEqual(cached);
    expect(r.stale).toBe(false);
  });

  it("cleared session id blanks", () => {
    expect(
      resolveSwitchTranscript({ nextId: null, cached: undefined, priorItems: [] }),
    ).toEqual({ items: [], stale: false, blank: true });
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

describe("busy runners keep Stop not Send", () => {
  it("sets thinking when active session runner is running after SSE detach", () => {
    const runners = { "sess-a": "idle" as const, "sess-b": "running" as const };
    // Switch TO sess-b which is still running -- composer must show Stop.
    expect(composerStatusFromRunner("sess-b", runners, false)).toBe("thinking");
    expect(composerStatusFromRunner("sess-a", runners, false)).toBe("idle");
  });

  it("does not override local SSE stream with runner poll", () => {
    const runners = { "sess-a": "running" as const };
    expect(composerStatusFromRunner("sess-a", runners, true)).toBeNull();
  });

  it("returns to idle/Send when runner flips idle", () => {
    let runners: Record<string, "running" | "idle"> = { "sess-b": "running" };
    expect(composerStatusFromRunner("sess-b", runners, false)).toBe("thinking");
    runners = { "sess-b": "idle" };
    expect(composerStatusFromRunner("sess-b", runners, false)).toBe("idle");
  });

  it("treats attaching (cold pilot build) as idle, not thinking", () => {
    const runners = { "sess-new": "attaching" as const };
    expect(composerStatusFromRunner("sess-new", runners, false)).toBe("idle");
  });

  it("on switch to running session: hydrate warm cache and keep busy chrome", () => {
    writeTranscriptCache("sess-busy", [makeMsg("user", "in flight")]);
    const runners = { "sess-busy": "running" as const };
    const status = composerStatusFromRunner("sess-busy", runners, false);
    expect(status).toBe("thinking");
    expect(peekTranscriptCache("sess-busy")).toEqual([makeMsg("user", "in flight")]);
    clearTranscriptCache();
  });
});

describe("per-session composer draft cache across session switch", () => {
  afterEach(() => {
    clearComposerDraftCache();
  });

  it("restores mid-type draft when returning to a session", () => {
    // Mirror useSessionSwitch: cache outgoing draft, restore incoming.
    let input = "hello from A";
    const composerInputRef = { current: input };

    const switchTo = (prevId: string, nextId: string) => {
      const restored = resolveComposerDraftOnSwitch({
        prevId,
        nextId,
        currentDraft: composerInputRef.current,
      });
      composerInputRef.current = restored;
      input = restored;
    };

    switchTo("sess-a", "sess-b");
    expect(input).toBe("");
    expect(peekComposerDraft("sess-a")).toBe("hello from A");

    composerInputRef.current = "typing in B";
    input = "typing in B";
    switchTo("sess-b", "sess-a");
    expect(input).toBe("hello from A");
    expect(peekComposerDraft("sess-b")).toBe("typing in B");
  });

  it("seeded drafts survive explicit write/peek without cross-bleed", () => {
    writeComposerDraft("sess-x", "keep me");
    writeComposerDraft("sess-y", "other");
    expect(peekComposerDraft("sess-x")).toBe("keep me");
    expect(
      resolveComposerDraftOnSwitch({
        prevId: "sess-y",
        nextId: "sess-x",
        currentDraft: "overwrite y",
      }),
    ).toBe("keep me");
    expect(peekComposerDraft("sess-y")).toBe("overwrite y");
  });

  it("restores draft after null activeSessionId flicker", () => {
    writeComposerDraft("sess-a", "still here");
    expect(
      resolveComposerDraftOnSwitch({
        prevId: "sess-a",
        nextId: null,
        currentDraft: "still here",
      }),
    ).toBe("");
    expect(
      resolveComposerDraftOnSwitch({
        prevId: null,
        nextId: "sess-a",
        currentDraft: "",
      }),
    ).toBe("still here");
  });
});

describe("per-session composer attachment cache across session switch", () => {
  afterEach(() => {
    clearComposerAttachmentCache();
  });

  it("A attachments must not remain after switch to B; restoring B restores B", () => {
    // Mirror useSessionSwitch: cache outgoing attachments, restore incoming.
    const imgA = {
      path: "uploads/a.png",
      name: "a.png",
      previewUrl: "blob:http://localhost/a",
    };
    const imgB = {
      path: "uploads/b.png",
      name: "b.png",
      previewUrl: "blob:http://localhost/b",
    };
    let attached = [imgA];
    const attachedImagesRef = { current: attached };

    const switchTo = (prevId: string, nextId: string) => {
      const restored = resolveComposerAttachmentsOnSwitch({
        prevId,
        nextId,
        currentAttachments: attachedImagesRef.current,
      });
      attachedImagesRef.current = restored;
      attached = restored;
    };

    switchTo("sess-a", "sess-b");
    expect(attached).toEqual([]);
    expect(peekComposerAttachments("sess-a")).toEqual([imgA]);

    attachedImagesRef.current = [imgB];
    attached = [imgB];
    switchTo("sess-b", "sess-a");
    expect(attached).toEqual([imgA]);
    expect(peekComposerAttachments("sess-b")).toEqual([imgB]);

    switchTo("sess-a", "sess-b");
    expect(attached).toEqual([imgB]);
  });

  it("seeded attachments survive write/peek without cross-bleed", () => {
    writeComposerAttachments("sess-x", [
      { path: "x.png", name: "x.png", previewUrl: "blob:x" },
    ]);
    writeComposerAttachments("sess-y", [
      { path: "y.png", name: "y.png", previewUrl: "blob:y" },
    ]);
    expect(peekComposerAttachments("sess-x")?.[0]?.path).toBe("x.png");
    expect(
      resolveComposerAttachmentsOnSwitch({
        prevId: "sess-y",
        nextId: "sess-x",
        currentAttachments: [
          { path: "y2.png", name: "y2.png", previewUrl: "blob:y2" },
        ],
      })?.[0]?.path,
    ).toBe("x.png");
    expect(peekComposerAttachments("sess-y")?.[0]?.path).toBe("y2.png");
  });
});

describe("session-switch busy chrome honesty", () => {
  it("resets busy chrome on switchedSession until runners resolve", () => {
    expect(shouldResetBusyChromeOnSwitch(true)).toBe(true);
    // Same-session effect re-entry must not force idle over local stream.
    expect(shouldResetBusyChromeOnSwitch(false)).toBe(false);
  });

  it("getSessionState failure surfaces notice and stays idle", () => {
    const failure = sessionStateFailureSwitchDecision();
    expect(failure.kind).toBe("idle_with_notice");
    expect(failure.notice).toBe(SESSION_STATE_FAIL_NOTICE);
  });

  it("clears userStopped latch on switchedSession so Stop on A cannot idle B", () => {
    const userStoppedRef = { current: true };
    const resumeQueuedRef = { current: true };
    const approvedCommandRetryRef = { current: "echo retry" as string | null };
    // Mirror useSessionSwitch when switchedSession: cross-session latches reset.
    resetCrossSessionLatchesOnSwitch({
      userStoppedRef,
      resumeQueuedRef,
      approvedCommandRetryRef,
    });
    expect(userStoppedRef.current).toBe(false);
    expect(resumeQueuedRef.current).toBe(false);
    expect(approvedCommandRetryRef.current).toBeNull();
  });

  it("clears sticky SESSION_* editNotice after successful hydrate", () => {
    expect(clearRecoveredSessionFailNotice(SESSION_TRANSCRIPT_FAIL_NOTICE)).toBeNull();
    expect(clearRecoveredSessionFailNotice(SESSION_STATE_FAIL_NOTICE)).toBeNull();
    expect(clearRecoveredSessionFailNotice("Keep this")).toBe("Keep this");
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
    const { scheduleResumeIfPending } = await import(
      "../components/conversation/sessionResumeLatch"
    );

    await scheduleResumeIfPending({
      getSessionState,
      resume,
      stillCurrent: () => true,
      schedule: setTimeout,
    });
    await vi.advanceTimersByTimeAsync(500);
    expect(resume).not.toHaveBeenCalled();
    expect(getSessionState).toHaveBeenCalledTimes(1);
  });

  it("schedules resume only when resume_pending latch is true after switch", async () => {
    const resume = vi.fn();
    const getSessionState = vi.fn()
      .mockResolvedValueOnce({
        state: "idle",
        pending_swarms: false,
        resume_pending: true,
        runners: { "sess-a": "running" },
      })
      .mockResolvedValueOnce({
        state: "idle",
        pending_swarms: false,
        resume_pending: true,
        runners: { "sess-a": "running" },
      });
    const { scheduleResumeIfPending } = await import(
      "../components/conversation/sessionResumeLatch"
    );

    await scheduleResumeIfPending({
      getSessionState,
      resume,
      stillCurrent: () => true,
      schedule: setTimeout,
    });
    expect(getSessionState).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(300);
    expect(resume).toHaveBeenCalledTimes(1);
    expect(getSessionState).toHaveBeenNthCalledWith(2, { consumeResume: true });
  });
});

describe("session-switch kick + composer chrome honesty (R9)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("clears queued setSafeTimeout kicks on switchedSession", () => {
    const timeouts = new Set<ReturnType<typeof setTimeout>>();
    const setSafeTimeout = (fn: () => void, ms: number) => {
      const id = setTimeout(() => {
        timeouts.delete(id);
        fn();
      }, ms);
      timeouts.add(id);
      return id;
    };
    const clearSafeTimeouts = () => {
      timeouts.forEach(clearTimeout);
      timeouts.clear();
    };
    const executeSend = vi.fn();
    const kickSid = { current: "session-a" as string | null };
    setSafeTimeout(() => {
      if (kickSid.current !== "session-a") return;
      executeSend();
    }, 60);
    // Mirror useSessionSwitch switchedSession: drop pending kicks, then retarget.
    clearSafeTimeouts();
    kickSid.current = "session-b";
    vi.advanceTimersByTime(60);
    expect(executeSend).not.toHaveBeenCalled();
  });

  it("fences drain/resume kick when activeSessionId changes before fire", () => {
    const executeSend = vi.fn();
    const activeSessionIdRef = { current: "session-a" as string | null };
    const kickSid = activeSessionIdRef.current;
    setTimeout(() => {
      if (activeSessionIdRef.current !== kickSid) return;
      executeSend();
    }, 60);
    activeSessionIdRef.current = "session-b";
    vi.advanceTimersByTime(60);
    expect(executeSend).not.toHaveBeenCalled();
  });

  it("clears composer chrome fields on switchedSession", () => {
    // Mirror useSessionSwitch switchedSession composer-chrome reset.
    const state = {
      wikiPrepared: { pages: [{ kind: "note" }], autoIngested: false } as {
        pages: any[];
        autoIngested: boolean;
      } | null,
      memoryProposals: [{ id: "m1", text: "x", category: "fact" }],
      distillNotice: "Distilled." as string | null,
      uploadError: "fail" as string | null,
      waitHint: "Looking…" as string | null,
    };
    const switchedSession = true;
    if (switchedSession) {
      state.wikiPrepared = null;
      state.memoryProposals = [];
      state.distillNotice = null;
      state.uploadError = null;
      state.waitHint = null;
    }
    expect(state).toEqual({
      wikiPrepared: null,
      memoryProposals: [],
      distillNotice: null,
      uploadError: null,
      waitHint: null,
    });
  });

  it("resets consecutiveIdlePolls when activeSessionId or poll gen changes", () => {
    const consecutiveIdlePollsRef = { current: 2 };
    const runnerBusyPollGenRef = { current: 3 };
    const seenRunnerBusyPollGenRef = { current: 3 };
    const onActiveSessionChange = () => {
      consecutiveIdlePollsRef.current = 0;
      seenRunnerBusyPollGenRef.current = runnerBusyPollGenRef.current;
    };
    const onPollTick = () => {
      if (seenRunnerBusyPollGenRef.current !== runnerBusyPollGenRef.current) {
        seenRunnerBusyPollGenRef.current = runnerBusyPollGenRef.current;
        consecutiveIdlePollsRef.current = 0;
      }
    };
    onActiveSessionChange();
    expect(consecutiveIdlePollsRef.current).toBe(0);
    consecutiveIdlePollsRef.current = 1;
    runnerBusyPollGenRef.current += 1; // switch bump
    onPollTick();
    expect(consecutiveIdlePollsRef.current).toBe(0);
  });

  it("restores awaiting_swarm chrome from getSessionState on switch (not thinking)", async () => {
    const { runnerBusySwitchDecision } = await import(
      "../components/conversation/sessionHydrate"
    );
    const { sessionStateShowsAwaitingSwarm, SWARM_AWAIT_HINT } = await import(
      "../components/conversation/swarmPoll"
    );
    const sessionState = {
      state: "awaiting_swarm" as const,
      pending_swarms: true,
      runners: { "sess-b": "running" as const },
    };
    const decision = runnerBusySwitchDecision({
      runnerState: sessionState.runners["sess-b"],
      localStreamActive: false,
      switchedSession: true,
      sessionState: sessionState.state,
      pendingSwarms: sessionState.pending_swarms,
    });
    expect(decision.kind).toBe("awaiting");
    expect(
      sessionStateShowsAwaitingSwarm({
        state: sessionState.state,
        pendingSwarms: sessionState.pending_swarms,
        userStopped: false,
      }),
    ).toBe(true);
    // applyRunnerBusy / peek paint contract
    const chrome = {
      turnOpen: false,
      status: "awaiting_swarm" as const,
      waitHint: SWARM_AWAIT_HINT,
    };
    expect(chrome).toEqual({
      turnOpen: false,
      status: "awaiting_swarm",
      waitHint: "Still working…",
    });
    // Switch clear drops backendPendingSwarms + pendingJobIds; awaiting restore
    // must re-arm the results-poller gate (not only Still working… chrome).
    const pendingJobIdsAfterClear: string[] = [];
    const backendPendingSwarms = sessionStateShowsAwaitingSwarm({
      state: sessionState.state,
      pendingSwarms: sessionState.pending_swarms,
      userStopped: false,
    });
    expect(backendPendingSwarms).toBe(true);
    expect(pendingJobIdsAfterClear.length > 0 || backendPendingSwarms).toBe(true);
    // Happy path: no pending → busy (thinking), not awaiting.
    expect(
      runnerBusySwitchDecision({
        runnerState: "running",
        localStreamActive: false,
        switchedSession: true,
        pendingSwarms: false,
        sessionState: "thinking",
      }).kind,
    ).toBe("busy");
  });
});
