import { describe, expect, it, afterEach } from "vitest";
import {
  clearTranscriptCache,
  peekTranscriptCache,
  resolveSwitchTranscript,
  writeTranscriptCache,
} from "../components/conversation/transcriptCache";
import {
  deduplicateAssistantNarration,
  getSimilarity,
  mergeTranscriptItems,
  transcriptFingerprint,
  transcriptResponseToItems,
} from "../components/conversation/transcriptItems";
import {
  clearToolPrepPlaceholders,
  finalizeStreamingThinking,
  upsertStreamingThinking,
  upsertToolPrep,
} from "../components/conversation/thinkingToolPrep";
import {
  CHAT_EVENTS_POLL_MS,
  chatFrameToStreamEvent,
  cursorAfterReplayMiss,
  isChatEventReplayMiss,
  isTerminalStreamKind,
  nextAppliedCursor,
  ringGenerationAfterReplayMiss,
  shouldAdvanceReplayCursor,
  shouldArmChatEventsFromRunners,
  shouldHydrateTranscriptOnReplayMiss,
  shouldPollChatEvents,
  shouldRetryRingAfterReplayMiss,
} from "../components/conversation/chatEvents";
import {
  formatWorkspaceOpenLeaseExhaustedMessage,
  isWorkspaceOpenLeaseExhausted,
} from "../components/conversation/leaseExhausted";
import { composerStatusFromRunner } from "../components/conversation/composerStatus";
import {
  SLASH_COMMANDS,
  formatMentionListingCapMessage,
  isBuiltInSlashCommand,
  mergeSlashCommands,
} from "../components/conversation/slashCommands";
import {
  filterTabsAfterDelete,
  normalizeTabPath,
  pathIsUnder,
  remapActiveTabAfterRename,
  remapTabsAfterRename,
} from "../components/conversation/tabPaths";
import {
  appendStreamingTextToItems,
  findStreamingBubbleIdx,
  typewriterCharsPerFrame,
} from "../components/conversation/streamBubbles";
import { derivePillStatus } from "../components/conversation/pillStatus";
import { workspaceLeafName } from "../components/conversation/workspaceDisplay";
import {
  statusPillDotClass,
  statusPillLabel,
  statusPillTextClass,
} from "../components/conversation/StatusPill";
import {
  appendActionStartCard,
  appendAuthFailure,
  appendAutoHalt,
  appendAutoStatus,
  appendCommandApproval,
  appendCommandBlocked,
  applySwarmResultToItems,
  ensureAssistantStreamingBubble,
  failSwarmPendingForActionError,
  finalizeOrphanSwarmPills,
  finalizePilotMessage,
  finalizeStreamingBubbleOnActionResult,
  formatDistilledNotice,
  formatWikiAutoIngestNotice,
  patchCardInItems,
  shouldPaintThinking,
  truncateWaitHint,
  updateCommandApproval,
  workspaceRootFromActionResult,
} from "../components/conversation/streamApply";
import {
  collectDisplayArtifacts,
  emptySessionSwitchState,
  mergeUniqueArtifacts,
  runnerBusySwitchDecision,
  shouldPreserveBusyStatus,
} from "../components/conversation/sessionHydrate";
import {
  classifyLocalSlashCommand,
  composerEnterAction,
  editNoticeAfterSend,
  executeSendGate,
  formatCompactCompleteMessage,
  formatHelpSlashReply,
  shouldBlockEmptySend,
} from "../components/conversation/composerSend";
import {
  appendMentionsToInput,
  buildMentionInsert,
  buildSymbolInsert,
  clampSelectIndex,
  cycleSelectIndex,
  detectComposerTrigger,
  filterSlashCommands,
  mentionTokenForDroppedPath,
} from "../components/conversation/composerInput";
import { moveItem, reorderByDrag } from "../components/conversation/queueOps";
import {
  notifyPrefEnabled,
  queueMessagesPrefEnabled,
  shouldShowCompletionNotification,
  soundPrefEnabled,
} from "../components/conversation/completionNotify";
import {
  closeTabResult,
  otherTabsHaveDirty,
  setTabDirty,
  tabHasDirty,
  upsertOpenTab,
} from "../components/conversation/openFileTabs";
import {
  preserveOrThinking,
  runnersBusyTickDecision,
  userStoppedBusyChrome,
} from "../components/conversation/runnersBusy";
import {
  contextUsagePercent,
  formatTokenK,
  normalizeContextUsage,
} from "../components/conversation/contextUsageColors";
import {
  FEED_SETTLE_STABLE_FRAMES,
  FEED_SETTLE_TIMEOUT_MS,
  isPinnedToBottom,
  pinStateFromScrollGeometry,
  settleFrameResult,
  shouldUnpinOnTouchMove,
  shouldUnpinOnWheel,
} from "../components/conversation/feedScroll";
import {
  STREAM_ABORT_MESSAGE,
  streamOnDoneDecision,
  streamOnErrorDecision,
} from "../components/conversation/streamTerminal";
import {
  appendMemoryProposal,
  classifySwarmPollEvent,
} from "../components/conversation/swarmPoll";
import {
  cancelTypewriterWithoutFlush,
  flushTypewriterBuffer,
  startTypewriterLoop,
} from "../components/conversation/streamTypewriter";
import type { Item } from "../components/TranscriptList";

function msg(role: "user" | "assistant", text: string, streaming = false): Item {
  return { kind: "msg", msg: { role, text, streaming } };
}

describe("transcriptCache module", () => {
  afterEach(() => clearTranscriptCache());

  it("write/peek isolates sessions and copies arrays", () => {
    const rows = [msg("user", "a")];
    writeTranscriptCache("s1", rows);
    rows.push(msg("assistant", "b"));
    expect(peekTranscriptCache("s1")).toEqual([msg("user", "a")]);
    expect(peekTranscriptCache("s2")).toBeUndefined();
  });

  it("resolveSwitchTranscript blanks on miss", () => {
    expect(
      resolveSwitchTranscript({
        nextId: "x",
        cached: undefined,
        priorItems: [msg("user", "leak")],
      }),
    ).toEqual({ items: [], stale: true, blank: false });
  });
});

describe("transcriptItems module", () => {
  it("getSimilarity treats prefix matches as identity", () => {
    expect(getSimilarity("Found the root cause", "Found the root cause here")).toBe(1);
    expect(getSimilarity("", "x")).toBe(0);
  });

  it("deduplicateAssistantNarration keeps longer near-duplicate across cards", () => {
    const items: Item[] = [
      msg("user", "go"),
      msg("assistant", "Found the issue in foo"),
      {
        kind: "card",
        card: {
          id: "c1",
          goal: "read",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
        },
      },
      msg("assistant", "Found the issue in foo.ts and fixed it"),
    ];
    const out = deduplicateAssistantNarration(items);
    const assistants = out.filter((i) => i.kind === "msg" && i.msg.role === "assistant");
    expect(assistants).toHaveLength(1);
    if (assistants[0].kind === "msg") {
      expect(assistants[0].msg.text).toContain("fixed it");
    }
  });

  it("deduplicateAssistantNarration never collapses streaming bubbles", () => {
    const items: Item[] = [
      msg("user", "go"),
      msg("assistant", "hello", true),
      msg("assistant", "hello world", true),
    ];
    expect(deduplicateAssistantNarration(items)).toHaveLength(3);
  });

  it("transcriptFingerprint distinguishes thinking and tool_prep", () => {
    const base: Item[] = [msg("user", "hi")];
    const withThink: Item[] = [
      ...base,
      { kind: "thinking", text: "reason", streaming: true, id: "t1" },
    ];
    const withPrep: Item[] = [...base, { kind: "tool_prep", name: "read_file" }];
    expect(transcriptFingerprint(withThink)).not.toBe(transcriptFingerprint(base));
    expect(transcriptFingerprint(withPrep)).not.toBe(transcriptFingerprint(base));
    expect(transcriptFingerprint(withThink)).not.toBe(transcriptFingerprint(withPrep));
  });

  it("transcriptResponseToItems maps history when display is empty", () => {
    const items = transcriptResponseToItems({
      history: [
        { role: "user", content: "(system note)" },
        { role: "user", content: "hello" },
        { role: "assistant", content: "hi" },
      ],
    });
    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({ kind: "msg", msg: { role: "user", text: "hello" } });
    expect(items[1]).toMatchObject({ kind: "msg", msg: { role: "assistant", text: "hi" } });
  });

  it("transcriptResponseToItems restores pending command_approval display rows", () => {
    const hash = "a".repeat(64);
    const items = transcriptResponseToItems({
      display: [
        { type: "message", role: "user", text: "go" },
        {
          type: "command_approval",
          id: "call-1",
          command: "ssh prod reboot",
          command_hash: hash,
          session_id: "session-a",
          workspace_root: "/workspace/a",
          category: "remote",
          reason: "ssh",
          matched: "ssh",
          status: "pending",
        },
      ],
    });
    expect(items).toHaveLength(2);
    expect(items[1]).toMatchObject({
      kind: "command_approval",
      commandHash: hash,
      status: "pending",
      sessionId: "session-a",
      workspaceRoot: "/workspace/a",
    });
  });

  it("transcriptResponseToItems skips malformed/empty approval hashes then keeps a later valid card", () => {
    const validHash = "c".repeat(64);
    const items = transcriptResponseToItems({
      display: [
        { type: "message", role: "user", text: "go" },
        {
          type: "command_approval",
          id: "bad-empty",
          command: "echo hello",
          command_hash: "",
          session_id: "session-a",
          workspace_root: "/workspace/a",
          status: "pending",
        },
        {
          type: "command_approval",
          id: "bad-shape",
          command: "echo hello",
          command_hash: "not-a-hash",
          session_id: "session-a",
          workspace_root: "/workspace/a",
          status: "pending",
        },
        {
          type: "command_approval",
          id: "call-good",
          command: "ssh prod reboot",
          command_hash: validHash,
          session_id: "session-a",
          workspace_root: "/workspace/a",
          category: "remote",
          reason: "ssh",
          matched: "ssh",
          status: "pending",
        },
      ],
    });
    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({ kind: "msg", msg: { role: "user", text: "go" } });
    expect(items[1]).toMatchObject({
      kind: "command_approval",
      commandHash: validHash,
      status: "pending",
      id: "call-good",
    });
  });

  it("mergeTranscriptItems keeps pending approval on equal-card-count remote hydrate", () => {
    const hash = "b".repeat(64);
    const local: Item[] = [
      msg("user", "go"),
      {
        kind: "card",
        card: {
          id: "a1",
          goal: "run",
          cwd: null,
          kind: "run_command",
          running: false,
          open: false,
          result: { adapter: "local" },
        },
      },
      {
        kind: "command_approval",
        id: "call-9",
        command: "rm -rf /",
        commandHash: hash,
        sessionId: "session-a",
        workspaceRoot: "/workspace/a",
        category: "destructive",
        reason: "rm -rf",
        matched: "rm -rf",
        status: "pending",
      },
    ];
    const remote: Item[] = [
      msg("user", "go"),
      {
        kind: "card",
        card: {
          id: "a1",
          goal: "run",
          cwd: null,
          kind: "run_command",
          running: false,
          open: false,
          result: { adapter: "local", duration_ms: 9 },
        },
      },
    ];
    // Equal tool-card counts take the remote path — approval must still survive.
    const merged = mergeTranscriptItems(local, remote);
    expect(
      merged.some((i) => i.kind === "command_approval" && i.commandHash === hash && i.status === "pending"),
    ).toBe(true);
    const card = merged.find((i) => i.kind === "card") as Extract<Item, { kind: "card" }>;
    expect(card.card.result?.duration_ms).toBe(9);
  });

  it("mergeTranscriptItems keeps extra local cards and appends remote-only ones", () => {
    const local: Item[] = [
      {
        kind: "card",
        card: {
          id: "a",
          goal: "one",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
        },
      },
      {
        kind: "card",
        card: {
          id: "b",
          goal: "two",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
        },
      },
      {
        kind: "card",
        card: {
          id: "d",
          goal: "four",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
        },
      },
    ];
    const remote: Item[] = [
      {
        kind: "card",
        card: {
          id: "a",
          goal: "one",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
        },
      },
      {
        kind: "card",
        card: {
          id: "c",
          goal: "three",
          cwd: null,
          kind: "write_file",
          running: false,
          open: false,
        },
      },
    ];
    // local has more cards -> prefer-local merge path
    const merged = mergeTranscriptItems(local, remote);
    const ids = merged
      .filter((i): i is Extract<Item, { kind: "card" }> => i.kind === "card")
      .map((i) => i.card.id);
    expect(ids).toEqual(["a", "b", "d", "c"]);
  });
});

describe("thinkingToolPrep module", () => {
  it("upsertToolPrep accumulates distinct call ids and clear removes placeholders", () => {
    let items: Item[] = [msg("user", "go")];
    items = upsertToolPrep(items, "Read", { id: "call-1", goal: "a.ts" });
    items = upsertToolPrep(items, "Read", { id: "call-2", goal: "b.ts" });
    const cards = items.filter((i) => i.kind === "card");
    expect(cards).toHaveLength(2);
    expect(clearToolPrepPlaceholders(items).every((i) => i.kind !== "tool_prep")).toBe(true);
    expect(clearToolPrepPlaceholders(items).every((i) => i.kind !== "card")).toBe(true);
  });

  it("finalizeStreamingThinking drops streaming flag but keeps id", () => {
    const live = upsertStreamingThinking([], "think");
    const id = (live[0] as Extract<Item, { kind: "thinking" }>).id;
    const done = finalizeStreamingThinking(live);
    expect((done[0] as Extract<Item, { kind: "thinking" }>).streaming).toBeFalsy();
    expect((done[0] as Extract<Item, { kind: "thinking" }>).id).toBe(id);
  });
});

describe("chatEvents module", () => {
  it("exports the reattach poll interval", () => {
    expect(CHAT_EVENTS_POLL_MS).toBe(1000);
  });

  it("classifies available:false as miss without advancing cursor", () => {
    const replay = { available: false, ok: true, missed: false };
    expect(isChatEventReplayMiss(replay)).toBe(true);
    expect(shouldAdvanceReplayCursor(replay)).toBe(false);
    expect(shouldHydrateTranscriptOnReplayMiss(replay)).toBe(true);
  });

  it("nextAppliedCursor prefers the highest frame or replay cursor", () => {
    expect(nextAppliedCursor(1, [{ cursor: 2 }, { cursor: 4 }], 3)).toBe(4);
    expect(nextAppliedCursor(5, [{ cursor: 2 }], 6)).toBe(6);
  });

  it("maps frames and recognizes terminals", () => {
    expect(chatFrameToStreamEvent({ kind: "done", data: { ok: 1 } })).toEqual({
      kind: "done",
      data: { ok: 1 },
    });
    expect(isTerminalStreamKind("error")).toBe(true);
  });

  it("gates poll and runner arming", () => {
    expect(
      shouldPollChatEvents({
        detachedBusy: true,
        localStreamActive: false,
        userStopped: false,
        sawTerminal: false,
      }),
    ).toBe(true);
    expect(
      shouldArmChatEventsFromRunners({
        runnerBusy: true,
        localStreamActive: false,
        userStopped: false,
        chatEventsPollArmed: false,
      }),
    ).toBe(true);
  });

  it("resets cursor on ring_miss and keeps generation otherwise", () => {
    expect(cursorAfterReplayMiss({ code: "ring_miss" }, 9)).toBe(0);
    expect(ringGenerationAfterReplayMiss({ code: "cursor_gap" }, 2)).toBe(2);
  });

  it("retries ring only for cursor_gap / refreshed generation_mismatch", () => {
    expect(shouldRetryRingAfterReplayMiss(
      { code: "cursor_gap" },
      { alreadyRetried: false },
    )).toBe(true);
    expect(shouldRetryRingAfterReplayMiss(
      { code: "ring_miss" },
      { alreadyRetried: false },
    )).toBe(false);
    expect(shouldRetryRingAfterReplayMiss(
      { code: "generation_mismatch" },
      { alreadyRetried: false, prevGeneration: 1, nextGeneration: 2 },
    )).toBe(true);
  });
});

describe("leaseExhausted + composerStatus modules", () => {
  it("formats capacity-only lease copy", () => {
    expect(
      formatWorkspaceOpenLeaseExhaustedMessage({
        code: "lease_exhausted",
        max_concurrent: 2,
        active_count: 2,
      }),
    ).toMatch(/2\/2/);
    expect(isWorkspaceOpenLeaseExhausted({ code: "lease_exhausted" })).toBe(true);
  });

  it("composerStatusFromRunner ignores attaching cold builds", () => {
    expect(composerStatusFromRunner("s", { s: "attaching" }, false)).toBe("idle");
    expect(composerStatusFromRunner(null, { s: "running" }, false)).toBeNull();
  });
});

describe("slashCommands + mention listing", () => {
  it("merges custom commands and recognizes built-ins", () => {
    expect(SLASH_COMMANDS.some((s) => s.cmd === "/clear")).toBe(true);
    expect(isBuiltInSlashCommand("/clear")).toBe(true);
    expect(isBuiltInSlashCommand("/custom")).toBe(false);
    const merged = mergeSlashCommands([{ name: "ship", description: "Ship it", scope: "user" }]);
    expect(merged).toContainEqual({ cmd: "/ship", desc: "Ship it (custom)" });
  });

  it("formats mention listing caps", () => {
    expect(formatMentionListingCapMessage({ total: 5000, capped: 1000 })).toMatch(/Showing .+ of .+/);
    expect(formatMentionListingCapMessage({ capped: 2000 })).toMatch(/capped at/i);
    expect(formatMentionListingCapMessage({})).toMatch(/capped/i);
  });
});

describe("tabPaths module", () => {
  it("normalizes separators and nest checks", () => {
    expect(normalizeTabPath("a\\b\\c")).toBe("a/b/c");
    expect(pathIsUnder("repo/src/a.ts", "repo/src")).toBe(true);
    expect(pathIsUnder("repo/other", "repo/src")).toBe(false);
  });

  it("filters deletes and remaps renames including nested paths", () => {
    const tabs = [
      { path: "src/a.ts", isDirty: false },
      { path: "src/nested/b.ts", isDirty: true },
      { path: "keep.ts", isDirty: false },
    ];
    expect(filterTabsAfterDelete(tabs, "src").map((t) => t.path)).toEqual(["keep.ts"]);
    const renamed = remapTabsAfterRename(tabs, "src", "lib");
    expect(renamed.map((t) => t.path)).toEqual(["lib/a.ts", "lib/nested/b.ts", "keep.ts"]);
    expect(remapActiveTabAfterRename("src/nested/b.ts", "src", "lib")).toBe("lib/nested/b.ts");
    expect(remapActiveTabAfterRename("src", "src", "lib")).toBe("lib");
    expect(remapActiveTabAfterRename("chat", "src", "lib")).toBe("chat");
  });
});

describe("streamBubbles module", () => {
  it("finds streaming bubble past decoration and appends text", () => {
    const items: Item[] = [
      msg("user", "go"),
      { kind: "msg", msg: { role: "assistant", text: "hi", streaming: true } },
      { kind: "thinking", text: "reason", streaming: true, id: "t1" },
      {
        kind: "card",
        card: { id: "c1", goal: "read", cwd: null, kind: "read_file", running: true, open: false },
      },
    ];
    expect(findStreamingBubbleIdx(items)).toBe(1);
    const next = appendStreamingTextToItems(items, " there");
    expect((next[1] as Extract<Item, { kind: "msg" }>).msg.text).toBe("hi there");
  });

  it("skips workerStream bubbles when asked and scales typewriter drain", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "assistant", text: "w", streaming: true, workerStream: true } },
    ];
    expect(findStreamingBubbleIdx(items, { excludeWorkerStream: true })).toBe(-1);
    expect(typewriterCharsPerFrame(0, false)).toBe(0);
    expect(typewriterCharsPerFrame(3, false)).toBe(3);
    expect(typewriterCharsPerFrame(40, true)).toBeGreaterThanOrEqual(12);
  });
});

describe("pillStatus + workspaceDisplay + StatusPill chrome", () => {
  it("derivePillStatus prefers investigation and open-turn over idle flaps", () => {
    expect(
      derivePillStatus({
        transcriptStale: true,
        answerChromeIdle: false,
        liveInvestigation: false,
        turnOpen: false,
        status: "idle",
      }),
    ).toBe("switching…");
    expect(
      derivePillStatus({
        transcriptStale: false,
        answerChromeIdle: true,
        liveInvestigation: false,
        turnOpen: false,
        status: "thinking",
      }),
    ).toBe("idle");
    expect(
      derivePillStatus({
        transcriptStale: false,
        answerChromeIdle: false,
        liveInvestigation: true,
        turnOpen: true,
        status: "idle",
      }),
    ).toBe("executing");
    expect(
      derivePillStatus({
        transcriptStale: false,
        answerChromeIdle: false,
        liveInvestigation: false,
        turnOpen: true,
        status: "done",
      }),
    ).toBe("thinking");
  });

  it("workspaceLeafName and StatusPill helpers stay stable", () => {
    expect(workspaceLeafName("C:\\Users\\me\\proj", undefined)).toBe("proj");
    expect(workspaceLeafName("C:\\Users\\me\\.pmharness\\home", "C:\\Users\\me\\.pmharness\\home")).toBe("Home");
    expect(statusPillLabel("thinking", "read_file")).toBe("read_file");
    expect(statusPillLabel("idle", "x")).toBe("idle");
    expect(statusPillTextClass("error")).toContain("risk");
    expect(statusPillDotClass("streaming")).toContain("animate-pulse");
  });
});

describe("streamApply module", () => {
  it("patches cards and dedupes auth_failure banners", () => {
    const items: Item[] = [
      {
        kind: "card",
        card: {
          id: "c1",
          goal: "read",
          cwd: null,
          kind: "read_file",
          running: true,
          open: false,
        },
      },
    ];
    const patched = patchCardInItems(items, "c1", { running: false, open: false });
    expect((patched[0] as Extract<Item, { kind: "card" }>).card.running).toBe(false);
    const once = appendAuthFailure(items, "bad key", "c1");
    const twice = appendAuthFailure(once, "bad key", "c1");
    expect(twice.filter((i) => i.kind === "auth_failure")).toHaveLength(1);
  });

  it("ensures bubbles, finalizes pilot message, and drops worker preview", () => {
    const withBubble = ensureAssistantStreamingBubble([], { isPlan: true });
    expect(withBubble).toHaveLength(1);
    expect((withBubble[0] as Extract<Item, { kind: "msg" }>).msg.streaming).toBe(true);

    const workerThenPilot: Item[] = [
      {
        kind: "msg",
        msg: { role: "assistant", text: "w", streaming: true, workerStream: true },
      },
    ];
    const finalized = finalizePilotMessage(workerThenPilot, "answer");
    expect(finalized).toHaveLength(1);
    expect((finalized[0] as Extract<Item, { kind: "msg" }>).msg.text).toBe("answer");
    expect((finalized[0] as Extract<Item, { kind: "msg" }>).msg.streaming).toBeFalsy();

    const dropped = finalizeStreamingBubbleOnActionResult([
      {
        kind: "msg",
        msg: { role: "assistant", text: "tmp", streaming: true, workerStream: true },
      },
    ]);
    expect(dropped).toHaveLength(0);
  });

  it("action_start is idempotent and swarm_result resolves pending chips", () => {
    let items = appendActionStartCard([], { id: "a1", goal: "g", kind: "read_file" });
    items = appendActionStartCard(items, { id: "a1", goal: "g", kind: "read_file" });
    expect(items.filter((i) => i.kind === "card")).toHaveLength(1);

    items = [
      {
        kind: "swarm_pending",
        job_ids: ["j1"],
        objective: "ship",
        resolved: false,
        status: "running",
        terminal_job_ids: [],
      },
    ];
    const next = applySwarmResultToItems(items, {
      job_id: "j1",
      applied: true,
      files: ["a.ts"],
      summary: "done",
      error: null,
    });
    expect(next[0]).toMatchObject({ kind: "swarm_pending", resolved: true, status: "done" });
    expect(next[1]).toMatchObject({ kind: "swarm_result", job_id: "j1", objective: "ship" });
  });

  it("swarm_result failure flips the pending pill to failed (no spinner)", () => {
    const items: Item[] = [
      {
        kind: "swarm_pending",
        job_ids: ["local-swarm-a1"],
        objective: "audit auth",
        status: "running",
        terminal_job_ids: [],
      },
    ];
    // Substrate job id differs from the local-swarm pending id — still match via objective.
    const next = applySwarmResultToItems(items, {
      job_id: "job_deadbeef1234",
      objective: "audit auth",
      applied: false,
      files: [],
      summary: "no artifacts",
      error: "swarm produced no artifacts",
    });
    expect(next[0]).toMatchObject({
      kind: "swarm_pending",
      status: "failed",
      resolved: true,
    });
    expect(next[1]).toMatchObject({
      kind: "swarm_result",
      applied: false,
      job_id: "job_deadbeef1234",
    });
  });

  it("run_parallel pill waits for all jobs and fails if any failed", () => {
    let items: Item[] = [
      {
        kind: "swarm_pending",
        job_ids: ["local-a", "local-b"],
        objective: "Parallel wave",
        status: "running",
        terminal_job_ids: [],
      },
    ];
    items = applySwarmResultToItems(items, {
      job_id: "local-a",
      applied: true,
      files: [],
      summary: "ok",
      error: null,
    });
    expect(items[0]).toMatchObject({
      kind: "swarm_pending",
      status: "running",
      terminal_job_ids: ["local-a"],
    });

    items = applySwarmResultToItems(items, {
      job_id: "local-b",
      applied: false,
      files: [],
      summary: "boom",
      error: "PATCH DID NOT APPLY",
    });
    expect(items[0]).toMatchObject({
      kind: "swarm_pending",
      status: "failed",
      resolved: true,
    });
  });

  it("finalizeOrphanSwarmPills ends spinning pills with no live tracker entry", () => {
    const items: Item[] = [
      {
        kind: "swarm_pending",
        job_ids: ["local-swarm-a9"],
        objective: "stuck",
        status: "running",
        terminal_job_ids: [],
      },
      {
        kind: "swarm_pending",
        job_ids: ["job_alive"],
        objective: "background",
        status: "running",
        terminal_job_ids: [],
      },
    ];
    const next = finalizeOrphanSwarmPills(items, ["job_alive"]);
    expect(next[0]).toMatchObject({ status: "ended", resolved: true });
    expect(next[1]).toMatchObject({ status: "running" });
  });

  it("failSwarmPendingForActionError marks local-swarm pill failed", () => {
    const items: Item[] = [
      {
        kind: "swarm_pending",
        job_ids: ["local-swarm-a3"],
        objective: "sync fail",
        status: "running",
        terminal_job_ids: [],
      },
    ];
    const next = failSwarmPendingForActionError(items, "a3");
    expect(next[0]).toMatchObject({ status: "failed", resolved: true });
  });

  it("formats notices and wait hints", () => {
    expect(formatDistilledNotice({ skill: { status: "skipped" } })).toBeNull();
    expect(
      formatDistilledNotice({ skill: { status: "proposed", name: "foo" } }),
    ).toMatch(/proposed 1 skill/);
    expect(formatWikiAutoIngestNotice(1)).toMatch(/1 page/);
    expect(formatWikiAutoIngestNotice(2)).toMatch(/2 pages/);
    expect(truncateWaitHint("")).toBeNull();
    expect(truncateWaitHint("x".repeat(80))?.endsWith("…")).toBe(true);
    expect(shouldPaintThinking({ text: "  ", delta: false }).painting).toBe(false);
    expect(shouldPaintThinking({ text: "a", delta: true }).painting).toBe(true);
    expect(workspaceRootFromActionResult({ path: "/repo" }, "(workspace root)")).toBe("/repo");
    expect(appendCommandBlocked([], { command: "rm" })[0].kind).toBe("command_blocked");
    const approvals = appendCommandApproval([], {
      id: "call-1",
      command: "ssh prod reboot",
      command_hash: "a".repeat(64),
      session_id: "session-a",
      workspace_root: "/workspace/a",
    });
    expect(approvals[0]).toMatchObject({
      kind: "command_approval",
      status: "pending",
      sessionId: "session-a",
    });
    expect(appendCommandApproval(approvals, {
      command_hash: "a".repeat(64),
    })).toBe(approvals);
    expect(updateCommandApproval(
      approvals,
      "a".repeat(64),
      { status: "rejected" },
    )[0]).toMatchObject({ status: "rejected" });
    const statusItems = appendAutoStatus([], 1, { swarms_used: 0, max_swarms: 5 });
    expect(appendAutoStatus(statusItems, 2, { swarms_used: 1, max_swarms: 5 })).toHaveLength(1);
    expect(appendAutoHalt([], "cancelled", { swarms_used: 0, max_swarms: 5 })[0]).toMatchObject({
      kind: "auto_halt",
      reason: "cancelled",
    });
  });

  it("appendCommandApproval ignores malformed/empty hashes without poisoning dedupe", () => {
    const validHash = "b".repeat(64);
    const afterEmpty = appendCommandApproval([], {
      id: "bad-empty",
      command: "echo hello",
      command_hash: "",
      session_id: "session-a",
      workspace_root: "/workspace/a",
    });
    expect(afterEmpty).toEqual([]);

    const afterMalformed = appendCommandApproval(afterEmpty, {
      id: "bad-shape",
      command: "echo hello",
      command_hash: "not-a-hash",
      session_id: "session-a",
      workspace_root: "/workspace/a",
    });
    expect(afterMalformed).toEqual([]);

    const withValid = appendCommandApproval(afterMalformed, {
      id: "call-good",
      command: "ssh prod reboot",
      command_hash: validHash,
      session_id: "session-a",
      workspace_root: "/workspace/a",
    });
    expect(withValid).toHaveLength(1);
    expect(withValid[0]).toMatchObject({
      kind: "command_approval",
      commandHash: validHash,
      status: "pending",
    });

    // A later empty/malformed event must not suppress or replace the valid card.
    expect(appendCommandApproval(withValid, {
      command_hash: "",
      command: "rm -rf /",
    })).toBe(withValid);
    expect(appendCommandApproval(withValid, {
      command_hash: "zzz",
      command: "rm -rf /",
    })).toBe(withValid);
  });
});

describe("sessionHydrate module", () => {
  it("collects and merges artifacts; empty-session switch keeps prior rows", () => {
    const display = [
      {
        type: "card",
        result: {
          artifacts: [
            { type: "diff", headline: "a" },
            { type: "diff", headline: "a" },
            { type: "note", headline: "b" },
          ],
        },
      },
    ];
    const collected = collectDisplayArtifacts(display);
    // collect mirrors display walk (no dedupe); mergeUniqueArtifacts dedupes.
    expect(collected).toHaveLength(3);
    expect(mergeUniqueArtifacts(collected, [{ type: "note", headline: "b" }])).toHaveLength(2);
    expect(emptySessionSwitchState(0)).toEqual({ clearItems: true, stale: false });
    expect(emptySessionSwitchState(3)).toEqual({ clearItems: false, stale: true });
  });

  it("runner busy switch decisions preserve chrome rules", () => {
    expect(shouldPreserveBusyStatus("executing")).toBe(true);
    expect(shouldPreserveBusyStatus("idle")).toBe(false);
    expect(
      runnerBusySwitchDecision({
        runnerState: "running",
        localStreamActive: false,
        switchedSession: true,
      }).kind,
    ).toBe("busy");
    expect(
      runnerBusySwitchDecision({
        runnerState: "idle",
        localStreamActive: false,
        switchedSession: true,
      }).kind,
    ).toBe("idle");
    expect(
      runnerBusySwitchDecision({
        runnerState: "running",
        localStreamActive: true,
        switchedSession: true,
      }).kind,
    ).toBe("noop");
  });
});

describe("composerSend module", () => {
  it("gates enter/send and formats slash replies", () => {
    expect(composerEnterAction({ busy: true, metaOrCtrl: true })).toBe("queue");
    expect(composerEnterAction({ busy: true, metaOrCtrl: false })).toBe("send");
    expect(
      executeSendGate({ transcriptStale: true, resume: false, userStopped: false }),
    ).toBe("stale");
    expect(
      executeSendGate({ transcriptStale: false, resume: true, userStopped: true }),
    ).toBe("stopped_resume");
    expect(shouldBlockEmptySend({ transcriptStale: false, text: "  ", imageCount: 0 })).toBe(true);
    expect(shouldBlockEmptySend({ transcriptStale: false, text: "", imageCount: 1 })).toBe(false);
    expect(formatHelpSlashReply([{ cmd: "/help", desc: "Help" }])).toMatch(/\/help/);
    expect(formatCompactCompleteMessage(10, 4)).toMatch(/10 -> 4/);
    expect(editNoticeAfterSend(true)).toMatch(/Revert/);
    expect(editNoticeAfterSend(false)).toBeNull();
  });

  it("classifies local slash commands", () => {
    const builtIn = (cmd: string) => ["/clear", "/help", "/compact", "/model", "/new"].includes(cmd);
    expect(
      classifyLocalSlashCommand({ message: "/clear", isBuiltIn: builtIn, customNames: [] }).kind,
    ).toBe("clear_or_new");
    expect(
      classifyLocalSlashCommand({ message: "/help", isBuiltIn: builtIn, customNames: [] }).kind,
    ).toBe("help");
    expect(
      classifyLocalSlashCommand({
        message: "/ship it",
        isBuiltIn: builtIn,
        customNames: ["ship"],
      }),
    ).toEqual({ kind: "custom", name: "ship", args: "it" });
    expect(
      classifyLocalSlashCommand({ message: "hello", isBuiltIn: builtIn, customNames: [] }).kind,
    ).toBe("none");
  });
});

describe("composerInput module", () => {
  it("detects slash and mention triggers", () => {
    expect(detectComposerTrigger("/he", 3)).toEqual({ kind: "slash", query: "he" });
    expect(detectComposerTrigger("see @src/a", 10)).toEqual({
      kind: "mention",
      query: "src/a",
      atIndex: 4,
    });
    expect(detectComposerTrigger("plain", 5).kind).toBe("none");
  });

  it("builds inserts, cycles selection, and resolves drop mentions", () => {
    expect(buildMentionInsert("hi @", 3, 4, "a.ts")).toEqual({
      next: "hi @a.ts ",
      cursor: 9,
    });
    expect(buildSymbolInsert("@", 0, 1, "Foo").next).toContain("@symbol:Foo");
    expect(filterSlashCommands([{ cmd: "/help" }, { cmd: "/clear" }], "he")).toEqual([
      { cmd: "/help" },
    ]);
    expect(cycleSelectIndex(0, 1, 3)).toBe(1);
    expect(cycleSelectIndex(0, -1, 3)).toBe(2);
    expect(clampSelectIndex(9, 3)).toBe(2);
    expect(
      mentionTokenForDroppedPath({ osPath: "/repo/a.ts", repo: "/repo" }),
    ).toBe("@a.ts");
    expect(
      mentionTokenForDroppedPath({ osPath: "/repo/a b.ts", repo: "/repo" }),
    ).toBeNull();
    expect(
      mentionTokenForDroppedPath({
        osPath: "",
        repo: "/repo",
        uploadedPath: "/repo/uploads/x.ts",
      }),
    ).toBe("@uploads/x.ts");
    expect(appendMentionsToInput("hi", ["@a", "@b"])).toBe("hi @a @b ");
  });
});

describe("queueOps / openFileTabs / runnersBusy", () => {
  it("reorders queues and upserts editor tabs", () => {
    expect(moveItem(["a", "b", "c"], 0, "down")).toEqual(["b", "a", "c"]);
    expect(reorderByDrag(["a", "b", "c"], 2, 0)).toEqual(["c", "a", "b"]);
    expect(upsertOpenTab([], "a.ts", 1, 2)).toEqual([
      { path: "a.ts", isDirty: false, line: 1, col: 2 },
    ]);
    expect(closeTabResult([{ path: "a.ts", isDirty: false }], "a.ts", "a.ts")).toEqual({
      tabs: [],
      activeTab: "chat",
    });
    expect(tabHasDirty([{ path: "a.ts", isDirty: true }], "a.ts")).toBe(true);
    expect(otherTabsHaveDirty([{ path: "a.ts", isDirty: true }, { path: "b.ts", isDirty: false }], "b.ts")).toBe(true);
    expect(setTabDirty([{ path: "a.ts", isDirty: false }], "a.ts", true)[0].isDirty).toBe(true);
    expect(userStoppedBusyChrome("thinking")).toBe("idle");
    expect(preserveOrThinking("idle")).toBe("thinking");
    expect(
      runnersBusyTickDecision({
        userStopped: false,
        localStreamActive: false,
        runnerBusy: true,
        detachedBusy: true,
        chatEventsPollArmed: false,
        items: [],
      }).kind,
    ).toBe("arm_reattach");
  });
});

describe("completionNotify / feedScroll / streamTerminal / swarmPoll", () => {
  it("reads prefs and scroll/terminal decisions", () => {
    const store: Record<string, string> = {};
    const getItem = (k: string) => store[k] ?? null;
    expect(notifyPrefEnabled(getItem)).toBe(true);
    expect(soundPrefEnabled(getItem)).toBe(false);
    expect(queueMessagesPrefEnabled(getItem)).toBe(true);
    store["pmharness.notify"] = "false";
    expect(notifyPrefEnabled(getItem)).toBe(false);
    expect(
      shouldShowCompletionNotification({ notifyEnabled: true, isHidden: true }),
    ).toBe(true);
    expect(isPinnedToBottom(1000, 900, 50)).toBe(true);
    expect(shouldUnpinOnWheel(-1, false)).toBe(true);
    expect(shouldUnpinOnTouchMove(10, 20, false)).toBe(true);
    expect(
      settleFrameResult({ height: 10, lastHeight: 10, stableFrames: FEED_SETTLE_STABLE_FRAMES - 1, frame: 1 }).done,
    ).toBe(true);
    // Settle loop must bail on wall-clock even while height keeps growing (stream).
    expect(
      settleFrameResult({
        height: 200,
        lastHeight: 100,
        stableFrames: 0,
        frame: 3,
        startedAtMs: 0,
        nowMs: FEED_SETTLE_TIMEOUT_MS - 1,
      }).done,
    ).toBe(false);
    {
      let height = 100;
      let lastHeight = 0;
      let stableFrames = 0;
      let frame = 0;
      let done = false;
      const startedAtMs = 0;
      for (let t = 0; !done && t < 5000; t += 16) {
        height += 10;
        const step = settleFrameResult({
          height,
          lastHeight,
          stableFrames,
          frame,
          startedAtMs,
          nowMs: t,
        });
        lastHeight = height;
        stableFrames = step.stableFrames;
        frame = step.frame;
        done = step.done;
      }
      expect(done).toBe(true);
      // Timed out near the wall-clock cap, not via the 90-frame fallback.
      expect(frame).toBeLessThanOrEqual(Math.ceil(FEED_SETTLE_TIMEOUT_MS / 16) + 2);
    }
    // onScroll during settling recomputes pin from geometry (scrolled-up unpins).
    expect(
      pinStateFromScrollGeometry(2000, 0, 400, true),
    ).toBe(false);
    expect(
      pinStateFromScrollGeometry(2000, 1600, 400, true),
    ).toBe(true);
    expect(streamOnDoneDecision({ turnSettled: false, userStopped: false }).kind).toBe("abort_error");
    expect(streamOnErrorDecision({ turnSettled: true, userStopped: false }).kind).toBe(
      "preserve_error_or_done",
    );
    expect(STREAM_ABORT_MESSAGE).toMatch(/aborted/);
    expect(contextUsagePercent(50, 100)).toBe(50);
    expect(formatTokenK(1500)).toBe("1.5");
    expect(classifySwarmPollEvent({ kind: "pilot_resume" }).kind).toBe("pilot_resume");
    expect(appendMemoryProposal([], { id: "1", text: "t", category: "g" })).toHaveLength(1);
    expect(appendMemoryProposal([{ id: "1", text: "t", category: "g" }], { id: "1", text: "t", category: "g" })).toHaveLength(1);
  });

  it("keeps context-usage display helpers finite on malformed inputs", () => {
    expect(contextUsagePercent(NaN, 100)).toBe(0);
    expect(contextUsagePercent(50, NaN)).toBe(0);
    expect(contextUsagePercent(Infinity, 100)).toBe(0);
    expect(contextUsagePercent(50, 0)).toBe(0);
    expect(contextUsagePercent(-10, 100)).toBe(0);
    expect(contextUsagePercent(250, 100)).toBe(100);

    expect(formatTokenK(NaN)).toBe("0.0");
    expect(formatTokenK(Infinity)).toBe("0.0");
    expect(formatTokenK(NaN, 0)).toBe("0");
    expect(formatTokenK(2500)).toBe("2.5");
  });

  it("accepts only well-formed context-usage payloads in normalizeContextUsage", () => {
    const valid = {
      total: 1200,
      limit: 200000,
      categories: [
        { name: "System prompt", tokens: 800 },
        { name: "Conversation", tokens: 400 },
      ],
      spill_count: 2,
    };
    // Valid payloads pass through unchanged, extra fields included.
    expect(normalizeContextUsage(valid)).toEqual(valid);
    expect(normalizeContextUsage({ total: 0, limit: 1, categories: [] })).toEqual({
      total: 0,
      limit: 1,
      categories: [],
    });

    expect(normalizeContextUsage(null)).toBeNull();
    expect(normalizeContextUsage(undefined)).toBeNull();
    expect(normalizeContextUsage("nope")).toBeNull();
    expect(normalizeContextUsage({})).toBeNull();
    // Missing categories array (fresh-session partial payload).
    expect(normalizeContextUsage({ total: 100, limit: 1000 })).toBeNull();
    // Non-finite / negative totals and limits.
    expect(normalizeContextUsage({ total: NaN, limit: 1000, categories: [] })).toBeNull();
    expect(normalizeContextUsage({ total: 100, limit: NaN, categories: [] })).toBeNull();
    expect(normalizeContextUsage({ total: -1, limit: 1000, categories: [] })).toBeNull();
    expect(normalizeContextUsage({ total: 100, limit: 0, categories: [] })).toBeNull();
    expect(normalizeContextUsage({ total: 100, limit: Infinity, categories: [] })).toBeNull();
    // Malformed category entries.
    expect(
      normalizeContextUsage({ total: 100, limit: 1000, categories: [{ name: "", tokens: 5 }] }),
    ).toBeNull();
    expect(
      normalizeContextUsage({ total: 100, limit: 1000, categories: [{ name: "Rules", tokens: NaN }] }),
    ).toBeNull();
    expect(
      normalizeContextUsage({ total: 100, limit: 1000, categories: [{ name: "Rules", tokens: -3 }] }),
    ).toBeNull();
    expect(
      normalizeContextUsage({ total: 100, limit: 1000, categories: [null] }),
    ).toBeNull();
  });

  it("drives typewriter flush/cancel helpers", () => {
    const refs = {
      typeBufRef: { current: "hello" },
      typeRafRef: { current: 7 as number | null },
      typeDoneRef: { current: false },
    };
    const chunks: string[] = [];
    flushTypewriterBuffer(refs, (c) => chunks.push(c), () => {});
    expect(chunks).toEqual(["hello"]);
    expect(refs.typeBufRef.current).toBe("");
    expect(refs.typeDoneRef.current).toBe(true);

    refs.typeBufRef.current = "x";
    refs.typeRafRef.current = 1;
    refs.typeDoneRef.current = true;
    cancelTypewriterWithoutFlush(refs, () => {});
    expect(refs.typeBufRef.current).toBe("");
    expect(refs.typeDoneRef.current).toBe(false);

    let scheduled = 0;
    startTypewriterLoop(
      {
        typeBufRef: { current: "" },
        typeRafRef: { current: null },
        typeDoneRef: { current: false },
      },
      () => {},
      () => {
        scheduled += 1;
        return 1;
      },
    );
    expect(scheduled).toBe(1);
  });
});
