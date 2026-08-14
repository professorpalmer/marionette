import { describe, expect, it, afterEach, vi } from "vitest";
import {
  clearTranscriptCache,
  peekTranscriptCache,
  resolveSwitchTranscript,
  writeTranscriptCache,
} from "../components/conversation/transcriptCache";
import {
  deduplicateAssistantNarration,
  dedupeDisplayItems,
  getSimilarity,
  mergeTranscriptItems,
  transcriptFingerprint,
  transcriptResponseToItems,
} from "../components/conversation/transcriptItems";
import {
  clearToolPrepPlaceholders,
  coalesceThinkingChunk,
  finalizeStreamingThinking,
  hoistCardsBeforeTrailingFinals,
  newThinkingId,
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
import { isAgentLoopOpen } from "../components/conversation/runnersBusy";
import { workspaceLeafName } from "../components/conversation/workspaceDisplay";
import {
  statusPillClickable,
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
  appendPendingReview,
  appendSwarmPending,
  applyActionResultCard,
  applySwarmResultToItems,
  focusReviewTabAndRefresh,
  swarmResultOutcome,
  ensureAssistantStreamingBubble,
  ensureWorkerStreamingBubble,
  failSwarmPendingForActionError,
  finalizeOrphanSwarmPills,
  finalizePilotMessage,
  finalizeStreamingBubbleOnActionResult,
  formatDistilledNotice,
  formatWikiAutoIngestNotice,
  foldSwarmLiveJobsAfterReload,
  mergeJobActionsIntoItems,
  reconcileOrphanInvestigationCards,
  reconcileTerminalJobCards,
  sealOpenStreamSurfaces,
  shouldApplySwarmLiveMerge,
  appendCompaction,
  appendStopHonestyNotice,
  compactionAbortLabel,
  noticeIsStopHonesty,
  noticeShowsWaitHint,
  patchCardInItems,
  shouldPaintThinking,
  truncateWaitHint,
  updateCommandApproval,
  workspaceRootFromActionResult,
  MAX_JOB_ACTIONS,
  MAX_ACTION_GOAL_CHARS,
  boundActionField,
  isTerminalJobStatus,
} from "../components/conversation/streamApply";
import {
  cacheHitEmptyTranscriptDecision,
  collectDisplayArtifacts,
  emptySessionSwitchState,
  emptyTranscriptAfterRetryDecision,
  mergeUniqueArtifacts,
  reattachSessionStateFailureDecision,
  clearRecoveredSessionFailNotice,
  runnerBusySwitchDecision,
  SESSION_STATE_FAIL_NOTICE,
  SESSION_TRANSCRIPT_FAIL_NOTICE,
  sessionStateFailureSwitchDecision,
  shouldPreserveBusyStatus,
  shouldResetBusyChromeOnSwitch,
  shouldRetryEmptyTranscript,
  transcriptRefreshFailureDecision,
} from "../components/conversation/sessionHydrate";
import {
  clearComposerDraftCache,
  peekComposerDraft,
  resolveComposerDraftOnSwitch,
  writeComposerDraft,
} from "../components/conversation/composerDraftCache";
import {
  clearComposerAttachmentCache,
  peekComposerAttachments,
  releaseDroppedComposerAttachmentPreviews,
  resolveComposerAttachmentsOnSwitch,
  writeComposerAttachments,
} from "../components/conversation/composerAttachmentCache";
import {
  classifyLocalSlashCommand,
  composerEnterAction,
  composerEnterBusy,
  editNoticeAfterSend,
  EDIT_BUSY_PROGRESS_NOTICE,
  STOP_INTERRUPT_FAILED_NOTICE,
  executeSendGate,
  formatCompactCompleteMessage,
  formatCompactErrorMessage,
  shouldApplyCompactSettle,
  formatHelpSlashReply,
  localSlashChromeAction,
  localSlashPaletteAction,
  runEditMessageFlow,
  runStopFlow,
  shouldBlockEmptySend,
  shouldClearSteerDraftOnResult,
  showStandaloneEditNoticeDismiss,
  userOrdinalBeforeIndex,
} from "../components/conversation/composerSend";
import {
  clearedSessionOverlays,
  shouldApplySpillPreview,
} from "../components/conversation/SpillPreviewModal";
import { runCommandPaletteAction } from "../lib/commandPalette";
import {
  appendMentionsToInput,
  buildCodebaseInsert,
  buildFolderInsert,
  buildMentionInsert,
  buildSymbolInsert,
  clampSelectIndex,
  codebaseMentionMatches,
  codebaseQueryFromMentionSearch,
  cycleSelectIndex,
  detectComposerTrigger,
  filterMentionPaths,
  filterSlashCommands,
  mentionTokenForDroppedPath,
} from "../components/conversation/composerInput";
import {
  blankMsgQueueOnSessionSwitch,
  blankQueueItemsOnSessionSwitch,
  moveItem,
  QUEUE_LOAD_FAIL_NOTICE,
  reorderByDrag,
  shouldApplyQueueRefresh,
} from "../components/conversation/queueOps";
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
  staleLocalStreamTickDecision,
  userStoppedBusyChrome,
} from "../components/conversation/runnersBusy";
import {
  contextUsagePercent,
  formatTokenK,
  normalizeContextUsage,
} from "../components/conversation/contextUsageColors";
import {
  FEED_REPIN_THRESHOLD_PX,
  FEED_SETTLE_STABLE_FRAMES,
  FEED_SETTLE_TIMEOUT_MS,
  FEED_UNPIN_BUBBLE_EVENT,
  feedWheelUnpinListenerOptions,
  isPinnedToBottom,
  nextFeedPinState,
  pinStateFromScrollGeometry,
  settleFrameResult,
  shouldStopNestedWheelBubble,
  shouldUnpinInnerOnWheel,
  shouldUnpinOnTouchMove,
  shouldUnpinOnWheel,
  THINKING_INNER_PIN_THRESHOLD_PX,
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

  it("newThinkingId mints unique ids", () => {
    const a = newThinkingId();
    const b = newThinkingId();
    expect(a).toMatch(/^th-/);
    expect(b).toMatch(/^th-/);
    expect(a).not.toBe(b);
  });

  it("coalesceThinkingChunk keeps identical/stale/extension and merges partial overlap", () => {
    expect(coalesceThinkingChunk("hello", "")).toBe("hello");
    expect(coalesceThinkingChunk("", "hello")).toBe("hello");
    expect(coalesceThinkingChunk("hello", "hello")).toBe("hello");
    // Stale prefix snapshot — keep the longer existing text.
    expect(coalesceThinkingChunk("hello world", "hello")).toBe("hello world");
    // Strict extension snapshot — replace with the longer chunk.
    expect(coalesceThinkingChunk("hello", "hello world")).toBe("hello world");
    // Partial overlap — merge at the longest shared boundary.
    expect(coalesceThinkingChunk("hello world", "world foo")).toBe("hello world foo");
    expect(coalesceThinkingChunk("abcXYZdef", "XYZdefGHI")).toBe("abcXYZdefGHI");
    // No overlap — append.
    expect(coalesceThinkingChunk("alpha", "beta")).toBe("alphabeta");
  });

  it("upsertStreamingThinking coalesceSnapshots uses coalesce; default strict-appends", () => {
    let snap: Item[] = [{ kind: "msg", msg: { role: "user", text: "go" } }];
    snap = upsertStreamingThinking(snap, "hello world", { coalesceSnapshots: true });
    snap = upsertStreamingThinking(snap, "world foo", { coalesceSnapshots: true });
    expect((snap.find((i) => i.kind === "thinking") as Extract<Item, { kind: "thinking" }>).text)
      .toBe("hello world foo");

    let live: Item[] = [{ kind: "msg", msg: { role: "user", text: "go" } }];
    live = upsertStreamingThinking(live, "hello world");
    live = upsertStreamingThinking(live, "world foo");
    expect((live.find((i) => i.kind === "thinking") as Extract<Item, { kind: "thinking" }>).text)
      .toBe("hello worldworld foo");
  });

  it("hoists all trailing tool_prep rows before a sealed finale", () => {
    const finalText =
      "Validated.\n\n| Gap | Evidence |\n|---|---|\n| Lease | heartbeat |\n\nShip-ready.";
    const items: Item[] = [
      msg("user", "validate"),
      { kind: "msg", msg: { role: "assistant", text: finalText } },
      {
        kind: "card",
        card: {
          id: "c-late",
          goal: "a.py",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
        },
      },
      { kind: "tool_prep", name: "Read" },
      { kind: "tool_prep", name: "Grep" },
    ];
    const next = hoistCardsBeforeTrailingFinals(items);
    const kinds = next.map((it) => {
      if (it.kind === "card") return "card";
      if (it.kind === "tool_prep") return `tool_prep:${it.name}`;
      if (it.kind === "msg") return `msg:${it.msg.role}`;
      return it.kind;
    });
    expect(kinds).toEqual([
      "msg:user",
      "card",
      "tool_prep:Read",
      "tool_prep:Grep",
      "msg:assistant",
    ]);
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
    expect(isTerminalStreamKind("interrupted")).toBe(true);
    expect(isTerminalStreamKind("done")).toBe(true);
  });

  it("treats degraded job status as terminal", () => {
    expect(isTerminalJobStatus("degraded")).toBe(true);
    expect(isTerminalJobStatus("DEGRADED")).toBe(true);
    expect(isTerminalJobStatus("running")).toBe(false);
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
  it("finds streaming bubble past thinking decoration but not past tool cards", () => {
    const withThinking: Item[] = [
      msg("user", "go"),
      { kind: "msg", msg: { role: "assistant", text: "hi", streaming: true } },
      { kind: "thinking", text: "reason", streaming: true, id: "t1" },
    ];
    expect(findStreamingBubbleIdx(withThinking)).toBe(1);
    const afterThink = appendStreamingTextToItems(withThinking, " there");
    expect((afterThink[1] as Extract<Item, { kind: "msg" }>).msg.text).toBe("hi there");

    // Tool/prep cards are a hard fence — never resume the pre-card bubble.
    const withCard: Item[] = [
      msg("user", "go"),
      { kind: "msg", msg: { role: "assistant", text: "hi", streaming: true } },
      {
        kind: "card",
        card: { id: "c1", goal: "read", cwd: null, kind: "read_file", running: true, open: false },
      },
    ];
    expect(findStreamingBubbleIdx(withCard)).toBe(-1);
    const afterCard = appendStreamingTextToItems(withCard, "verdict");
    expect((afterCard[1] as Extract<Item, { kind: "msg" }>).msg.text).toBe("hi");
    expect((afterCard[afterCard.length - 1] as Extract<Item, { kind: "msg" }>).msg.text).toBe("verdict");
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

  it("finds an earlier pilot bubble when a trailing worker preview fails affinity", () => {
    // Regression: excludeWorkerStream used to break on the trailing worker
    // stream and mint a duplicate pilot bubble instead of appending.
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      { kind: "msg", msg: { role: "assistant", text: "pilot", streaming: true } },
      {
        kind: "msg",
        msg: { role: "assistant", text: "worker", streaming: true, workerStream: true },
      },
    ];
    expect(findStreamingBubbleIdx(items, { excludeWorkerStream: true })).toBe(1);
    expect(findStreamingBubbleIdx(items, { workerStreamOnly: true })).toBe(2);
    const next = appendStreamingTextToItems(items, " more", { workerStream: false });
    expect((next[1] as Extract<Item, { kind: "msg" }>).msg.text).toBe("pilot more");
    expect((next[2] as Extract<Item, { kind: "msg" }>).msg.text).toBe("worker");
  });

  it("finds an earlier worker bubble when a trailing pilot fails workerStreamOnly affinity", () => {
    // Mirror of the pilot-affinity regression: workerStreamOnly must skip the
    // trailing open pilot and land on the earlier worker preview.
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      {
        kind: "msg",
        msg: { role: "assistant", text: "worker", streaming: true, workerStream: true },
      },
      { kind: "msg", msg: { role: "assistant", text: "pilot", streaming: true } },
    ];
    expect(findStreamingBubbleIdx(items, { workerStreamOnly: true })).toBe(1);
    expect(findStreamingBubbleIdx(items, { excludeWorkerStream: true })).toBe(2);
    const next = appendStreamingTextToItems(items, " more", { workerStream: true });
    expect((next[1] as Extract<Item, { kind: "msg" }>).msg.text).toBe("worker more");
    expect((next[2] as Extract<Item, { kind: "msg" }>).msg.text).toBe("pilot");
  });

  it("keys workerStream bubbles by worker_id so parallel workers do not collide", () => {
    let items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "swarm" } },
    ];
    items = ensureWorkerStreamingBubble(items, { workerId: "local-aa" });
    items = appendStreamingTextToItems(items, "a1", {
      workerStream: true,
      workerId: "local-aa",
    });
    items = ensureWorkerStreamingBubble(items, { workerId: "local-bb" });
    items = appendStreamingTextToItems(items, "b1", {
      workerStream: true,
      workerId: "local-bb",
    });
    items = appendStreamingTextToItems(items, "a2", {
      workerStream: true,
      workerId: "local-aa",
    });

    const workers = items.filter(
      (i): i is Extract<Item, { kind: "msg" }> =>
        i.kind === "msg" && i.msg.workerStream === true,
    );
    expect(workers).toHaveLength(2);
    expect(workers[0].msg.worker_id).toBe("local-aa");
    expect(workers[0].msg.text).toBe("a1a2");
    expect(workers[1].msg.worker_id).toBe("local-bb");
    expect(workers[1].msg.text).toBe("b1");

    expect(
      findStreamingBubbleIdx(items, {
        workerStreamOnly: true,
        workerId: "local-aa",
      }),
    ).toBe(1);
    expect(
      findStreamingBubbleIdx(items, {
        workerStreamOnly: true,
        workerId: "local-bb",
      }),
    ).toBe(2);

    const dropped = finalizeStreamingBubbleOnActionResult(items);
    expect(
      dropped.some(
        (i) => i.kind === "msg" && i.msg.workerStream === true,
      ),
    ).toBe(false);
  });

  it("does not resume under a sealed pilot when only a worker preview is open", () => {
    // Sealed assistant ends the scan — excludeWorkerStream must not fall
    // through to invent a resume slot under a finished pilot.
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      { kind: "msg", msg: { role: "assistant", text: "done", streaming: false } },
      {
        kind: "msg",
        msg: { role: "assistant", text: "worker", streaming: true, workerStream: true },
      },
    ];
    expect(findStreamingBubbleIdx(items, { excludeWorkerStream: true })).toBe(-1);
  });

  it("appendStreamingTextToItems appends into an open pilot or opens a new bubble", () => {
    const open: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      { kind: "msg", msg: { role: "assistant", text: "hi", streaming: true } },
    ];
    const appended = appendStreamingTextToItems(open, " there");
    expect((appended[1] as Extract<Item, { kind: "msg" }>).msg.text).toBe("hi there");
    expect(appendStreamingTextToItems(open, "")).toBe(open);

    const sealed: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      { kind: "msg", msg: { role: "assistant", text: "done", streaming: false } },
    ];
    const minted = appendStreamingTextToItems(sealed, "next");
    expect(minted).toHaveLength(3);
    expect((minted[2] as Extract<Item, { kind: "msg" }>).msg).toMatchObject({
      role: "assistant",
      text: "next",
      streaming: true,
    });
  });
});

describe("pillStatus + workspaceDisplay + StatusPill chrome", () => {
  it("derivePillStatus prefers Investigating chrome over idle/machine flaps", () => {
    expect(
      derivePillStatus({
        transcriptStale: true,
        answerChromeIdle: false,
        liveInvestigation: false,
        turnOpen: false,
        status: "idle",
      }),
    ).toBe("switching…");
    // answerChromeIdle alone must not idle the pill while composerBusy holds.
    expect(
      derivePillStatus({
        transcriptStale: false,
        answerChromeIdle: true,
        liveInvestigation: false,
        turnOpen: false,
        status: "thinking",
        agentLoopOpen: true,
      }),
    ).toBe("thinking");
    expect(
      derivePillStatus({
        transcriptStale: false,
        answerChromeIdle: false,
        liveInvestigation: true,
        turnOpen: true,
        status: "idle",
      }),
    ).toBe("investigating");
    // Between tools: raw executing/thinking must not flash in the header.
    expect(
      derivePillStatus({
        transcriptStale: false,
        answerChromeIdle: false,
        liveInvestigation: true,
        turnOpen: true,
        status: "executing",
      }),
    ).toBe("investigating");
    expect(
      derivePillStatus({
        transcriptStale: false,
        answerChromeIdle: false,
        liveInvestigation: true,
        turnOpen: true,
        status: "thinking",
      }),
    ).toBe("investigating");
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

  it("StatusPill stays Still working / Investigating while composerBusy", () => {
    // Sealed answer + lagging thinking: composerBusy true via agentLoopOpen.
    expect(isAgentLoopOpen(false, "thinking")).toBe(true);
    expect(isAgentLoopOpen(false, "streaming")).toBe(true);
    const sealedLag = derivePillStatus({
      transcriptStale: false,
      answerChromeIdle: true,
      liveInvestigation: false,
      turnOpen: false,
      status: "thinking",
      agentLoopOpen: true,
    });
    expect(sealedLag).toBe("thinking");
    expect(statusPillLabel(sealedLag)).toBe("Still working…");
    expect(
      statusPillLabel(
        derivePillStatus({
          transcriptStale: false,
          answerChromeIdle: true,
          liveInvestigation: false,
          turnOpen: false,
          status: "streaming",
          agentLoopOpen: true,
        }),
      ),
    ).toBe("Still working…");
    // Between tools with turnOpen: no idle flash.
    expect(
      derivePillStatus({
        transcriptStale: false,
        answerChromeIdle: false,
        liveInvestigation: false,
        turnOpen: true,
        status: "idle",
        agentLoopOpen: true,
      }),
    ).toBe("thinking");
    expect(
      statusPillLabel(
        derivePillStatus({
          transcriptStale: false,
          answerChromeIdle: false,
          liveInvestigation: true,
          turnOpen: true,
          status: "executing",
          agentLoopOpen: true,
        }),
      ),
    ).toBe("Investigating…");
    // Truly closed loop may still early-idle via answerChromeIdle.
    expect(
      derivePillStatus({
        transcriptStale: false,
        answerChromeIdle: true,
        liveInvestigation: false,
        turnOpen: false,
        status: "idle",
        agentLoopOpen: false,
      }),
    ).toBe("idle");
  });

  it("isAgentLoopOpen includes awaiting_swarm (Conversation + TranscriptList latch)", () => {
    expect(isAgentLoopOpen(false, "awaiting_swarm")).toBe(true);
    expect(isAgentLoopOpen(false, "idle")).toBe(false);
    expect(isAgentLoopOpen(true, "idle")).toBe(true);
    expect(isAgentLoopOpen(false, "streaming")).toBe(true);
  });

  it("workspaceLeafName and StatusPill helpers stay calm Cursor chrome", () => {
    expect(workspaceLeafName("C:\\Users\\me\\proj", undefined)).toBe("proj");
    expect(workspaceLeafName("C:\\Users\\me\\.pmharness\\home", "C:\\Users\\me\\.pmharness\\home")).toBe("Home");
    expect(statusPillLabel("thinking", "Investigating · read_file")).toBe("Investigating · read_file");
    expect(statusPillLabel("investigating")).toBe("Investigating…");
    expect(statusPillLabel("executing")).toBe("Investigating…");
    expect(statusPillLabel("thinking")).toBe("Still working…");
    expect(statusPillLabel("streaming")).toBe("Still working…");
    expect(statusPillLabel("awaiting_swarm")).toBe("Still working…");
    expect(statusPillLabel("idle", "x")).toBe("Ready");
    expect(statusPillLabel("idle")).toBe("Ready");
    expect(statusPillLabel("done")).toBe("Done");
    expect(statusPillLabel("error")).toBe("Error");
    expect(statusPillTextClass("error")).toContain("risk");
    expect(statusPillDotClass("streaming")).toContain("animate-pulse");
    expect(statusPillDotClass("investigating")).toContain("animate-pulse");
    expect(statusPillClickable("awaiting_swarm", undefined, () => {})).toBe(true);
    expect(statusPillClickable("investigating", "Investigating…", () => {})).toBe(true);
    expect(statusPillClickable("thinking", undefined, () => {})).toBe(false);
    expect(statusPillClickable("idle", "x", () => {})).toBe(false);
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

  it("held_for_review / analysis_ok are not failed applies", () => {
    expect(swarmResultOutcome({
      applied: false,
      error: null,
      held_for_review: true,
    })).toBe("held_for_review");
    expect(swarmResultOutcome({
      applied: false,
      error: null,
      analysis_ok: true,
    })).toBe("analysis_ok");
    expect(swarmResultOutcome({
      applied: false,
      error: null,
    })).toBe("failed");
    expect(swarmResultOutcome({
      applied: true,
      error: null,
    })).toBe("applied");

    let items: Item[] = [
      {
        kind: "swarm_pending",
        job_ids: ["job_held12345678"],
        objective: "ship patch",
        status: "running",
        terminal_job_ids: [],
      },
    ];
    items = applySwarmResultToItems(items, {
      job_id: "job_held12345678",
      objective: "ship patch",
      result: {
        applied: false,
        files: ["a.ts"],
        summary: "Patch held for review",
        error: null,
        held_for_review: true,
      },
    });
    expect(items[0]).toMatchObject({
      kind: "swarm_pending",
      status: "done",
      resolved: true,
    });
    expect(items[1]).toMatchObject({
      kind: "swarm_result",
      applied: false,
      held_for_review: true,
      error: null,
    });

    items = applySwarmResultToItems([], {
      job_id: "job_analysisok001",
      objective: "audit auth",
      result: {
        applied: false,
        files: [],
        summary: "FINDING: race",
        error: null,
        analysis_ok: true,
      },
    });
    expect(items[0]).toMatchObject({
      kind: "swarm_result",
      applied: false,
      analysis_ok: true,
      error: null,
    });
  });

  it("pending_review receipt is idempotent and focuses Review tab", () => {
    const kinds: string[] = [];
    const onEvt = (e: Event) => kinds.push(e.type);
    window.addEventListener("harness-focus-tab", onEvt);
    window.addEventListener("harness-reviews-refresh", onEvt);
    try {
      let items = appendPendingReview([], {
        id: "rev-abc12345",
        summary: "Held 2 files for review",
      });
      expect(items).toEqual([{
        kind: "pending_review",
        id: "rev-abc12345",
        summary: "Held 2 files for review",
      }]);
      items = appendPendingReview(items, {
        id: "rev-abc12345",
        summary: "Held 2 files for review",
      });
      expect(items).toHaveLength(1);
      focusReviewTabAndRefresh();
      expect(kinds).toEqual(["harness-focus-tab", "harness-reviews-refresh"]);
    } finally {
      window.removeEventListener("harness-focus-tab", onEvt);
      window.removeEventListener("harness-reviews-refresh", onEvt);
    }
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

  it("mixed reused+fresh: swarm_result before swarm_pending still settles the pill", () => {
    // Counterexample: reused terminal result arrives before the multi-job
    // pending frame; appendSwarmPending must seed terminal_job_ids so the
    // later fresh result can clear running.
    let items: Item[] = [];
    items = applySwarmResultToItems(items, {
      job_id: "local-reused",
      objective: "goal A",
      result: {
        applied: true,
        files: [],
        summary: "reused prior analysis",
        error: null,
        reuse_status: "reused",
        source_job_id: "local-src",
      },
    });
    items = appendSwarmPending(
      items,
      ["local-reused", "local-fresh"],
      "Parallel wave of goals: goal A, goal B",
    );
    const pendingAfterSeed = items.filter((it) => it.kind === "swarm_pending");
    expect(pendingAfterSeed).toHaveLength(1);
    expect(pendingAfterSeed[0]).toMatchObject({
      kind: "swarm_pending",
      status: "running",
      resolved: false,
      terminal_job_ids: ["local-reused"],
    });
    expect(pendingAfterSeed[0].job_ids).toEqual(["local-fresh", "local-reused"]);

    items = applySwarmResultToItems(items, {
      job_id: "local-fresh",
      objective: "goal B",
      result: {
        applied: true,
        files: [],
        summary: "fresh analysis",
        error: null,
        reuse_status: "fresh",
      },
    });
    const pending = items.filter((it) => it.kind === "swarm_pending");
    const results = items.filter((it) => it.kind === "swarm_result");
    expect(pending).toHaveLength(1);
    expect(pending[0]).toMatchObject({
      kind: "swarm_pending",
      status: "done",
      resolved: true,
    });
    expect(pending[0].terminal_job_ids).toEqual(["local-fresh", "local-reused"]);
    expect(results).toHaveLength(2);
    expect(results.map((r) => r.job_id).sort()).toEqual(["local-fresh", "local-reused"]);
    expect(results.some((r) => r.reuse_status === "reused")).toBe(true);
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

  it("stop-path seal closes streaming surfaces then settles orphan cards", () => {
    // Mirrors Conversation.stop / assistant_done: seal → hoist late Cursor CLI
    // tools → finalizeOrphanSwarmPills → reconcileOrphanInvestigationCards.
    let items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
    ];
    items = ensureAssistantStreamingBubble(items);
    items = appendStreamingTextToItems(items, "Still drafting…");
    items = upsertStreamingThinking(items, "reasoning in flight");
    items = [
      ...items,
      {
        kind: "swarm_pending",
        job_ids: ["local-swarm-stop"],
        objective: "orphan on stop",
        status: "running",
        terminal_job_ids: [],
      },
      {
        kind: "card",
        card: {
          id: "orphan-stop-card",
          kind: "run_command",
          goal: "pytest",
          running: true,
          open: false,
        },
      },
    ];
    const liveIds: string[] = [];
    const next = reconcileOrphanInvestigationCards(
      finalizeOrphanSwarmPills(
        hoistCardsBeforeTrailingFinals(sealOpenStreamSurfaces(items)),
        liveIds,
      ),
      liveIds,
    );

    const streamingMsgs = next.filter(
      (it) => it.kind === "msg" && it.msg.role === "assistant" && it.msg.streaming,
    );
    expect(streamingMsgs).toHaveLength(0);
    const openThinking = next.filter(
      (it) => it.kind === "thinking" && (it as { streaming?: boolean }).streaming,
    );
    expect(openThinking).toHaveLength(0);
    expect(next.find((it) => it.kind === "swarm_pending")).toMatchObject({
      status: "ended",
      resolved: true,
    });
    const card = next.find((it) => it.kind === "card") as Extract<Item, { kind: "card" }>;
    expect(card.card.running).toBe(false);
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

  it("60 identical swarm_pending replays keep one lifecycle row (scroll-stable)", () => {
    let items: Item[] = [];
    for (let i = 0; i < 60; i++) {
      items = appendSwarmPending(items, ["local-swarm-a1"], "fix auth");
    }
    const pills = items.filter((it) => it.kind === "swarm_pending");
    expect(pills).toHaveLength(1);
    expect(pills[0]).toMatchObject({
      job_ids: ["local-swarm-a1"],
      status: "running",
      objective: "fix auth",
    });
    // Scroll stability proxy: duplicate replay must not change transcript length.
    const afterReplay = appendSwarmPending(items, ["local-swarm-a1"], "fix auth");
    expect(afterReplay).toHaveLength(items.length);
    expect(afterReplay).toBe(items);
  });

  it("running → done → pending replay stays one done row", () => {
    let items = appendSwarmPending([], ["local-swarm-a1"], "fix auth");
    items = applySwarmResultToItems(items, {
      job_id: "local-swarm-a1",
      objective: "fix auth",
      applied: true,
      files: [],
      summary: "ok",
      error: null,
    });
    expect(items.filter((it) => it.kind === "swarm_pending")).toHaveLength(1);
    expect(items[0]).toMatchObject({ status: "done", resolved: true });

    const lenBefore = items.length;
    items = appendSwarmPending(items, ["local-swarm-a1"], "fix auth");
    expect(items).toHaveLength(lenBefore);
    expect(items.filter((it) => it.kind === "swarm_pending")).toHaveLength(1);
    expect(items[0]).toMatchObject({ status: "done", resolved: true });
  });

  it("keeps distinct job ids / objectives as separate lifecycle rows", () => {
    let items = appendSwarmPending([], ["local-swarm-a1"], "shared goal");
    items = appendSwarmPending(items, ["local-swarm-a2"], "shared goal");
    items = appendSwarmPending(items, ["job_other"], "different goal");
    const pills = items.filter((it) => it.kind === "swarm_pending");
    expect(pills).toHaveLength(3);
  });

  it("result alias updates the local-swarm pill in place (idempotent rehydrate)", () => {
    let items = appendSwarmPending([], ["local-swarm-a1"], "audit auth");
    items = applySwarmResultToItems(items, {
      job_id: "job_deadbeef1234",
      objective: "audit auth",
      applied: true,
      files: ["a.ts"],
      summary: "done",
      error: null,
    });
    expect(items.filter((it) => it.kind === "swarm_pending")).toHaveLength(1);
    expect(items[0]).toMatchObject({ status: "done", resolved: true });
    expect(items.filter((it) => it.kind === "swarm_result")).toHaveLength(1);

    // Session-switch clears processed-job refs; re-applying must not grow the feed.
    const again = applySwarmResultToItems(items, {
      job_id: "job_deadbeef1234",
      objective: "audit auth",
      applied: true,
      files: ["a.ts"],
      summary: "done",
      error: null,
    });
    expect(again).toBe(items);
    expect(again.filter((it) => it.kind === "swarm_result")).toHaveLength(1);

    // Reattach with richer reuse provenance must patch in place, not first-wins.
    const enriched = applySwarmResultToItems(items, {
      job_id: "job_deadbeef1234",
      objective: "audit auth",
      applied: true,
      files: ["a.ts"],
      summary: "done",
      error: null,
      result: {
        applied: true,
        files: ["a.ts"],
        summary: "done",
        error: null,
        reuse_status: "partial",
        source_job_id: "local-src",
        reuse_reason: "subset_invalidated",
        invalidated_paths: ["harness/auth.py"],
      },
    });
    expect(enriched.filter((it) => it.kind === "swarm_result")).toHaveLength(1);
    expect(enriched.find((it) => it.kind === "swarm_result")).toMatchObject({
      reuse_status: "partial",
      source_job_id: "local-src",
      invalidated_paths: ["harness/auth.py"],
    });

    // Later SSE correction: explicit false / fresh / [] must replace prior fields.
    const corrected = applySwarmResultToItems(enriched, {
      job_id: "job_deadbeef1234",
      objective: "audit auth",
      result: {
        applied: false,
        files: ["a.ts"],
        summary: "corrected",
        error: "failed",
        reuse_status: "fresh",
        source_job_id: "",
        reuse_reason: "",
        invalidated_paths: [],
      },
    });
    expect(corrected.filter((it) => it.kind === "swarm_result")).toHaveLength(1);
    expect(corrected.find((it) => it.kind === "swarm_result")).toMatchObject({
      applied: false,
      reuse_status: "fresh",
      source_job_id: "",
      invalidated_paths: [],
      error: "failed",
    });

    // Error-only SSE correction must patch (not drop) when other fields omitted.
    const errorOnly = applySwarmResultToItems(corrected, {
      job_id: "job_deadbeef1234",
      objective: "audit auth",
      result: {
        error: "timeout after retry",
      },
    });
    expect(errorOnly.find((it) => it.kind === "swarm_result")).toMatchObject({
      error: "timeout after retry",
      applied: false,
      reuse_status: "fresh",
      source_job_id: "",
      files: ["a.ts"],
    });

    // Files-only SSE correction; explicit [] clears, omitted files inherit.
    const filesOnly = applySwarmResultToItems(errorOnly, {
      job_id: "job_deadbeef1234",
      objective: "audit auth",
      result: {
        files: ["b.ts", "c.ts"],
      },
    });
    expect(filesOnly.find((it) => it.kind === "swarm_result")).toMatchObject({
      files: ["b.ts", "c.ts"],
      error: "timeout after retry",
      applied: false,
    });
    const filesCleared = applySwarmResultToItems(filesOnly, {
      job_id: "job_deadbeef1234",
      objective: "audit auth",
      result: {
        files: [],
      },
    });
    expect(filesCleared.find((it) => it.kind === "swarm_result")).toMatchObject({
      files: [],
      error: "timeout after retry",
    });
  });

  it("swarm_result stream payload keeps environment_fingerprint and acceptance_criteria", () => {
    let items = appendSwarmPending([], ["local-swarm-a1"], "audit auth");
    items = applySwarmResultToItems(items, {
      job_id: "job_envdrift1234",
      objective: "audit auth",
      result: {
        applied: false,
        files: [],
        summary: "full swarm",
        error: null,
        reuse_status: "fresh",
        reuse_reason: "environment_changed",
        environment_fingerprint: "env-fp-stream",
        acceptance_criteria: ["  keep env stamp  ", "", "tests pass"],
        validation_fingerprint: "fp-stream",
      },
    });
    expect(items.find((it) => it.kind === "swarm_result")).toMatchObject({
      kind: "swarm_result",
      reuse_status: "fresh",
      reuse_reason: "environment_changed",
      environment_fingerprint: "env-fp-stream",
      acceptance_criteria: ["keep env stamp", "tests pass"],
      validation_fingerprint: "fp-stream",
    });

    // Corrective SSE: enrich then clear environment provenance in place.
    const enriched = applySwarmResultToItems(items, {
      job_id: "job_envdrift1234",
      objective: "audit auth",
      result: {
        applied: true,
        files: ["a.ts"],
        summary: "reused",
        error: null,
        reuse_status: "reused",
        source_job_id: "local-src",
        reuse_reason: "fingerprint_match",
        environment_fingerprint: "env-fp-enriched",
        acceptance_criteria: ["docs updated"],
      },
    });
    expect(enriched.find((it) => it.kind === "swarm_result")).toMatchObject({
      reuse_status: "reused",
      environment_fingerprint: "env-fp-enriched",
      acceptance_criteria: ["docs updated"],
      source_job_id: "local-src",
    });

    const cleared = applySwarmResultToItems(enriched, {
      job_id: "job_envdrift1234",
      objective: "audit auth",
      result: {
        applied: false,
        files: [],
        summary: "fresh after env drift",
        error: null,
        reuse_status: "fresh",
        source_job_id: "",
        reuse_reason: "environment_changed",
        environment_fingerprint: "",
        acceptance_criteria: [],
      },
    });
    expect(cleared.filter((it) => it.kind === "swarm_result")).toHaveLength(1);
    expect(cleared.find((it) => it.kind === "swarm_result")).toMatchObject({
      reuse_status: "fresh",
      reuse_reason: "environment_changed",
      environment_fingerprint: "",
      acceptance_criteria: [],
      source_job_id: "",
    });

    // Omitted environment fields inherit; fingerprint must still see prior clear.
    const omitted = applySwarmResultToItems(cleared, {
      job_id: "job_envdrift1234",
      objective: "audit auth",
      result: {
        error: "thin findings",
      },
    });
    expect(omitted.find((it) => it.kind === "swarm_result")).toMatchObject({
      error: "thin findings",
      reuse_reason: "environment_changed",
      environment_fingerprint: "",
      acceptance_criteria: [],
    });
  });

  it("dedupeDisplayItems collapses hydrate duplicate swarm_pending rows", () => {
    const items: Item[] = [
      {
        kind: "swarm_pending",
        job_ids: ["local-swarm-a1"],
        objective: "fix auth",
        status: "running",
        terminal_job_ids: [],
      },
      {
        kind: "swarm_pending",
        job_ids: ["local-swarm-a1"],
        objective: "fix auth",
        status: "done",
        resolved: true,
        terminal_job_ids: ["local-swarm-a1"],
      },
      {
        kind: "swarm_pending",
        job_ids: ["local-swarm-a1"],
        objective: "fix auth",
        status: "running",
        terminal_job_ids: [],
      },
    ];
    const out = dedupeDisplayItems(items);
    expect(out.filter((it) => it.kind === "swarm_pending")).toHaveLength(1);
    expect(out[0]).toMatchObject({
      status: "done",
      resolved: true,
      terminal_job_ids: ["local-swarm-a1"],
    });
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
    expect(noticeShowsWaitHint(undefined)).toBe(true);
    expect(noticeShowsWaitHint("wait")).toBe(true);
    expect(noticeShowsWaitHint("stagnation")).toBe(true);
    expect(noticeShowsWaitHint("resume_cap")).toBe(true);
    expect(noticeShowsWaitHint("memory")).toBe(false);
    expect(noticeIsStopHonesty("owned_command_orphan")).toBe(true);
    expect(noticeIsStopHonesty("steer_dropped")).toBe(true);
    expect(noticeIsStopHonesty("wait")).toBe(false);
    expect(noticeIsStopHonesty(undefined)).toBe(false);
    expect(
      appendStopHonestyNotice([], "Stop cancelled owned tool work"),
    ).toEqual([
      {
        kind: "msg",
        msg: { role: "assistant", text: "Stop cancelled owned tool work" },
      },
    ]);
    // Dedupes identical honesty rows.
    expect(
      appendStopHonestyNotice(
        [
          {
            kind: "msg",
            msg: { role: "assistant", text: "Stop cancelled owned tool work" },
          },
        ],
        "Stop cancelled owned tool work",
      ),
    ).toHaveLength(1);
    expect(shouldPaintThinking({ text: "  ", delta: false }).painting).toBe(false);
    expect(shouldPaintThinking({ text: "a", delta: true }).painting).toBe(true);
    expect(workspaceRootFromActionResult({ path: "/repo" }, "(workspace root)")).toBe("/repo");
    expect(appendCommandBlocked([], { command: "rm" })[0].kind).toBe("command_blocked");
    expect(compactionAbortLabel("Automatic compaction paused", "anti_thrash_cooldown"))
      .toBe("Automatic compaction paused");
    expect(compactionAbortLabel("", "insufficient_reduction"))
      .toBe("Context compaction aborted (insufficient_reduction)");
    expect(compactionAbortLabel(null, null)).toBe("Context compaction aborted");
    const abortedRow = appendCompaction([], 12000, 12000, {
      aborted: true,
      reason: "degenerate_summary",
    })[0];
    expect(abortedRow).toMatchObject({
      kind: "compaction",
      aborted: true,
      reason: "degenerate_summary",
      message: "Context compaction aborted (degenerate_summary)",
    });
    expect(String((abortedRow as { message?: string }).message || ""))
      .not.toMatch(/Context summarized/i);
    expect(appendCompaction([], 9000, 3000, { mode: "llm" })[0]).toMatchObject({
      kind: "compaction",
      before_tokens: 9000,
      after_tokens: 3000,
      mode: "llm",
    });
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
    expect(shouldPreserveBusyStatus("awaiting_swarm")).toBe(true);
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
    // Pending swarms / awaiting_swarm prefer awaiting over thinking.
    expect(
      runnerBusySwitchDecision({
        runnerState: "running",
        localStreamActive: false,
        switchedSession: true,
        pendingSwarms: true,
      }).kind,
    ).toBe("awaiting");
    expect(
      runnerBusySwitchDecision({
        runnerState: "idle",
        localStreamActive: false,
        switchedSession: true,
        sessionState: "awaiting_swarm",
      }).kind,
    ).toBe("awaiting");
  });

  it("clears sticky SESSION_* editNotice after successful hydrate recovery", () => {
    expect(clearRecoveredSessionFailNotice(SESSION_TRANSCRIPT_FAIL_NOTICE)).toBeNull();
    expect(clearRecoveredSessionFailNotice(SESSION_STATE_FAIL_NOTICE)).toBeNull();
    expect(clearRecoveredSessionFailNotice("Rewind failed.")).toBe("Rewind failed.");
    expect(clearRecoveredSessionFailNotice(null)).toBeNull();
  });

  it("session-switch busy honesty defaults idle until runners resolve", () => {
    expect(shouldResetBusyChromeOnSwitch(true)).toBe(true);
    expect(shouldResetBusyChromeOnSwitch(false)).toBe(false);
    expect(sessionStateFailureSwitchDecision()).toEqual({
      kind: "idle_with_notice",
      notice: SESSION_STATE_FAIL_NOTICE,
    });
    expect(shouldRetryEmptyTranscript({ loadedCount: 0, attempt: 0, maxAttempts: 4 })).toBe(true);
    expect(shouldRetryEmptyTranscript({
      loadedCount: 0, attempt: 0, maxAttempts: 4, cachedCount: 0, seededEmpty: true,
    })).toBe(false);
    expect(shouldRetryEmptyTranscript({
      loadedCount: 0, attempt: 0, maxAttempts: 4, cachedCount: 0, seededEmpty: false,
    })).toBe(true);
    expect(cacheHitEmptyTranscriptDecision()).toEqual({
      kind: "keep_warm_with_notice",
      stale: true,
      notice: SESSION_TRANSCRIPT_FAIL_NOTICE,
    });
    expect(emptyTranscriptAfterRetryDecision({ cachedCount: 3 })).toEqual({
      kind: "keep_warm_with_notice",
      stale: true,
      notice: SESSION_TRANSCRIPT_FAIL_NOTICE,
    });
    expect(emptyTranscriptAfterRetryDecision({
      cachedCount: 0,
      seededEmpty: true,
    })).toEqual({
      kind: "accept_empty",
    });
    expect(transcriptRefreshFailureDecision(true)).toEqual({
      kind: "keep_warm_with_notice",
      clearItems: false,
      stale: true,
      notice: SESSION_TRANSCRIPT_FAIL_NOTICE,
    });
    expect(transcriptRefreshFailureDecision(false)).toEqual({
      kind: "clear_stale_with_notice",
      clearItems: true,
      stale: true,
      notice: SESSION_TRANSCRIPT_FAIL_NOTICE,
    });
    expect(reattachSessionStateFailureDecision({ attempt: 1, maxAttempts: 2 })).toBe("retry");
    expect(reattachSessionStateFailureDecision({ attempt: 2, maxAttempts: 2 })).toBe(
      "optimistic_busy",
    );
  });
});

describe("composer draft cache", () => {
  afterEach(() => {
    clearComposerDraftCache();
  });

  it("write/peek round-trip stores per session id", () => {
    writeComposerDraft("sess-a", "draft A");
    expect(peekComposerDraft("sess-a")).toBe("draft A");
    expect(peekComposerDraft("sess-b")).toBeUndefined();
  });

  it("resolveComposerDraftOnSwitch caches outgoing and restores incoming", () => {
    writeComposerDraft("sess-b", "cached B");
    const restored = resolveComposerDraftOnSwitch({
      prevId: "sess-a",
      nextId: "sess-b",
      currentDraft: "mid-type A",
    });
    expect(restored).toBe("cached B");
    expect(peekComposerDraft("sess-a")).toBe("mid-type A");
  });

  it("resolveComposerDraftOnSwitch blanks on cache miss (no cross-session draft)", () => {
    const restored = resolveComposerDraftOnSwitch({
      prevId: "sess-a",
      nextId: "sess-new",
      currentDraft: "only for A",
    });
    expect(restored).toBe("");
    expect(peekComposerDraft("sess-a")).toBe("only for A");
  });
});

describe("composer attachment cache", () => {
  afterEach(() => {
    clearComposerAttachmentCache();
  });

  it("resolveComposerAttachmentsOnSwitch blanks A on B and restores B cache", () => {
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
    writeComposerAttachments("sess-b", [imgB]);

    const onB = resolveComposerAttachmentsOnSwitch({
      prevId: "sess-a",
      nextId: "sess-b",
      currentAttachments: [imgA],
    });
    expect(onB).toEqual([imgB]);
    expect(peekComposerAttachments("sess-a")).toEqual([imgA]);

    const backToA = resolveComposerAttachmentsOnSwitch({
      prevId: "sess-b",
      nextId: "sess-a",
      currentAttachments: onB,
    });
    expect(backToA).toEqual([imgA]);
    expect(peekComposerAttachments("sess-b")).toEqual([imgB]);
  });

  it("blanks on cache miss (no cross-session attachment bleed)", () => {
    const imgA = {
      path: "uploads/a.png",
      name: "a.png",
      previewUrl: "blob:http://localhost/a",
    };
    const restored = resolveComposerAttachmentsOnSwitch({
      prevId: "sess-a",
      nextId: "sess-new",
      currentAttachments: [imgA],
    });
    expect(restored).toEqual([]);
    expect(peekComposerAttachments("sess-a")).toEqual([imgA]);
  });

  it("releaseDroppedComposerAttachmentPreviews revokes only unretained blobs", () => {
    const revoked: string[] = [];
    const original = URL.revokeObjectURL;
    URL.revokeObjectURL = (url: string) => {
      revoked.push(url);
    };
    try {
      const keep = {
        path: "uploads/keep.png",
        name: "keep.png",
        previewUrl: "blob:http://localhost/keep",
      };
      const drop = {
        path: "uploads/drop.png",
        name: "drop.png",
        previewUrl: "blob:http://localhost/drop",
      };
      releaseDroppedComposerAttachmentPreviews([keep, drop], [keep]);
      expect(revoked).toEqual(["blob:http://localhost/drop"]);
    } finally {
      URL.revokeObjectURL = original;
    }
  });
});

describe("prompt queue session-switch honesty", () => {
  it("blanks visible queue rows and soft msgQueue on switch", () => {
    expect(blankQueueItemsOnSessionSwitch()).toEqual([]);
    expect(blankMsgQueueOnSessionSwitch()).toEqual([]);
  });

  it("shouldApplyQueueRefresh fences stale session / gen", () => {
    expect(
      shouldApplyQueueRefresh({
        requestSessionId: "sess-a",
        activeSessionId: "sess-a",
        requestGen: 2,
        currentGen: 2,
      }),
    ).toBe(true);
    expect(
      shouldApplyQueueRefresh({
        requestSessionId: "sess-a",
        activeSessionId: "sess-b",
        requestGen: 2,
        currentGen: 2,
      }),
    ).toBe(false);
    expect(
      shouldApplyQueueRefresh({
        requestSessionId: "sess-b",
        activeSessionId: "sess-b",
        requestGen: 1,
        currentGen: 2,
      }),
    ).toBe(false);
    expect(QUEUE_LOAD_FAIL_NOTICE.length).toBeGreaterThan(0);
  });
});

describe("composerSend module", () => {
  it("Enter busy latch matches composerBusy / agentLoopOpen (awaiting_swarm + turnOpen)", () => {
    expect(composerEnterBusy({ turnOpen: false, status: "awaiting_swarm" })).toBe(true);
    expect(composerEnterBusy({ turnOpen: true, status: "idle" })).toBe(true);
    expect(composerEnterBusy({ turnOpen: false, status: "idle" })).toBe(false);
    expect(composerEnterBusy({ turnOpen: false, status: "done" })).toBe(false);
    // During awaiting_swarm, Cmd/Ctrl+Enter must queue (not start a new send).
    expect(
      composerEnterAction({
        busy: composerEnterBusy({ turnOpen: false, status: "awaiting_swarm" }),
        metaOrCtrl: true,
      }),
    ).toBe("queue");
    // Plain Enter stays "send" so send() can steer while composerBusy.
    expect(
      composerEnterAction({
        busy: composerEnterBusy({ turnOpen: false, status: "awaiting_swarm" }),
        metaOrCtrl: false,
      }),
    ).toBe("send");
    expect(
      composerEnterAction({
        busy: composerEnterBusy({ turnOpen: true, status: "idle" }),
        metaOrCtrl: true,
      }),
    ).toBe("queue");
    // Idle: Cmd/Ctrl+Enter is a normal send (no queue latch).
    expect(
      composerEnterAction({
        busy: composerEnterBusy({ turnOpen: false, status: "idle" }),
        metaOrCtrl: true,
      }),
    ).toBe("send");
  });

  it("gates enter/send and formats slash replies", () => {
    expect(composerEnterAction({ busy: true, metaOrCtrl: true })).toBe("queue");
    expect(composerEnterAction({ busy: true, metaOrCtrl: false })).toBe("send");
    // Alt+Enter while busy interrupts (stop turn, then queue typed prompt).
    expect(
      composerEnterAction({ busy: true, metaOrCtrl: false, altKey: true }),
    ).toBe("interrupt");
    // Cmd/Ctrl wins over Alt when both are held.
    expect(
      composerEnterAction({ busy: true, metaOrCtrl: true, altKey: true }),
    ).toBe("queue");
    // Idle: Alt+Enter is a normal send (no interrupt latch).
    expect(
      composerEnterAction({ busy: false, metaOrCtrl: false, altKey: true }),
    ).toBe("send");
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
    expect(
      formatCompactErrorMessage(
        Object.assign(new Error("Recent turn is already compact"), {
          reason: "no_compactable_history",
        }),
      ),
    ).toMatch(/already compact/i);
    expect(
      formatCompactErrorMessage(
        Object.assign(new Error("rejected"), { reason: "summary_rejected" }),
      ),
    ).toMatch(/rejected/i);
    // Post-send edit chrome is cleared so Resubmit starts a live turn without
    // a leftover Revert? banner sitting on an idle composer.
    expect(editNoticeAfterSend(true)).toBeNull();
    expect(editNoticeAfterSend(false)).toBeNull();
  });

  it("shouldApplyCompactSettle fences mid-flight A→B session switch", () => {
    // Manual /compact and harness-compact-session settle must drop when the
    // active session changed — otherwise A's receipt paints into B's transcript.
    expect(
      shouldApplyCompactSettle({
        requestSessionId: "session-a",
        activeSessionId: "session-a",
      }),
    ).toBe(true);
    expect(
      shouldApplyCompactSettle({
        requestSessionId: "session-a",
        activeSessionId: "session-b",
      }),
    ).toBe(false);
    expect(
      shouldApplyCompactSettle({
        requestSessionId: "session-a",
        activeSessionId: null,
      }),
    ).toBe(false);
    expect(
      shouldApplyCompactSettle({
        requestSessionId: null,
        activeSessionId: null,
      }),
    ).toBe(true);
  });

  it("clearedSessionOverlays + shouldApplySpillPreview drop spill/lightbox on A→B", () => {
    // SpillPreviewModal / ImageLightbox are Conversation-local; switch must
    // clear both, and late readSpill must not re-fill A's body into B.
    expect(clearedSessionOverlays()).toEqual({
      spillPreview: null,
      lightboxUrl: null,
    });
    expect(
      shouldApplySpillPreview({
        requestSessionId: "session-a",
        activeSessionId: "session-a",
      }),
    ).toBe(true);
    expect(
      shouldApplySpillPreview({
        requestSessionId: "session-a",
        activeSessionId: "session-b",
      }),
    ).toBe(false);
    expect(
      shouldApplySpillPreview({
        requestSessionId: "session-a",
        activeSessionId: null,
      }),
    ).toBe(false);
    expect(
      shouldApplySpillPreview({
        requestSessionId: null,
        activeSessionId: null,
      }),
    ).toBe(true);
  });

  it("runStopFlow settles local UI then awaits interrupt", async () => {
    const order: string[] = [];
    const stopLocal = vi.fn(() => { order.push("stopLocal"); });
    const interruptSession = vi.fn(async () => {
      order.push("interrupt");
      return { ok: true };
    });

    const result = await runStopFlow({ stopLocal, interruptSession });
    expect(order).toEqual(["stopLocal", "interrupt"]);
    expect(result).toEqual({ kind: "ok", notices: [] });
  });

  it("runStopFlow refreshes transcript and returns interrupt notices", async () => {
    const order: string[] = [];
    const result = await runStopFlow({
      stopLocal: () => { order.push("stopLocal"); },
      interruptSession: async () => {
        order.push("interrupt");
        return {
          ok: true,
          notices: [
            { message: "orphan procs", reason: "owned_command_orphan", count: 1 },
          ],
        };
      },
      refreshTranscript: async () => { order.push("refresh"); },
    });
    expect(order).toEqual(["stopLocal", "interrupt", "refresh"]);
    expect(result).toEqual({
      kind: "ok",
      notices: [
        { message: "orphan procs", reason: "owned_command_orphan", count: 1 },
      ],
    });
  });

  it("runStopFlow surfaces interrupt failure notice", async () => {
    const result = await runStopFlow({
      stopLocal: vi.fn(),
      interruptSession: async () => ({ ok: false }),
    });
    expect(result).toEqual({
      kind: "interrupt_failed",
      notice: STOP_INTERRUPT_FAILED_NOTICE,
    });
  });

  it("runStopFlow surfaces interrupt throw as failure notice", async () => {
    const result = await runStopFlow({
      stopLocal: vi.fn(),
      interruptSession: async () => {
        throw new Error("network down");
      },
    });
    expect(result).toEqual({
      kind: "interrupt_failed",
      notice: "network down",
    });
  });

  it("shouldClearSteerDraftOnResult clears only on success", () => {
    expect(shouldClearSteerDraftOnResult(true)).toBe(true);
    expect(shouldClearSteerDraftOnResult(false)).toBe(false);
  });

  it("runEditMessageFlow stops locally, awaits interrupt, then rewinds when busy", async () => {
    const order: string[] = [];
    const stopLocal = vi.fn(() => { order.push("stopLocal"); });
    const interruptSession = vi.fn(async () => {
      order.push("interrupt");
      return { ok: true };
    });
    const rewindSession = vi.fn(async () => {
      order.push("rewind");
      return { ok: true, prefill: "hello", notice: "Editing — resubmit, or Revert to restore." };
    });

    const result = await runEditMessageFlow({
      composerBusy: true,
      idx: 2,
      userOrdinal: 1,
      originalText: "hello",
      stopLocal,
      interruptSession,
      rewindSession,
    });

    expect(order).toEqual(["stopLocal", "interrupt", "rewind"]);
    expect(result).toEqual({
      kind: "success",
      truncateToIndex: 2,
      prefill: "hello",
      notice: "Editing — resubmit, or Revert to restore.",
      workspace_restored: false,
    });
  });

  it("runEditMessageFlow idle edit rewinds without interrupt", async () => {
    const stopLocal = vi.fn();
    const interruptSession = vi.fn();
    const rewindSession = vi.fn(async () => ({ ok: true, prefill: "draft" }));

    const result = await runEditMessageFlow({
      composerBusy: false,
      idx: 3,
      userOrdinal: 2,
      originalText: "draft",
      stopLocal,
      interruptSession,
      rewindSession,
    });

    expect(stopLocal).not.toHaveBeenCalled();
    expect(interruptSession).not.toHaveBeenCalled();
    expect(rewindSession).toHaveBeenCalledWith(2);
    expect(result.kind).toBe("success");
    if (result.kind === "success") {
      expect(result.workspace_restored).toBe(false);
    }
  });

  it("runEditMessageFlow surfaces workspace_restored from rewind", async () => {
    const result = await runEditMessageFlow({
      composerBusy: false,
      idx: 1,
      userOrdinal: 0,
      originalText: "hi",
      stopLocal: vi.fn(),
      interruptSession: vi.fn(),
      rewindSession: async () => ({
        ok: true,
        prefill: "hi",
        notice: "workspace restored",
        workspace_restored: true,
      }),
    });
    expect(result).toEqual({
      kind: "success",
      truncateToIndex: 1,
      prefill: "hi",
      notice: "workspace restored",
      workspace_restored: true,
    });
  });

  it("EDIT_BUSY_PROGRESS_NOTICE begins with the auto-stop wording", () => {
    expect(EDIT_BUSY_PROGRESS_NOTICE.startsWith("Sending will stop and revert")).toBe(true);
  });

  it("showStandaloneEditNoticeDismiss targets orphan editNotice banners", () => {
    expect(
      showStandaloneEditNoticeDismiss({
        editingIndex: null,
        canRevertEdit: false,
        editNotice: "Could not stop the current turn.",
      }),
    ).toBe(true);
    expect(
      showStandaloneEditNoticeDismiss({
        editingIndex: 1,
        canRevertEdit: false,
        editNotice: "Editing",
      }),
    ).toBe(false);
  });

  it("userOrdinalBeforeIndex counts only user messages before the index", () => {
    const items = [
      { kind: "msg", msg: { role: "user" } },
      { kind: "thinking" },
      { kind: "msg", msg: { role: "assistant" } },
      { kind: "msg", msg: { role: "user" } },
    ];
    expect(userOrdinalBeforeIndex(items, 3)).toBe(1);
  });

  it("classifies local slash commands", () => {
    const builtIn = isBuiltInSlashCommand;
    expect(
      classifyLocalSlashCommand({ message: "/clear", isBuiltIn: builtIn, customNames: [] }).kind,
    ).toBe("clear");
    expect(
      classifyLocalSlashCommand({ message: "/new", isBuiltIn: builtIn, customNames: [] }).kind,
    ).toBe("new");
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

  it("classifies navigation slash commands as local (not sent to the model)", () => {
    const nav = [
      ["/swarm", "swarm", "open-swarm"],
      ["/terminal", "terminal", "open-terminal"],
      ["/settings", "settings", "open-settings"],
      ["/memory", "memory", "open-memory"],
      ["/mcp", "mcp", "open-mcp"],
      ["/files", "files", "open-files"],
      ["/state", "state", "open-state"],
    ] as const;
    for (const [cmd, kind, paletteId] of nav) {
      expect(isBuiltInSlashCommand(cmd)).toBe(true);
      const action = classifyLocalSlashCommand({
        message: cmd,
        isBuiltIn: isBuiltInSlashCommand,
        customNames: [],
      });
      expect(action.kind).toBe(kind);
      expect(localSlashPaletteAction(action)).toBe(paletteId);
    }
  });

  it("navigation slashes do not createSession or send; /memory and /mcp fire expected events", () => {
    const createSession = vi.fn();
    const send = vi.fn();
    const focusSettingsPage = vi.fn();
    const tabs: string[] = [];
    const expandEvents: string[] = [];
    const onFocusTab = (e: Event) => {
      tabs.push(String((e as CustomEvent).detail));
    };
    const onExpandMemory = () => expandEvents.push("harness-expand-memory");
    const onNew = () => {
      createSession();
    };
    window.addEventListener("harness-focus-tab", onFocusTab as EventListener);
    window.addEventListener("harness-expand-memory", onExpandMemory);
    window.addEventListener("harness-new-session", onNew);
    try {
      const cmds = [
        "/swarm",
        "/terminal",
        "/settings",
        "/memory",
        "/mcp",
        "/files",
        "/state",
      ];
      for (const cmd of cmds) {
        const action = classifyLocalSlashCommand({
          message: cmd,
          isBuiltIn: isBuiltInSlashCommand,
          customNames: [],
        });
        expect(action.kind).not.toBe("none");
        const paletteId = localSlashPaletteAction(action);
        expect(paletteId).not.toBeNull();
        // Hermetic Conversation handler: palette path only — never createSession/send.
        runCommandPaletteAction(paletteId!, {
          toggleLeft: () => {},
          toggleRight: () => {},
          focusSettingsPage,
        });
      }
      expect(createSession).not.toHaveBeenCalled();
      expect(send).not.toHaveBeenCalled();
      expect(tabs).toEqual([
        "swarm",
        "terminal",
        "settings",
        "settings", // /memory opens Settings advanced
        "mcp",
        "files",
        "state",
      ]);
      expect(focusSettingsPage).toHaveBeenCalledWith("advanced");
      expect(focusSettingsPage).toHaveBeenCalledTimes(1);
      // /memory must expand Agent Memory (not leave the accordion collapsed).
      expect(expandEvents).toEqual(["harness-expand-memory"]);
    } finally {
      window.removeEventListener("harness-focus-tab", onFocusTab as EventListener);
      window.removeEventListener("harness-expand-memory", onExpandMemory);
      window.removeEventListener("harness-new-session", onNew);
    }
  });

  it("help note lists local chrome slash commands", () => {
    const help = formatHelpSlashReply(SLASH_COMMANDS);
    expect(help).toMatch(/\/swarm/);
    expect(help).toMatch(/\/memory/);
    expect(help).toMatch(/\/mcp/);
    expect(help).toMatch(/Local chrome/);
  });

  it("separates /clear (visible transcript) from /new (new session)", () => {
    const builtIn = (cmd: string) => ["/clear", "/new"].includes(cmd);
    const clear = classifyLocalSlashCommand({
      message: "/clear",
      isBuiltIn: builtIn,
      customNames: [],
    });
    const neu = classifyLocalSlashCommand({
      message: "/new",
      isBuiltIn: builtIn,
      customNames: [],
    });
    expect(localSlashChromeAction(clear)).toBe("clear_visible");
    expect(localSlashChromeAction(neu)).toBe("new_session");

    // Hermetic Conversation handler: /clear must not dispatch harness-new-session
    // (LeftRail maps that to api.createSession).
    const seen: string[] = [];
    const onNew = () => {
      seen.push("harness-new-session");
    };
    window.addEventListener("harness-new-session", onNew);
    try {
      const applyChrome = (action: typeof clear) => {
        const chrome = localSlashChromeAction(action);
        if (chrome === "clear_visible") return "cleared_items";
        if (chrome === "new_session") {
          window.dispatchEvent(new Event("harness-new-session"));
          return "new_session";
        }
        return null;
      };
      expect(applyChrome(clear)).toBe("cleared_items");
      expect(seen).toEqual([]);
      expect(applyChrome(neu)).toBe("new_session");
      expect(seen).toEqual(["harness-new-session"]);
    } finally {
      window.removeEventListener("harness-new-session", onNew);
    }
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

  it("keeps @-mention picker alive while typing spaced filter queries", () => {
    // Mid-type spaced folder/file filter (Cursor keeps picker open).
    expect(detectComposerTrigger("@my file", 8)).toEqual({
      kind: "mention",
      query: "my file",
      atIndex: 0,
    });
    expect(detectComposerTrigger("see @my docs/util", 17)).toEqual({
      kind: "mention",
      query: "my docs/util",
      atIndex: 4,
    });
    expect(detectComposerTrigger('@folder:"my docs', 16)).toEqual({
      kind: "mention",
      query: 'folder:"my docs',
      atIndex: 0,
    });
    expect(detectComposerTrigger("@codebase:auth flow", 19)).toEqual({
      kind: "mention",
      query: "codebase:auth flow",
      atIndex: 0,
    });
    // Slash commands still close after the first space.
    expect(detectComposerTrigger("/help me", 8).kind).toBe("none");
    // Completed picker inserts must not reopen on the trailing space.
    expect(detectComposerTrigger("@a.ts ", 6).kind).toBe("none");
    expect(detectComposerTrigger('@"a b.ts" ', 10).kind).toBe("none");
    expect(detectComposerTrigger("@folder:src/lib ", 16).kind).toBe("none");
    expect(detectComposerTrigger('@folder:"my docs" ', 18).kind).toBe("none");
    expect(detectComposerTrigger("@codebase ", 10).kind).toBe("none");
  });

  it("builds inserts, cycles selection, and resolves drop mentions", () => {
    expect(buildMentionInsert("hi @", 3, 4, "a.ts")).toEqual({
      next: "hi @a.ts ",
      cursor: 9,
    });
    expect(buildMentionInsert("hi @", 3, 4, "a b.ts")).toEqual({
      next: 'hi @"a b.ts" ',
      cursor: 13,
    });
    expect(buildSymbolInsert("@", 0, 1, "Foo").next).toContain("@symbol:Foo");
    expect(buildFolderInsert("see @", 4, 5, "src/lib")).toEqual({
      next: "see @folder:src/lib ",
      cursor: 20,
    });
    expect(buildFolderInsert("see @", 4, 5, "my docs")).toEqual({
      next: 'see @folder:"my docs" ',
      cursor: 22,
    });
    expect(buildCodebaseInsert("hi @", 3, 4)).toEqual({
      next: "hi @codebase ",
      cursor: 13,
    });
    expect(buildCodebaseInsert("hi @", 3, 4, "Auth")).toEqual({
      next: "hi @codebase:Auth ",
      cursor: 18,
    });
    expect(buildCodebaseInsert("hi @", 3, 4, "my query")).toEqual({
      next: 'hi @codebase:"my query" ',
      cursor: 24,
    });
    expect(codebaseMentionMatches("")).toBe(true);
    expect(codebaseMentionMatches("code")).toBe(true);
    expect(codebaseMentionMatches("codebase")).toBe(true);
    expect(codebaseMentionMatches("codebase:Auth")).toBe(true);
    expect(codebaseMentionMatches("file")).toBe(false);
    expect(codebaseQueryFromMentionSearch("codebase:Auth")).toBe("Auth");
    expect(codebaseQueryFromMentionSearch("code")).toBeUndefined();
    expect(filterMentionPaths(["src/a.ts", "src/b.ts", "web/c.ts"], "src/", 10)).toEqual([
      "src/a.ts",
      "src/b.ts",
    ]);
    expect(filterMentionPaths(["aaa", "bbb", "ccc"], "", 2)).toEqual(["aaa", "bbb"]);
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
      mentionTokenForDroppedPath({
        osPath: "/repo/src",
        repo: "/repo",
        isDirectory: true,
      }),
    ).toBe("@folder:src");
    expect(
      mentionTokenForDroppedPath({ osPath: "/repo/a b.ts", repo: "/repo" }),
    ).toBe('@"a b.ts"');
    expect(
      mentionTokenForDroppedPath({
        osPath: "/repo/my docs",
        repo: "/repo",
        isDirectory: true,
      }),
    ).toBe('@folder:"my docs"');
    expect(
      mentionTokenForDroppedPath({
        osPath: "",
        repo: "/repo",
        uploadedPath: "/repo/uploads/x.ts",
      }),
    ).toBe("@uploads/x.ts");
    expect(
      mentionTokenForDroppedPath({
        osPath: "",
        repo: "/repo",
        uploadedPath: "/tmp/cool file.txt",
      }),
    ).toBe('@"/tmp/cool file.txt"');
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
    expect(
      staleLocalStreamTickDecision({
        localStreamActive: true,
        userStopped: false,
        runnerBusy: false,
        awaitingSwarm: false,
        turnSettled: false,
        sawRunnerBusyThisStream: false,
        consecutiveIdlePolls: 5,
      }).kind,
    ).toBe("noop");
    expect(
      staleLocalStreamTickDecision({
        localStreamActive: true,
        userStopped: false,
        runnerBusy: false,
        awaitingSwarm: false,
        turnSettled: false,
        sawRunnerBusyThisStream: true,
        consecutiveIdlePolls: 1,
      }).kind,
    ).toBe("hold_unconfirmed");
    expect(
      staleLocalStreamTickDecision({
        localStreamActive: true,
        userStopped: false,
        runnerBusy: false,
        awaitingSwarm: false,
        turnSettled: false,
        sawRunnerBusyThisStream: true,
        consecutiveIdlePolls: 2,
      }).kind,
    ).toBe("abandon");
    expect(
      staleLocalStreamTickDecision({
        localStreamActive: true,
        userStopped: false,
        runnerBusy: true,
        awaitingSwarm: false,
        turnSettled: false,
        sawRunnerBusyThisStream: true,
        consecutiveIdlePolls: 2,
      }).kind,
    ).toBe("noop");
    expect(
      staleLocalStreamTickDecision({
        localStreamActive: true,
        userStopped: false,
        runnerBusy: false,
        awaitingSwarm: true,
        turnSettled: false,
        sawRunnerBusyThisStream: true,
        consecutiveIdlePolls: 2,
      }).kind,
    ).toBe("noop");
    expect(
      staleLocalStreamTickDecision({
        localStreamActive: true,
        userStopped: false,
        runnerBusy: false,
        awaitingSwarm: false,
        turnSettled: true,
        sawRunnerBusyThisStream: true,
        consecutiveIdlePolls: 2,
      }).kind,
    ).toBe("noop");
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
    // Trackpad stutter fix: light upward wheel releases stick; a scroll event
    // still within the soft near-bottom band must NOT re-pin until the user
    // scrolls back toward the true bottom.
    {
      const height = 2000;
      const client = 400;
      // 40px from bottom — inside old 120px band, outside tight repin.
      const lightUpTop = height - client - 40;
      expect(
        nextFeedPinState({
          wasPinned: true,
          releasedByGesture: true,
          scrollHeight: height,
          scrollTop: lightUpTop,
          clientHeight: client,
          prevScrollTop: lightUpTop + 8,
          settling: false,
          repinPx: FEED_REPIN_THRESHOLD_PX,
        }),
      ).toEqual({ pinned: false, releasedByGesture: true });
      // Scroll back down into the tight bottom band → re-pin.
      expect(
        nextFeedPinState({
          wasPinned: false,
          releasedByGesture: true,
          scrollHeight: height,
          scrollTop: height - client - 10,
          clientHeight: client,
          prevScrollTop: lightUpTop,
          settling: false,
          repinPx: FEED_REPIN_THRESHOLD_PX,
        }),
      ).toEqual({ pinned: true, releasedByGesture: false });
    }
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

  it("nested live reasoning wheel helpers cooperate with capture-phase unpin", () => {
    expect(FEED_UNPIN_BUBBLE_EVENT).toBe("pmharness-feed-unpin");
    expect(feedWheelUnpinListenerOptions()).toEqual({
      passive: true,
      capture: true,
    });
    // Inner pane consumes wheel while scrolled off an edge.
    expect(shouldStopNestedWheelBubble(-1, false, true)).toBe(true);
    expect(shouldStopNestedWheelBubble(1, true, false)).toBe(true);
    // At top/bottom edges, bubble continues so outer feed can scroll too.
    expect(shouldStopNestedWheelBubble(-1, true, false)).toBe(false);
    expect(shouldStopNestedWheelBubble(1, false, true)).toBe(false);
    expect(shouldUnpinInnerOnWheel(-1)).toBe(true);
    expect(shouldUnpinInnerOnWheel(1)).toBe(false);
    // Inner pin threshold matches ThinkingBlock geometry checks.
    expect(
      isPinnedToBottom(500, 452, 100, THINKING_INNER_PIN_THRESHOLD_PX),
    ).toBe(true);
    expect(
      isPinnedToBottom(500, 300, 100, THINKING_INNER_PIN_THRESHOLD_PX),
    ).toBe(false);
    // Capture listener runs before nested stopPropagation — simulate ordering.
    let outerPinned = true;
    const settling = false;
    const onCaptureWheel = (deltaY: number) => {
      if (shouldUnpinOnWheel(deltaY, settling)) outerPinned = false;
    };
    const onNestedWheel = (deltaY: number, atTop: boolean, atBottom: boolean) => {
      if (shouldStopNestedWheelBubble(deltaY, atTop, atBottom)) {
        // stopPropagation — outer bubble listener would not run.
      }
      if (shouldUnpinInnerOnWheel(deltaY)) {
        /* inner unpinned */
      }
    };
    onCaptureWheel(-1);
    onNestedWheel(-1, false, true);
    expect(outerPinned).toBe(false);
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

describe("pilot tool-action visibility (prep promotion + result upsert)", () => {
  it("keeps Read→Write→Read as distinct ordered rows (no prep slot theft)", () => {
    let items: Item[] = [{ kind: "msg", msg: { role: "user", text: "edit" } }];
    items = upsertToolPrep(items, "read_file", { id: "call-r1", goal: "a.py" });
    items = upsertToolPrep(items, "write_file", { id: "call-w1", goal: "a.py" });
    items = appendActionStartCard(items, {
      id: "call-r1",
      kind: "read_file",
      goal: "a.py",
      call_id: "call-r1",
    });
    items = appendActionStartCard(items, {
      id: "call-w1",
      kind: "write_file",
      goal: "a.py",
      call_id: "call-w1",
    });
    items = appendActionStartCard(items, {
      id: "call-r2",
      kind: "read_file",
      goal: "a.py",
      call_id: "call-r2",
    });
    const cards = items.filter((i) => i.kind === "card") as Extract<Item, { kind: "card" }>[];
    expect(cards.map((c) => `${c.card.kind}:${c.card.id}`)).toEqual([
      "read_file:call-r1",
      "write_file:call-w1",
      "read_file:call-r2",
    ]);
    expect(cards[0].card.kind).toBe("read_file");
    expect(cards[1].card.kind).toBe("write_file");
  });

  it("does not promote by kind-only or oldest prep fallback", () => {
    let items: Item[] = [{ kind: "msg", msg: { role: "user", text: "go" } }];
    items = upsertToolPrep(items, "read_file", { id: "prep-read", goal: "a.py" });
    items = appendActionStartCard(items, {
      id: "a9",
      kind: "write_file",
      goal: "b.py",
    });
    const cards = items.filter((i) => i.kind === "card") as Extract<Item, { kind: "card" }>[];
    expect(cards).toHaveLength(2);
    expect(cards[0].card.id).toBe("tool-prep:prep-read");
    expect(cards[0].card.kind).toBe("read_file");
    expect(cards[1].card.id).toBe("a9");
    expect(cards[1].card.kind).toBe("write_file");
  });

  it("action_result inserts a missing-start card with kind/goal/status", () => {
    const items = applyActionResultCard([], {
      id: "miss-1",
      kind: "run_command",
      goal: "pytest -q",
      error: "boom",
      duration_ms: 42,
    });
    expect(items).toHaveLength(1);
    const card = (items[0] as Extract<Item, { kind: "card" }>).card;
    expect(card.id).toBe("miss-1");
    expect(card.kind).toBe("run_command");
    expect(card.goal).toBe("pytest -q");
    expect(card.running).toBe(false);
    expect(card.open).toBe(true); // error keeps the card expanded
    expect(card.result?.error).toBe("boom");
    expect(card.result?.duration_ms).toBe(42);
  });

  it("applyActionResultCard opens on non-zero exit and keeps quiet success collapsed", () => {
    const running: Item[] = [{
      kind: "card",
      card: {
        id: "run-1",
        goal: "pytest -q",
        kind: "run_command",
        running: true,
        open: true,
      },
    }];
    const failed = applyActionResultCard(running, {
      id: "run-1",
      kind: "run_command",
      command: "pytest -q",
      exit_code: 1,
      output: "FAILED tests/test_x.py::test_y",
      artifacts: [{ type: "command", headline: "exit 1 · FAILED tests/test_x.py::test_y" }],
    });
    const failedCard = (failed[0] as Extract<Item, { kind: "card" }>).card;
    expect(failedCard.open).toBe(true);
    expect(failedCard.running).toBe(false);
    expect(failedCard.result?.exit_code).toBe(1);
    expect(failedCard.result?.output).toContain("FAILED");
    expect(failedCard.result?.command).toBe("pytest -q");

    const quietOk = applyActionResultCard(running, {
      id: "run-1",
      kind: "run_command",
      command: "true",
      exit_code: 0,
      output: "",
      artifacts: [{ type: "command", headline: "Command exited with 0" }],
    });
    expect((quietOk[0] as Extract<Item, { kind: "card" }>).card.open).toBe(false);
  });

  it("applyActionResultCard hydrates spill_uri / output_spilled / output_chars", () => {
    const running: Item[] = [{
      kind: "card",
      card: {
        id: "spill-1",
        goal: "pytest -q",
        kind: "run_command",
        running: true,
        open: true,
      },
    }];
    const next = applyActionResultCard(running, {
      id: "spill-1",
      kind: "run_command",
      command: "pytest -q",
      exit_code: 0,
      output: "…truncated…",
      spill_uri: "spill://sess1/call_spill",
      output_spilled: true,
      output_chars: 12000,
      output_preview: "head…tail",
    });
    const card = (next[0] as Extract<Item, { kind: "card" }>).card;
    expect(card.result?.spill_uri).toBe("spill://sess1/call_spill");
    expect(card.result?.output_spilled).toBe(true);
    expect(card.result?.output_chars).toBe(12000);
    expect(card.result?.output_preview).toBe("head…tail");
    expect(card.result?.output).toBe("…truncated…");
  });

  it("applyActionResultCard opens on numeric-string exit_code and ignores non-numeric", () => {
    const running: Item[] = [{
      kind: "card",
      card: {
        id: "run-str",
        goal: "pytest -q",
        kind: "run_command",
        running: true,
        open: false,
      },
    }];
    const failed = applyActionResultCard(running, {
      id: "run-str",
      kind: "run_command",
      exit_code: "1",
      output: "",
    });
    expect((failed[0] as Extract<Item, { kind: "card" }>).card.open).toBe(true);

    const junk = applyActionResultCard(running, {
      id: "run-str",
      kind: "run_command",
      exit_code: "oops",
      output: "",
    });
    expect((junk[0] as Extract<Item, { kind: "card" }>).card.open).toBe(false);

    const empty = applyActionResultCard(running, {
      id: "run-str",
      kind: "run_command",
      exit_code: "",
      output: "",
    });
    expect((empty[0] as Extract<Item, { kind: "card" }>).card.open).toBe(false);
  });

  it("hydrates run_parallel goals and nested actions across reload", () => {
    const loaded = transcriptResponseToItems({
      display: [
        {
          type: "card",
          id: "a1",
          kind: "run_parallel",
          goal: "",
          goals: ["fix auth", "add tests"],
          result: { job_id: "local-aa,local-bb", status: "pending" },
          actions: [
            {
              action_id: "local-aa:t1",
              kind: "read_file",
              goal: "auth.py",
              status: "complete",
              duration_ms: 11,
              worker_id: "local-aa",
            },
            {
              action_id: "local-bb:t2",
              kind: "write_file",
              goal: "test_auth.py",
              status: "running",
              worker_id: "local-bb",
            },
          ],
        },
      ],
    });
    const card = (loaded[0] as Extract<Item, { kind: "card" }>).card;
    expect(card.goals).toEqual(["fix auth", "add tests"]);
    expect(card.actions).toHaveLength(2);
    expect(card.actions?.[0].kind).toBe("read_file");
    expect(card.actions?.[1].status).toBe("running");
  });

  it("mergeJobActionsIntoItems attaches live nested rows by job_id", () => {
    const items: Item[] = [{
      kind: "card",
      card: {
        id: "a1",
        goal: "implement",
        kind: "run_implement",
        running: true,
        open: false,
        result: { job_id: "local-xyz", status: "pending" },
      },
    }];
    const next = mergeJobActionsIntoItems(items, [{
      id: "local-xyz",
      actions: [
        { action_id: "n1", kind: "read_file", goal: "x.py", status: "complete", duration_ms: 3 },
        { action_id: "n2", kind: "edit_file", goal: "x.py", status: "running" },
      ],
    }]);
    const card = (next[0] as Extract<Item, { kind: "card" }>).card;
    expect(card.actions?.map((a) => a.action_id)).toEqual(["n1", "n2"]);
    expect(card.worker_id).toBe("local-xyz");
  });

  it("shouldApplySwarmLiveMerge fences stale generation and session", () => {
    expect(shouldApplySwarmLiveMerge({
      pollGen: 2,
      currentGen: 2,
      pollSessionId: "s1",
      cachedSessionId: "s1",
      activeSessionId: "s1",
    })).toBe(true);
    expect(shouldApplySwarmLiveMerge({
      pollGen: 1,
      currentGen: 2,
      pollSessionId: "s1",
      cachedSessionId: "s1",
      activeSessionId: "s1",
    })).toBe(false);
    expect(shouldApplySwarmLiveMerge({
      pollGen: 2,
      currentGen: 2,
      pollSessionId: "s1",
      cachedSessionId: "s2",
      activeSessionId: "s1",
    })).toBe(false);
    expect(shouldApplySwarmLiveMerge({
      pollGen: 2,
      currentGen: 2,
      pollSessionId: "s1",
      cachedSessionId: "s1",
      activeSessionId: "s2",
    })).toBe(false);
  });

  it("shouldApplySwarmLiveMerge busy-poll fence rejects late session-A poll for session B", () => {
    // Same contract useRunnersBusyPoll uses inside setItems: generation +
    // cached + active must all still match the poll's session id.
    // useSessionSwitch bumps runnerBusyPollGenRef on switch so generation alone
    // also rejects an in-flight session-A poll before B's next tick.
    const pollFromA = {
      pollGen: 3,
      currentGen: 4, // session switch bumped gen
      pollSessionId: "session-a",
      cachedSessionId: "session-b",
      activeSessionId: "session-b",
    };
    expect(shouldApplySwarmLiveMerge(pollFromA)).toBe(false);
    // Session-id mismatch alone (gen not yet bumped) must still reject.
    expect(shouldApplySwarmLiveMerge({
      pollGen: 3,
      currentGen: 3,
      pollSessionId: "session-a",
      cachedSessionId: "session-b",
      activeSessionId: "session-b",
    })).toBe(false);
    expect(shouldApplySwarmLiveMerge({
      pollGen: 4,
      currentGen: 4,
      pollSessionId: "session-b",
      cachedSessionId: "session-b",
      activeSessionId: "session-b",
    })).toBe(true);
  });

  it("shouldApplySwarmLiveMerge also fences getSwarmResults apply (R9)", () => {
    // Conversation swarm-results poll captures pollSid/pollGen before await;
    // after switch, pilot_resume / wiki / memory must not apply into B.
    expect(shouldApplySwarmLiveMerge({
      pollGen: 1,
      currentGen: 2,
      pollSessionId: "sess-a",
      cachedSessionId: "sess-b",
      activeSessionId: "sess-b",
    })).toBe(false);
    expect(shouldApplySwarmLiveMerge({
      pollGen: 2,
      currentGen: 2,
      pollSessionId: "sess-b",
      cachedSessionId: "sess-b",
      activeSessionId: "sess-b",
    })).toBe(true);
  });

  it("foldSwarmLiveJobsAfterReload leaves running tool-prep alone when live jobs empty", () => {
    // Mid-turn / reconnecting reload: empty swarmLive is not an authoritative
    // turn terminal — must not false-complete non-job tool-prep cards.
    const items: Item[] = [
      {
        kind: "card",
        card: {
          id: "tool-prep:call-mid",
          goal: "foo.ts",
          kind: "Read",
          running: true,
          open: true,
          call_id: "call-mid",
        },
      },
      {
        kind: "card",
        card: {
          id: "orphan-cmd",
          goal: "pytest",
          kind: "run_command",
          running: true,
          open: false,
        },
      },
    ];
    const next = foldSwarmLiveJobsAfterReload(items, []);
    expect(next).toBe(items);
    expect((next[0] as Extract<Item, { kind: "card" }>).card.running).toBe(true);
    expect((next[1] as Extract<Item, { kind: "card" }>).card.running).toBe(true);
    // Contrast: empty liveIds reconcile would clear them (turn-terminal only).
    const wrongly = reconcileOrphanInvestigationCards(items, []);
    expect((wrongly[0] as Extract<Item, { kind: "card" }>).card.running).toBe(false);
  });

  it("foldSwarmLiveJobsAfterReload merges terminal/actions without orphan-settling prep", () => {
    const items: Item[] = [
      {
        kind: "card",
        card: {
          id: "tool-prep:call-keep",
          goal: "bar.ts",
          kind: "Read",
          running: true,
          open: true,
          call_id: "call-keep",
        },
      },
      {
        kind: "card",
        card: {
          id: "a1",
          goal: "implement",
          kind: "run_implement",
          running: true,
          open: false,
          result: { job_id: "local-xyz", status: "pending" },
        },
      },
    ];
    const next = foldSwarmLiveJobsAfterReload(items, [{
      id: "local-xyz",
      status: "completed",
      actions: [
        { action_id: "n1", kind: "read_file", goal: "x.py", status: "complete", duration_ms: 3 },
      ],
    }]);
    const prep = (next[0] as Extract<Item, { kind: "card" }>).card;
    const jobCard = (next[1] as Extract<Item, { kind: "card" }>).card;
    expect(prep.running).toBe(true);
    expect(prep.id).toBe("tool-prep:call-keep");
    expect(jobCard.actions?.map((a) => a.action_id)).toEqual(["n1"]);
  });
});

describe("investigation terminal reconciliation + live ordering", () => {
  it("reconcileTerminalJobCards settles matching job and leaves unrelated alone", () => {
    const items: Item[] = [
      {
        kind: "card",
        card: {
          id: "a1",
          goal: "implement",
          kind: "run_implement",
          running: true,
          open: false,
          result: { job_id: "local-done" },
          actions: [
            { action_id: "n1", kind: "read_file", goal: "a.py", status: "running" },
          ],
        },
      },
      {
        kind: "card",
        card: {
          id: "a2",
          goal: "other",
          kind: "run_implement",
          running: true,
          open: false,
          result: { job_id: "local-live" },
          actions: [
            { action_id: "n2", kind: "write_file", goal: "b.py", status: "running" },
          ],
        },
      },
    ];
    const next = reconcileTerminalJobCards(items, "local-done", "complete");
    const done = (next[0] as Extract<Item, { kind: "card" }>).card;
    const live = (next[1] as Extract<Item, { kind: "card" }>).card;
    expect(done.running).toBe(false);
    expect(done.actions?.[0].status).toBe("complete");
    expect(live.running).toBe(true);
    expect(live.actions?.[0].status).toBe("running");
  });

  it("applySwarmResultToItems clears matching card.running and nested spinners", () => {
    let items: Item[] = [
      {
        kind: "swarm_pending",
        job_ids: ["local-xyz"],
        objective: "fix it",
        status: "running",
      },
      {
        kind: "card",
        card: {
          id: "a1",
          goal: "fix it",
          kind: "run_implement",
          running: true,
          open: false,
          result: { job_id: "local-xyz", status: "pending" },
          actions: [
            { action_id: "t1", kind: "read_file", goal: "x.py", status: "running" },
          ],
        },
      },
    ];
    items = applySwarmResultToItems(items, {
      job_id: "local-xyz",
      objective: "fix it",
      applied: true,
      summary: "done",
    });
    const card = items.find((i) => i.kind === "card") as Extract<Item, { kind: "card" }>;
    expect(card.card.running).toBe(false);
    expect(card.card.actions?.[0].status).toBe("complete");
    const pill = items.find((i) => i.kind === "swarm_pending") as Extract<Item, { kind: "swarm_pending" }>;
    expect(pill.status).toBe("done");
  });

  it("run_parallel siblings: terminal child settles only its nested rows", () => {
    const items: Item[] = [{
      kind: "card",
      card: {
        id: "p1",
        goal: "",
        kind: "run_parallel",
        running: true,
        open: false,
        result: { job_id: "local-aa,local-bb", status: "pending" },
        actions: [
          { action_id: "local-aa:t1", kind: "read_file", goal: "a.py", status: "running", worker_id: "local-aa" },
          { action_id: "local-bb:t2", kind: "write_file", goal: "b.py", status: "running", worker_id: "local-bb" },
        ],
      },
    }];
    const next = mergeJobActionsIntoItems(items, [
      {
        id: "local-aa",
        status: "completed",
        actions: [
          { action_id: "t1", kind: "read_file", goal: "a.py", status: "complete", duration_ms: 2 },
        ],
      },
    ]);
    const card = (next[0] as Extract<Item, { kind: "card" }>).card;
    expect(card.running).toBe(true); // sibling still live
    expect(card.actions?.find((a) => a.action_id.endsWith("t1"))?.status).toBe("complete");
    expect(card.actions?.find((a) => a.action_id.endsWith("t2"))?.status).toBe("running");
  });

  it("partial live snapshot preserves omitted sibling rows", () => {
    const items: Item[] = [{
      kind: "card",
      card: {
        id: "p1",
        goal: "",
        kind: "run_parallel",
        running: true,
        open: false,
        result: { job_id: "local-aa,local-bb" },
        actions: [
          { action_id: "local-aa:t1", kind: "read_file", goal: "a.py", status: "complete", worker_id: "local-aa" },
          { action_id: "local-bb:t2", kind: "write_file", goal: "b.py", status: "running", worker_id: "local-bb" },
        ],
      },
    }];
    const next = mergeJobActionsIntoItems(items, [{
      id: "local-aa",
      status: "completed",
      actions: [
        { action_id: "t1", kind: "read_file", goal: "a.py", status: "complete" },
      ],
    }]);
    const card = (next[0] as Extract<Item, { kind: "card" }>).card;
    expect(card.actions?.map((a) => a.action_id)).toEqual([
      "local-aa:t1",
      "local-bb:t2",
    ]);
  });

  it("mergeJobActionsIntoItems retains action_ids missing from a later partial poll", () => {
    const items: Item[] = [{
      kind: "card",
      card: {
        id: "a1",
        goal: "implement",
        kind: "run_implement",
        running: true,
        open: false,
        result: { job_id: "local-xyz" },
        actions: [
          { action_id: "n1", kind: "read_file", goal: "a.py", status: "complete", worker_id: "local-xyz" },
          { action_id: "n2", kind: "edit_file", goal: "a.py", status: "running", worker_id: "local-xyz" },
        ],
      },
    }];
    // Shorter poll drops n1 — must retain, not wipe the timeline.
    const next = mergeJobActionsIntoItems(items, [{
      id: "local-xyz",
      status: "running",
      actions: [
        { action_id: "n2", kind: "edit_file", goal: "a.py", status: "complete", duration_ms: 4 },
      ],
    }]);
    const card = (next[0] as Extract<Item, { kind: "card" }>).card;
    expect(card.actions?.map((a) => a.action_id)).toEqual(["n1", "n2"]);
    expect(card.actions?.find((a) => a.action_id === "n1")?.status).toBe("complete");
    expect(card.actions?.find((a) => a.action_id === "n2")?.status).toBe("complete");
  });

  it("mergeJobActionsIntoItems refuses terminal→running and failed→complete regressions", () => {
    const items: Item[] = [{
      kind: "card",
      card: {
        id: "a1",
        goal: "implement",
        kind: "run_implement",
        running: true,
        open: false,
        result: { job_id: "local-xyz" },
        actions: [
          {
            action_id: "n1",
            kind: "read_file",
            goal: "a.py",
            status: "complete",
            duration_ms: 3,
            worker_id: "local-xyz",
          },
          {
            action_id: "n2",
            kind: "edit_file",
            goal: "a.py",
            status: "failed",
            error: "boom",
            worker_id: "local-xyz",
          },
        ],
      },
    }];
    const next = mergeJobActionsIntoItems(items, [{
      id: "local-xyz",
      status: "running",
      actions: [
        { action_id: "n1", kind: "read_file", goal: "a.py", status: "running" },
        { action_id: "n2", kind: "edit_file", goal: "a.py", status: "complete" },
      ],
    }]);
    const card = (next[0] as Extract<Item, { kind: "card" }>).card;
    expect(card.actions?.find((a) => a.action_id === "n1")?.status).toBe("complete");
    expect(card.actions?.find((a) => a.action_id === "n1")?.duration_ms).toBe(3);
    expect(card.actions?.find((a) => a.action_id === "n2")?.status).toBe("failed");
    expect(card.actions?.find((a) => a.action_id === "n2")?.error).toBe("boom");
  });

  it("mergeJobActionsIntoItems empty actions[] poll retains known nested rows", () => {
    const items: Item[] = [{
      kind: "card",
      card: {
        id: "a1",
        goal: "implement",
        kind: "run_implement",
        running: true,
        open: false,
        result: { job_id: "local-xyz" },
        actions: [
          { action_id: "n1", kind: "read_file", goal: "a.py", status: "complete", worker_id: "local-xyz" },
        ],
      },
    }];
    const next = mergeJobActionsIntoItems(items, [{
      id: "local-xyz",
      status: "running",
      actions: [],
    }]);
    const card = (next[0] as Extract<Item, { kind: "card" }>).card;
    expect(card.actions?.map((a) => a.action_id)).toEqual(["n1"]);
    expect(card.actions?.[0].status).toBe("complete");
  });

  it("mergeJobActionsIntoItems caps combined multi-job list at MAX_JOB_ACTIONS", () => {
    const actions = Array.from({ length: MAX_JOB_ACTIONS + 10 }, (_, i) => ({
      action_id: `n${i}`,
      kind: "read_file",
      goal: `f${i}.py`,
      status: "complete" as const,
    }));
    const items: Item[] = [{
      kind: "card",
      card: {
        id: "a1",
        goal: "big",
        kind: "run_implement",
        running: false,
        open: false,
        result: { job_id: "local-big" },
      },
    }];
    const next = mergeJobActionsIntoItems(items, [{
      id: "local-big",
      status: "completed",
      actions,
    }]);
    const card = (next[0] as Extract<Item, { kind: "card" }>).card;
    expect(card.actions?.length).toBe(MAX_JOB_ACTIONS);
  });

  it("completed tool_prep patches a promoted durable card by call_id", () => {
    let items: Item[] = [{ kind: "msg", msg: { role: "user", text: "go" } }];
    items = upsertToolPrep(items, "Read", { id: "call-9", goal: "a.ts", status: "in_progress" });
    items = appendActionStartCard(items, {
      id: "a9",
      kind: "read_file",
      goal: "a.ts",
      call_id: "call-9",
    });
    items = upsertToolPrep(items, "Read", { id: "call-9", goal: "a.ts", status: "completed" });
    const card = items.find((i) => i.kind === "card") as Extract<Item, { kind: "card" }>;
    expect(card.card.id).toBe("a9");
    expect(card.card.running).toBe(false);
    expect(card.card.call_id).toBe("call-9");
  });

  it("reconcileOrphanInvestigationCards settles missing action_result without live job", () => {
    const items: Item[] = [
      {
        kind: "card",
        card: {
          id: "orphan-1",
          goal: "pytest",
          kind: "run_command",
          running: true,
          open: false,
        },
      },
      {
        kind: "card",
        card: {
          id: "bg-1",
          goal: "implement",
          kind: "run_implement",
          running: true,
          open: false,
          result: { job_id: "local-bg", status: "pending" },
        },
      },
    ];
    const next = reconcileOrphanInvestigationCards(items, ["local-bg"]);
    const orphan = (next[0] as Extract<Item, { kind: "card" }>).card;
    const bg = (next[1] as Extract<Item, { kind: "card" }>).card;
    expect(orphan.running).toBe(false);
    expect(orphan.result?.error).toBe("missing action_result");
    expect(bg.running).toBe(true);
  });

  it("reconcileOrphanInvestigationCards drops anonymous tool-prep shells", () => {
    const items: Item[] = [
      {
        kind: "card",
        card: {
          id: "tool-prep:run_command",
          goal: "",
          kind: "run_command",
          running: true,
          open: false,
        },
      },
      {
        kind: "card",
        card: {
          id: "tool-prep:call-xyz",
          goal: "ship",
          kind: "run_implement",
          call_id: "call-xyz",
          running: true,
          open: false,
        },
      },
      {
        kind: "card",
        card: {
          id: "real-1",
          goal: "git status",
          kind: "run_command",
          running: false,
          open: false,
          result: { exit_code: 0 },
        },
      },
    ];
    const next = reconcileOrphanInvestigationCards(items, []);
    expect(next).toHaveLength(2);
    const prep = (next[0] as Extract<Item, { kind: "card" }>).card;
    const real = (next[1] as Extract<Item, { kind: "card" }>).card;
    expect(prep.id).toBe("tool-prep:call-xyz");
    expect(prep.running).toBe(false);
    expect(prep.result?.status).toBe("complete");
    expect(prep.result?.error).toBeUndefined();
    expect(real.id).toBe("real-1");
  });

  it("appendActionStartCard drops anonymous kind-keyed tool-prep shell", () => {
    const items: Item[] = [
      {
        kind: "card",
        card: {
          id: "tool-prep:run_implement",
          goal: "",
          kind: "run_implement",
          running: true,
          open: false,
        },
      },
    ];
    const next = appendActionStartCard(items, {
      id: "call-impl-1",
      kind: "run_implement",
      goal: "prefer marionette child",
    });
    const cards = next.filter((it) => it.kind === "card") as Extract<Item, { kind: "card" }>[];
    expect(cards).toHaveLength(1);
    expect(cards[0].card.id).toBe("call-impl-1");
    expect(cards[0].card.running).toBe(true);
    expect(cards[0].card.goal).toBe("prefer marionette child");
  });

  it("reconcileOrphanInvestigationCards clears stale running when result already landed", () => {
    const items: Item[] = [
      {
        kind: "card",
        card: {
          id: "read-1",
          goal: "harness/server.py",
          kind: "read_file",
          running: true,
          open: false,
          result: {
            num: 1,
            types: ["READ"],
            artifacts: [{ type: "READ", headline: "Read 120 chars" }],
          },
        },
      },
      {
        kind: "card",
        card: {
          id: "bg-live",
          goal: "ship",
          kind: "run_implement",
          running: true,
          open: false,
          result: { job_id: "job-live", status: "pending" },
        },
      },
    ];
    const next = reconcileOrphanInvestigationCards(items, ["job-live"]);
    const read = (next[0] as Extract<Item, { kind: "card" }>).card;
    const bg = (next[1] as Extract<Item, { kind: "card" }>).card;
    expect(read.running).toBe(false);
    expect(read.result?.error).toBeUndefined();
    expect(bg.running).toBe(true);
  });

  it("live row ordering: reasoning → prep → later reasoning keeps prep slot", () => {
    let items: Item[] = [{ kind: "msg", msg: { role: "user", text: "audit" } }];
    items = upsertStreamingThinking(items, "analysis-1");
    items = upsertToolPrep(sealOpenStreamSurfaces(items), "Read", {
      id: "call-r",
      goal: "foo.ts",
    });
    items = upsertStreamingThinking(items, "analysis-2");
    const kinds = items.map((it) => {
      if (it.kind === "card") return `card:${it.card.id}`;
      if (it.kind === "thinking") return "thinking";
      if (it.kind === "tool_prep") return "tool_prep";
      return it.kind;
    });
    expect(kinds).toEqual([
      "msg",
      "thinking",
      "card:tool-prep:call-r",
      "tool_prep",
      "thinking",
    ]);
    items = appendActionStartCard(items, {
      id: "call-r",
      kind: "read_file",
      goal: "foo.ts",
      call_id: "call-r",
    });
    const after = items.map((it) => {
      if (it.kind === "card") return `card:${it.card.id}`;
      if (it.kind === "thinking") return "thinking";
      return it.kind;
    });
    expect(after).toEqual([
      "msg",
      "thinking",
      "card:call-r",
      "thinking",
    ]);
  });

  it("reload merge replaces tool-prep slot instead of appending after later reasoning", () => {
    const local: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      { kind: "thinking", text: "reason-1" },
      {
        kind: "card",
        card: {
          id: "tool-prep:call-z",
          goal: "z.ts",
          kind: "read_file",
          running: true,
          open: false,
          call_id: "call-z",
        },
      },
      { kind: "thinking", text: "reason-2" },
    ];
    const remote: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      { kind: "thinking", text: "reason-1" },
      { kind: "thinking", text: "reason-2" },
      {
        kind: "card",
        card: {
          id: "a-z",
          goal: "z.ts",
          kind: "read_file",
          running: false,
          open: false,
          call_id: "call-z",
          result: { status: "complete", duration_ms: 3 },
        },
      },
    ];
    const merged = mergeTranscriptItems(local, remote);
    const kinds = merged.map((it) => {
      if (it.kind === "card") return `card:${it.card.id}`;
      if (it.kind === "thinking") return `thinking:${(it as Extract<Item, { kind: "thinking" }>).text}`;
      return it.kind;
    });
    expect(kinds).toEqual([
      "msg",
      "thinking:reason-1",
      "card:a-z",
      "thinking:reason-2",
    ]);
    expect(merged.some((it) => it.kind === "card" && it.card.id === "tool-prep:call-z")).toBe(false);
  });

  it("unknown nested status fallback aligns hydrate vs live merge", () => {
    const hydrated = transcriptResponseToItems({
      display: [{
        type: "card",
        id: "h1",
        kind: "run_implement",
        goal: "g",
        result: { job_id: "local-h" },
        actions: [{ action_id: "x", kind: "read_file", goal: "a.py", status: "weird" }],
      }],
    });
    const live = mergeJobActionsIntoItems([{
      kind: "card",
      card: {
        id: "h1",
        goal: "g",
        kind: "run_implement",
        running: true,
        open: false,
        result: { job_id: "local-h" },
      },
    }], [{
      id: "local-h",
      actions: [{ action_id: "x", kind: "read_file", goal: "a.py", status: "weird" }],
    }]);
    const hStatus = (hydrated[0] as Extract<Item, { kind: "card" }>).card.actions?.[0].status;
    const lStatus = (live[0] as Extract<Item, { kind: "card" }>).card.actions?.[0].status;
    expect(hStatus).toBe("complete");
    expect(lStatus).toBe("complete");
  });

  it("mergeJobActionsIntoItems bounds client action strings", () => {
    const hugeGoal = "g".repeat(MAX_ACTION_GOAL_CHARS + 40);
    const items: Item[] = [{
      kind: "card",
      card: {
        id: "b1",
        goal: "bound",
        kind: "run_implement",
        running: true,
        open: false,
        result: { job_id: "local-b" },
      },
    }];
    const next = mergeJobActionsIntoItems(items, [{
      id: "local-b",
      actions: [{
        action_id: "n1",
        kind: "read_file",
        goal: hugeGoal,
        status: "complete",
        error: "e".repeat(300),
      }],
    }]);
    const row = (next[0] as Extract<Item, { kind: "card" }>).card.actions?.[0];
    expect(row).toBeTruthy();
    expect((row?.goal || "").length).toBeLessThanOrEqual(MAX_ACTION_GOAL_CHARS);
    expect((row?.error || "").length).toBeLessThanOrEqual(240);
    expect(boundActionField(hugeGoal, MAX_ACTION_GOAL_CHARS).endsWith("…")).toBe(true);
  });

  it("applyActionResultCard settles nested running rows on the matched card", () => {
    const items: Item[] = [{
      kind: "card",
      card: {
        id: "a1",
        goal: "implement",
        kind: "run_implement",
        running: true,
        open: false,
        call_id: "call-a1",
        result: { job_id: "local-x", status: "pending" },
        actions: [
          { action_id: "n1", kind: "read_file", goal: "a.py", status: "running" },
        ],
      },
    }];
    const next = applyActionResultCard(items, {
      id: "a1",
      kind: "run_implement",
      goal: "implement",
      status: "complete",
      job_id: "local-x",
    });
    const card = (next[0] as Extract<Item, { kind: "card" }>).card;
    expect(card.running).toBe(false);
    expect(card.actions?.[0].status).toBe("complete");
  });

  it("tool_prep stamps call_id on provisional card create", () => {
    let items: Item[] = [{ kind: "msg", msg: { role: "user", text: "go" } }];
    items = upsertToolPrep(items, "Read", { id: "call-stamp", goal: "s.ts" });
    const card = items.find((i) => i.kind === "card") as Extract<Item, { kind: "card" }>;
    expect(card.card.id).toBe("tool-prep:call-stamp");
    expect(card.card.call_id).toBe("call-stamp");
  });

  it("hydrate settles nested running when parent result is terminal", () => {
    const hydrated = transcriptResponseToItems({
      display: [{
        type: "card",
        id: "h2",
        kind: "run_implement",
        goal: "g",
        result: { job_id: "local-h2", status: "completed" },
        actions: [
          { action_id: "x", kind: "read_file", goal: "a.py", status: "running" },
        ],
      }],
    });
    const card = (hydrated[0] as Extract<Item, { kind: "card" }>).card;
    expect(card.running).toBe(false);
    expect(card.actions?.[0].status).toBe("complete");
  });
});
