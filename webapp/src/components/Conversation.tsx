import { useEffect, useLayoutEffect, useRef, useState, useCallback } from "react";
import { Loader2, Send, Zap, Square, ChevronDown, ChevronUp, GripVertical, Trash2, ListChecks, Pencil, FileText, X, Code, Share2, Image as ImageIcon, Brain } from "lucide-react";
import { api, type Config } from "../lib/api";
import { panelOpacityClass } from "../lib/panelTransition";
import { usePolling } from "../lib/usePolling";
import PilotPicker from "./PilotPicker";
import { revealInFolderLabel, revealWorkspacePath, toAbsoluteWorkspacePath } from "../lib/transport";
import FileEditorPane from "./FileEditorPane";
import { TranscriptList, type Item, type Card } from "./TranscriptList";
import {
  deriveBusyProgress,
  turnHasInvestigationActivity,
  turnHasLiveInvestigation,
  turnLooksAnswerComplete,
} from "../lib/turnProgress";
import { renameDefaultSessionIfNeeded } from "../lib/sessionTitle";

import {
  resolveSwitchTranscript,
  peekTranscriptCache,
  writeTranscriptCache,
} from "./conversation/transcriptCache";
import {
  transcriptResponseToItems,
  mergeTranscriptItems,
  transcriptFingerprint,
} from "./conversation/transcriptItems";
import {
  finalizeStreamingThinking,
  upsertStreamingThinking,
  upsertToolPrep,
  newThinkingId,
} from "./conversation/thinkingToolPrep";
import {
  nextAppliedCursor,
  isTerminalStreamKind,
  shouldPollChatEvents,
  shouldArmChatEventsFromRunners,
  isChatEventReplayMiss,
  shouldAdvanceReplayCursor,
  ringGenerationAfterReplayMiss,
  shouldHydrateTranscriptOnReplayMiss,
  cursorAfterReplayMiss,
  chatFrameToStreamEvent,
  CHAT_EVENTS_POLL_MS,
} from "./conversation/chatEvents";
import {
  type MentionListingCap,
  formatMentionListingCapMessage,
  mergeSlashCommands,
  isBuiltInSlashCommand,
} from "./conversation/slashCommands";
import {
  pathIsUnder,
  filterTabsAfterDelete,
  remapTabsAfterRename,
  remapActiveTabAfterRename,
} from "./conversation/tabPaths";
import {
  appendStreamingTextToItems,
  typewriterCharsPerFrame,
} from "./conversation/streamBubbles";
import { derivePillStatus } from "./conversation/pillStatus";
import StatusPill from "./conversation/StatusPill";
import WorkspaceChip from "./conversation/WorkspaceChip";
import {
  appendActionStartCard,
  appendAuthFailure,
  appendAutoHalt,
  appendCheckpoint,
  appendCodegraphContext,
  appendCommandBlocked,
  appendCompaction,
  appendNonStreamingThinking,
  appendQueuedPromptUserBubble,
  appendStreamError,
  appendSwarmPending,
  applySwarmResultToItems,
  ensureAssistantStreamingBubble,
  ensureWorkerStreamingBubble,
  finalizePilotMessage,
  finalizeStreamingBubbleOnActionResult,
  formatDistilledNotice,
  formatWikiAutoIngestNotice,
  patchCardInItems,
  shouldPaintThinking,
  truncateWaitHint,
  workspaceRootFromActionResult,
} from "./conversation/streamApply";
import {
  collectDisplayArtifacts,
  emptySessionSwitchState,
  mergeUniqueArtifacts,
  runnerBusySwitchDecision,
  shouldPreserveBusyStatus,
} from "./conversation/sessionHydrate";
import {
  composerEnterAction,
  editNoticeAfterSend,
  executeSendGate,
  formatCompactCompleteMessage,
  formatCompactErrorMessage,
  formatHelpSlashReply,
  formatRenderCommandErrorMessage,
  formatSteerErrorMessage,
  shouldBlockEmptySend,
} from "./conversation/composerSend";

// Re-export pure helpers so existing test / LeftRail import paths keep working.
export {
  resolveSwitchTranscript,
  clearTranscriptCache,
  peekTranscriptCache,
  writeTranscriptCache,
} from "./conversation/transcriptCache";
export {
  getSimilarity,
  deduplicateAssistantNarration,
  dedupeDisplayItems,
  transcriptResponseToItems,
  shouldPreferLocalTranscript,
  mergeTranscriptItems,
  transcriptFingerprint,
} from "./conversation/transcriptItems";
export {
  finalizeStreamingThinking,
  upsertStreamingThinking,
  type ToolPrepOpts,
  upsertToolPrep,
  clearToolPrepPlaceholders,
  newThinkingId,
} from "./conversation/thinkingToolPrep";
export {
  nextAppliedCursor,
  isTerminalStreamKind,
  shouldPollChatEvents,
  shouldArmChatEventsFromRunners,
  type ChatEventReplayMissFields,
  isChatEventReplayMiss,
  shouldAdvanceReplayCursor,
  ringGenerationAfterReplayMiss,
  shouldHydrateTranscriptOnReplayMiss,
  cursorAfterReplayMiss,
  chatFrameToStreamEvent,
} from "./conversation/chatEvents";
export {
  isWorkspaceOpenLeaseExhausted,
  formatWorkspaceOpenLeaseExhaustedMessage,
} from "./conversation/leaseExhausted";
export { composerStatusFromRunner } from "./conversation/composerStatus";
export {
  SLASH_COMMANDS,
  formatMentionListingCapMessage,
  mergeSlashCommands,
  isBuiltInSlashCommand,
} from "./conversation/slashCommands";
export {
  normalizeTabPath,
  pathIsUnder,
  filterTabsAfterDelete,
  remapTabsAfterRename,
  remapActiveTabAfterRename,
} from "./conversation/tabPaths";
export {
  findStreamingBubbleIdx,
  appendStreamingTextToItems,
  typewriterCharsPerFrame,
} from "./conversation/streamBubbles";
export { derivePillStatus } from "./conversation/pillStatus";
export { workspaceLeafName } from "./conversation/workspaceDisplay";
export {
  statusPillLabel,
  statusPillTextClass,
  statusPillDotClass,
} from "./conversation/StatusPill";
export { default as StatusPill } from "./conversation/StatusPill";
export { default as WorkspaceChip } from "./conversation/WorkspaceChip";
export {
  patchCardInItems,
  appendAuthFailure,
  appendCommandBlocked,
  appendCodegraphContext,
  appendCompaction,
  truncateWaitHint,
  shouldPaintThinking,
  ensureAssistantStreamingBubble,
  ensureWorkerStreamingBubble,
  finalizePilotMessage,
  appendActionStartCard,
  finalizeStreamingBubbleOnActionResult,
  workspaceRootFromActionResult,
  appendSwarmPending,
  appendCheckpoint,
  appendQueuedPromptUserBubble,
  appendAutoHalt,
  appendStreamError,
  appendNonStreamingThinking,
  applySwarmResultToItems,
  formatDistilledNotice,
  formatWikiAutoIngestNotice,
} from "./conversation/streamApply";
export {
  collectDisplayArtifacts,
  mergeUniqueArtifacts,
  emptySessionSwitchState,
  shouldPreserveBusyStatus,
  runnerBusySwitchDecision,
} from "./conversation/sessionHydrate";
export {
  composerEnterAction,
  executeSendGate,
  shouldBlockEmptySend,
  formatHelpSlashReply,
  formatCompactCompleteMessage,
  formatCompactErrorMessage,
  formatSteerErrorMessage,
  formatRenderCommandErrorMessage,
  editNoticeAfterSend,
} from "./conversation/composerSend";

export default function Conversation({
  config,
  activeSessionId,
  onArtifacts,
  onJobChange,
}: {
  config: Config | null;
  activeSessionId: string | null;
  onArtifacts: (a: { type: string; headline: string }[]) => void;
  onJobChange: () => void;
}) {
  const [items, setItems] = useState<Item[]>([]);
  // Mirror of items for session-switch cache writes without stale closures.
  const itemsRef = useRef<Item[]>([]);
  useEffect(() => { itemsRef.current = items; }, [items]);
  // Tracks which session the visible transcript belongs to (for warm-cache save).
  const cachedSessionIdRef = useRef<string | null>(null);
  // Monotonic id so a slow sessionTranscript response for a prior switch is ignored.
  const transcriptLoadGenRef = useRef(0);
  // Busy-poll fingerprint: skip setItems when disk payload matches what's on screen
  // (avoids remounting the whole transcript every 1.5s = periodic blink).
  const transcriptFpRef = useRef("");
  // SSE ownership: ignore late events after detach / session switch.
  const streamSessionIdRef = useRef<string | null>(null);
  const streamGenRef = useRef(0);
  // Mid-turn reattach: last applied /api/chat/events ring cursor (incremental).
  const lastAppliedCursorRef = useRef(0);
  // Ring generation from the last successful chatEvents replay (pin subsequent polls).
  const ringGenerationRef = useRef<number | undefined>(undefined);
  // setInterval handle for light chatEvents poll while detached-busy (no EventSource).
  const chatEventsPollTimerRef = useRef<number | null>(null);
  // Shared live-SSE + reattach event applicator (assigned where handlers live).
  const applyStreamEventRef = useRef<(ev: { kind: string; data?: any }) => void>(() => {});
  const flushTypewriterRef = useRef<() => void>(() => {});
  const maybeRunQueuedResumeRef = useRef<() => void>(() => {});
  const maybeDrainQueueRef = useRef<() => void>(() => {});
  // Session-load effect installs the reattach starter; runners-poll calls it when
  // a turn begins without a local EventSource (e.g. Discord Bridge queue drain).
  const ensureChatEventsReattachRef = useRef<() => void>(() => {});

  const clearChatEventsPoll = () => {
    if (chatEventsPollTimerRef.current != null) {
      window.clearInterval(chatEventsPollTimerRef.current);
      chatEventsPollTimerRef.current = null;
    }
  };

  const [openTabs, setOpenTabs] = useState<{ path: string; isDirty: boolean; line?: number; col?: number }[]>([]);
  const [activeTab, setActiveTab] = useState<string>("chat");
  const [tabContextMenu, setTabContextMenu] = useState<{
    x: number;
    y: number;
    path: string;
  } | null>(null);
  const [repoRoot, setRepoRoot] = useState<string>("");

  const handleCloseTab = (path: string) => {
    const tab = openTabs.find((t) => t.path === path);
    if (tab?.isDirty) {
      if (!window.confirm(`Discard unsaved changes for ${path}?`)) {
        return;
      }
    }
    const nextTabs = openTabs.filter((t) => t.path !== path);
    setOpenTabs(nextTabs);
    if (activeTab === path) {
      setActiveTab("chat");
    }
  };

  const handleCloseOtherTabs = (keepPath: string) => {
    const closing = openTabs.filter((t) => t.path !== keepPath);
    if (closing.some((t) => t.isDirty)) {
      if (!window.confirm("Discard unsaved changes in other tabs?")) return;
    }
    setOpenTabs((prev) => prev.filter((t) => t.path === keepPath));
    setActiveTab(keepPath);
  };

  const handleCloseAllTabs = () => {
    if (openTabs.some((t) => t.isDirty)) {
      if (!window.confirm("Discard unsaved changes in all tabs?")) return;
    }
    setOpenTabs([]);
    setActiveTab("chat");
  };

  const handleTabDirtyChange = (path: string, isDirty: boolean) => {
    setOpenTabs((prev) =>
      prev.map((t) => (t.path === path ? { ...t, isDirty } : t))
    );
  };

  useEffect(() => {
    const handleOpenFile = (e: CustomEvent<{ path: string; line?: number; col?: number }>) => {
      const filePath = e.detail.path;
      if (!filePath) return;
      const line = e.detail.line;
      const col = e.detail.col;
      setOpenTabs((prev) => {
        const exists = prev.some((t) => t.path === filePath);
        if (exists) {
          return prev.map((t) =>
            t.path === filePath ? { ...t, line, col } : t
          );
        }
        return [...prev, { path: filePath, isDirty: false, line, col }];
      });
      setActiveTab(filePath);
    };
    window.addEventListener("harness-open-file", handleOpenFile as EventListener);
    return () => {
      window.removeEventListener("harness-open-file", handleOpenFile as EventListener);
    };
  }, []);

  useEffect(() => {
    const handleDeleted = (e: CustomEvent<{ path: string }>) => {
      const deleted = e.detail?.path;
      if (!deleted) return;
      setOpenTabs((prev) => filterTabsAfterDelete(prev, deleted));
      setActiveTab((cur) => (pathIsUnder(cur, deleted) ? "chat" : cur));
    };
    const handleRenamed = (e: CustomEvent<{ from: string; to: string }>) => {
      const from = e.detail?.from;
      const to = e.detail?.to;
      if (!from || !to) return;
      setOpenTabs((prev) => remapTabsAfterRename(prev, from, to));
      setActiveTab((cur) => remapActiveTabAfterRename(cur, from, to));
    };
    window.addEventListener("harness-file-deleted", handleDeleted as EventListener);
    window.addEventListener("harness-file-renamed", handleRenamed as EventListener);
    return () => {
      window.removeEventListener("harness-file-deleted", handleDeleted as EventListener);
      window.removeEventListener("harness-file-renamed", handleRenamed as EventListener);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const cfg = await api.config();
        if (!cancelled) setRepoRoot(cfg.repo || "");
      } catch {
        /* ignore */
      }
    })();
    const onCfg = () => {
      void api.config().then((cfg) => setRepoRoot(cfg.repo || "")).catch(() => {});
    };
    window.addEventListener("harness-config-changed", onCfg);
    return () => {
      cancelled = true;
      window.removeEventListener("harness-config-changed", onCfg);
    };
  }, []);

  useEffect(() => {
    if (!tabContextMenu) return;
    const handleClose = () => setTabContextMenu(null);
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setTabContextMenu(null);
    };
    window.addEventListener("click", handleClose);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("click", handleClose);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [tabContextMenu]);

  const [input, setInput] = useState("");
  const [status, setStatus] = useState<"idle"|"thinking"|"executing"|"done"|"error"|"streaming">("idle");
  // Wall clock for the live busy footer ("running · read_file · step 3 · 2m 14s").
  // Starts when we enter a busy phase; clears on idle/done/error. A 1s tick keeps
  // the elapsed label honest without re-rendering the whole app on a fast interval.
  const [busyStartedAt, setBusyStartedAt] = useState<number | null>(null);
  const [busyNow, setBusyNow] = useState(() => Date.now());
  useEffect(() => {
    const busy = status === "thinking" || status === "executing" || status === "streaming";
    if (busy) {
      setBusyStartedAt((prev) => prev ?? Date.now());
    } else {
      setBusyStartedAt(null);
    }
  }, [status]);
  useEffect(() => {
    if (busyStartedAt == null) return;
    setBusyNow(Date.now());
    const id = window.setInterval(() => setBusyNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [busyStartedAt]);
  const busyElapsedMs = busyStartedAt != null ? Math.max(0, busyNow - busyStartedAt) : null;
  // Sticky until assistant_done / error / Stop — never infer end-of-turn from
  // transcript shape (mid-turn narration after tools looks like a final answer).
  const [turnOpen, setTurnOpen] = useState(false);
  const agentLoopOpen =
    turnOpen
    || status === "thinking"
    || status === "executing"
    || status === "streaming";
  const liveInvestigation = turnHasLiveInvestigation(items, agentLoopOpen);
  const [waitHint, setWaitHint] = useState<string | null>(null);
  const busyProgress = deriveBusyProgress(items, status, busyElapsedMs, {
    modelLabel: config?.driver || "",
    waitHint,
  });
  // True while visible items belong to a prior session (or are awaiting hydrate).
  // Dims the feed and blocks send so stale A is never treated as B.
  const [transcriptStale, setTranscriptStale] = useState(false);
  const transcriptStaleRef = useRef(false);
  useEffect(() => { transcriptStaleRef.current = transcriptStale; }, [transcriptStale]);
  // T5: pure-chat only — tool turns never early-idle (see turnLooksAnswerComplete).
  const answerChromeIdle =
    !liveInvestigation
    && !turnHasInvestigationActivity(items)
    && !turnOpen
    && turnLooksAnswerComplete(items)
    && (status === "thinking" || status === "streaming");
  // Runner/SSE can briefly report idle while a card is still running (or the
  // reverse). Prefer the investigation / open-turn truth for the header pill.
  const pillStatus: string = derivePillStatus({
    transcriptStale,
    answerChromeIdle,
    liveInvestigation,
    turnOpen,
    status,
  });
  // Same latch as agentLoopOpen — Steer/Stop stay up for the whole turn.
  const composerBusy = agentLoopOpen;
  // True while this Conversation owns a live SSE stream for the active session.
  // Runner-poll busy chrome must not clobber local streaming status, and must
  // not force idle while SSE is still attached.
  const localStreamActiveRef = useRef(false);
  // When we return to a running session without SSE, poll transcript until the
  // runner flips idle, then finalize once.
  const runnerBusyPollGenRef = useRef(0);
  // True while composer busy chrome is driven by runners poll (no local SSE).
  const detachedBusyRef = useRef(false);
  const [auto, setAuto] = useState(false);
  const [plan, setPlan] = useState(false);
  const [distillNotice, setDistillNotice] = useState<string | null>(null);
  const [wikiPrepared, setWikiPrepared] = useState<{ pages: any[]; autoIngested: boolean } | null>(null);
  const [memoryProposals, setMemoryProposals] = useState<
    { id: string; text: string; category: string }[]
  >([]);
  const cancelRef = useRef<null | (() => void)>(null);
  // User hit Stop: suppress runners-poll "thinking" re-arm and keep-alive resume
  // until the next real user send (not an auto pilot_resume).
  const userStoppedRef = useRef(false);
  // True once this turn got a real terminal SSE event (assistant_done / error /
  // auto_halt) or the user hit Stop. When the EventSource dies without that,
  // we surface an explicit abort bubble instead of silently leaving "thinking"
  // with no answer (the "died mid-turn" hang).
  const turnSettledRef = useRef(false);
  const feedRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const planTurnRef = useRef(false);
  // Keep-alive: set when a background swarm finishes (pilot_resume) while a turn
  // is still streaming. The in-flight turn's onDone drains it so the pilot
  // continues automatically instead of going to sleep after dispatching work.
  const resumeQueuedRef = useRef(false);
  // Stable indirection so the always-on swarm-results poll (defined before the
  // trigger) can fire a keep-alive turn without a declaration-order dependency.
  const resumeTriggerRef = useRef<() => void>(() => {});
  // Typewriter buffer: network deltas arrive in bursts (whole sentences at a
  // time). To render smoothly like Cursor/Hermes we DON'T paint on arrival --
  // we queue incoming text here and drain it at a steady per-frame cadence via
  // requestAnimationFrame, so the user sees an even "typing" effect regardless
  // of how chunky the underlying stream is.
  const typeBufRef = useRef<string>("");          // undrained characters
  const typeRafRef = useRef<number | null>(null); // active rAF handle
  const typeDoneRef = useRef<boolean>(false);     // stream ended -> drain fast then stop

  // Cancel any in-flight typewriter rAF on unmount so the loop never leaks.
  useEffect(() => {
    return () => {
      if (typeRafRef.current != null) {
        cancelAnimationFrame(typeRafRef.current);
        typeRafRef.current = null;
      }
    };
  }, []);
  const [msgQueue, setMsgQueue] = useState<{ text: string; auto: boolean; plan?: boolean }[]>([]);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [dragOverIndex, setDragOverIndex] = useState<number | null>(null);

  // PROMPT QUEUE (server-side "playlist"): distinct from the client-only
  // msgQueue above. These items live on the backend and are drained by the
  // harness itself at turn completion (an SSE "queued_prompt" event fires when
  // one starts running) -- so they persist across reloads and survive even if
  // this tab isn't watching. We just mirror the backend list here for display.
  const [queueItems, setQueueItems] = useState<{ id: string; text: string; images?: string[]; model?: string }[]>([]);
  // Ref mirror so the status-transition effect (deps [status]) reads the CURRENT
  // queue when a turn ends, not a stale snapshot, without re-running on poll.
  const queueItemsRef = useRef<{ id: string; text: string; images?: string[]; model?: string }[]>([]);
  useEffect(() => { queueItemsRef.current = queueItems; }, [queueItems]);
  const [queueDragIndex, setQueueDragIndex] = useState<number | null>(null);
  const [queueDragOverIndex, setQueueDragOverIndex] = useState<number | null>(null);

  const [pendingJobIds, setPendingJobIds] = useState<string[]>([]);
  const processedSwarmJobIdsRef = useRef<string[]>([]);
  const [backendPendingSwarms, setBackendPendingSwarms] = useState(false);

  const [attachedImages, setAttachedImages] = useState<{ path: string; name: string; previewUrl: string }[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  // Refs to track every outstanding setTimeout so we can clear them on unmount
  // and avoid state-updates-after-unmount warnings.
  const timeoutsRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());
  const setSafeTimeout = (fn: () => void, ms: number) => {
    const id = setTimeout(() => { timeoutsRef.current.delete(id); fn(); }, ms);
    timeoutsRef.current.add(id);
    return id;
  };

  useEffect(() => {
    return () => {
      timeoutsRef.current.forEach(clearTimeout);
      timeoutsRef.current.clear();
    };
  }, []);

  // Auto-fade upload errors after 6s so a transient failure doesn't sit in the
  // composer looking permanently broken (it used to persist until the next
  // upload attempt). Pass null to clear immediately.
  const flashUploadError = (msg: string | null) => {
    setUploadError(msg);
    if (msg) {
      setSafeTimeout(() => setUploadError((cur) => (cur === msg ? null : cur)), 6000);
    }
  };
  const [lightboxUrl, setLightboxUrl] = useState<string | null>(null);

  // Compacting & Context breakdown states
  const [compactingStatus, setCompactingStatus] = useState<string | null>(null);
  const [showContextPanel, setShowContextPanel] = useState(false);
  const [contextUsage, setContextUsage] = useState<import("../lib/api").ContextUsageResponse | null>(null);

  // Ergonomics states
  const [allFiles, setAllFiles] = useState<string[]>([]);
  const [mentionListingCap, setMentionListingCap] = useState<MentionListingCap | null>(null);
  const [mentionSearch, setMentionSearch] = useState<string | null>(null);
  const [mentionIndex, setMentionIndex] = useState<number>(-1);
  const [filteredFiles, setFilteredFiles] = useState<string[]>([]);
  const [selectedFileIndex, setSelectedFileIndex] = useState<number>(0);
  const [symbolResults, setSymbolResults] = useState<{ name: string; kind: string; path: string; line: number }[]>([]);
  const [codegraphStatus, setCodegraphStatus] = useState<string | null>(null);

  const [slashSearch, setSlashSearch] = useState<string | null>(null);
  const [selectedSlashIndex, setSelectedSlashIndex] = useState<number>(0);

  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editNotice, setEditNotice] = useState<string | null>(null);
  const [canRevertEdit, setCanRevertEdit] = useState(false);
  const [editBusy, setEditBusy] = useState(false);

  const [customCommands, setCustomCommands] = useState<{ name: string; description: string; scope: string }[]>([]);

  const allSlashCommands = mergeSlashCommands(customCommands);

  const fetchCustomCommands = () => {
    api.listCommands()
      .then((res) => {
        if (res && Array.isArray(res.commands)) {
          setCustomCommands(res.commands);
        }
      })
      .catch((err) => {
        console.error("Failed to load custom commands:", err);
      });
  };

  useEffect(() => {
    fetchCustomCommands();
  }, []);

  useEffect(() => {
    if (slashSearch !== null) {
      fetchCustomCommands();
    }
  }, [slashSearch]);

  // PROMPT QUEUE: light refresh -- on mount, on a small poll interval, and
  // after any local mutation (add/remove/reorder/clear). Never throws; a
  // failed fetch just leaves the last-known list on screen.
  const refreshQueue = () => {
    api.queueList()
      .then((res) => {
        if (res && Array.isArray(res.items)) {
          setQueueItems(res.items);
        }
      })
      .catch((err) => {
        console.error("Failed to load prompt queue:", err);
      });
  };

  useEffect(() => {
    refreshQueue();
    const t = window.setInterval(refreshQueue, 3000);
    return () => window.clearInterval(t);
  }, []);

  const moveQueueItem = (index: number, direction: "up" | "down") => {
    if (direction === "up" && index === 0) return;
    if (direction === "down" && index === msgQueue.length - 1) return;
    const targetIndex = direction === "up" ? index - 1 : index + 1;
    setMsgQueue((prev) => {
      const next = [...prev];
      const temp = next[index];
      next[index] = next[targetIndex];
      next[targetIndex] = temp;
      return next;
    });
  };

  const handleDragStart = (idx: number) => {
    setDragIndex(idx);
  };

  const handleDragOver = (e: React.DragEvent, idx: number) => {
    e.preventDefault();
    setDragOverIndex(idx);
  };

  const handleDragLeave = (idx: number) => {
    if (dragOverIndex === idx) {
      setDragOverIndex(null);
    }
  };

  const handleDrop = (e: React.DragEvent, targetIdx: number) => {
    e.preventDefault();
    if (dragIndex === null || dragIndex === targetIdx) {
      setDragIndex(null);
      setDragOverIndex(null);
      return;
    }
    setMsgQueue((prev) => {
      const next = [...prev];
      const [draggedItem] = next.splice(dragIndex, 1);
      next.splice(targetIdx, 0, draggedItem);
      return next;
    });
    setDragIndex(null);
    setDragOverIndex(null);
  };

  const handleDragEnd = () => {
    setDragIndex(null);
    setDragOverIndex(null);
  };

  // PROMPT QUEUE drag-to-reorder. Mirrors the tab reorder pattern in
  // RightPane.tsx (handleDragStart/handleDragOver/handleDragEnd): optimistic
  // local reorder on drop, then persist to the backend; resync from the
  // server on failure so the UI never drifts from what will actually run.
  const handleQueueDragStart = (idx: number) => {
    setQueueDragIndex(idx);
  };

  const handleQueueDragOver = (e: React.DragEvent, idx: number) => {
    e.preventDefault();
    setQueueDragOverIndex(idx);
  };

  const handleQueueDragLeave = (idx: number) => {
    if (queueDragOverIndex === idx) {
      setQueueDragOverIndex(null);
    }
  };

  const handleQueueDrop = (e: React.DragEvent, targetIdx: number) => {
    e.preventDefault();
    const fromIdx = queueDragIndex;
    setQueueDragIndex(null);
    setQueueDragOverIndex(null);
    if (fromIdx === null || fromIdx === targetIdx) return;
    setQueueItems((prev) => {
      const next = [...prev];
      const [dragged] = next.splice(fromIdx, 1);
      next.splice(targetIdx, 0, dragged);
      api.queueReorder(next.map((it) => it.id))
        .catch((err) => {
          console.error("Failed to reorder prompt queue:", err);
          refreshQueue();
        });
      return next;
    });
  };

  const handleQueueDragEnd = () => {
    setQueueDragIndex(null);
    setQueueDragOverIndex(null);
  };

  const handleQueueEdit = (item: { id: string; text: string }) => {
    // Load the prompt back into the composer for editing, and pull it out of
    // the queue -- sending again will re-add it (as a normal turn, not a
    // requeue), matching the existing msgQueue "click to edit" ergonomics.
    setInput(item.text);
    setEditingIndex(null);
    setQueueItems((prev) => prev.filter((it) => it.id !== item.id));
    api.queueRemove(item.id).catch((err) => {
      console.error("Failed to remove queued prompt for edit:", err);
      refreshQueue();
    });
    taRef.current?.focus();
  };

  const handleQueueRemove = (id: string) => {
    setQueueItems((prev) => prev.filter((it) => it.id !== id));
    api.queueRemove(id)
      .then(() => refreshQueue())
      .catch((err) => {
        console.error("Failed to remove queued prompt:", err);
        refreshQueue();
      });
  };

  const handleQueueClearAll = () => {
    setQueueItems([]);
    api.queueClear()
      .then(() => refreshQueue())
      .catch((err) => {
        console.error("Failed to clear prompt queue:", err);
        refreshQueue();
      });
  };

  const handleQueueAdd = () => {
    const text = input.trim();
    if (!text) return;
    // Snapshot the attached image paths BEFORE clearing input/attachments, so a
    // queued prompt carries its images just like a normal turn. The backend
    // delivers them as real image content when the prompt drains.
    const queueImages = attachedImages.map((img) => img.path).filter(Boolean);
    setInput("");
    setAttachedImages([]);
    api.queueAdd(text, queueImages)
      .then(() => refreshQueue())
      .catch((err) => {
        console.error("Failed to add prompt to queue:", err);
      });
  };

  // Request notifications permission on mount
  useEffect(() => {
    const notifyPref = localStorage.getItem("pmharness.notify");
    const isNotifyEnabled = notifyPref !== null ? notifyPref === "true" : true;
    if (isNotifyEnabled && typeof Notification !== "undefined" && Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }
  }, []);

  const triggerCompletionEffects = () => {
    const notifyPref = localStorage.getItem("pmharness.notify");
    const isNotifyEnabled = notifyPref !== null ? notifyPref === "true" : true;

    const soundPref = localStorage.getItem("pmharness.sound");
    const isSoundEnabled = soundPref !== null ? soundPref === "true" : false;

    const isHidden = document.hidden || !document.hasFocus();
    if (isNotifyEnabled && isHidden) {
      if (typeof Notification !== "undefined") {
        if (Notification.permission === "granted") {
          new Notification("Marionette", {
            body: "Run complete",
          });
        } else if (Notification.permission !== "denied") {
          Notification.requestPermission().then((permission) => {
            if (permission === "granted") {
              new Notification("Marionette", {
                body: "Run complete",
              });
            }
          });
        }
      }
    }

    if (isSoundEnabled) {
      try {
        const AudioCtx = window.AudioContext || (window as any).webkitAudioContext;
        if (AudioCtx) {
          const ctx = new AudioCtx();
          const osc = ctx.createOscillator();
          const gain = ctx.createGain();
          osc.type = "sine";
          osc.frequency.setValueAtTime(587.33, ctx.currentTime);
          gain.gain.setValueAtTime(0.08, ctx.currentTime);
          gain.gain.exponentialRampToValueAtTime(0.00001, ctx.currentTime + 0.15);
          osc.connect(gain);
          gain.connect(ctx.destination);
          osc.start();
          osc.stop(ctx.currentTime + 0.15);
        }
      } catch (err) {
        console.error("Failed to play completion sound:", err);
      }
    }
  };

  useEffect(() => {
    if (status === "done" || status === "error") {
      triggerCompletionEffects();
      // Refresh the context-usage badge as soon as a turn ends, so the inline
      // composer % updates live instead of only when the context panel is open
      // or clicked. (The 5s poll only runs while the panel is visible.)
      fetchContextUsage();

      const queuePrefVal = localStorage.getItem("pmharness.queueMessages");
      const isQueueEnabled = queuePrefVal !== null ? queuePrefVal === "true" : true;

      if (isQueueEnabled && msgQueue.length > 0) {
        const nextMsg = msgQueue[0];
        setMsgQueue((prev) => prev.slice(1));
        executeSend(nextMsg.text, nextMsg.auto, nextMsg.plan || false);
      }
      // NOTE: server-side prompt-queue auto-drain is NOT done here. This effect
      // keys on `status` and status is set to "done" on the assistant_done SSE
      // event WHILE the stream is still open (cancelRef still set), then set to
      // "done" AGAIN in the terminal onDone -- which does not re-fire the effect
      // (status unchanged). So the drain lives in maybeDrainQueue(), called from
      // the stream's terminal onDone/onError callbacks, exactly like the
      // maybeRunQueuedResume() keep-alive pattern.
    }
  }, [status]);

  // Auto-scroll to the bottom ONLY when the transcript grows (new
  // messages/tool rows) or when the user is already pinned near the bottom --
  // NOT on in-place mutations like expanding a tool card. Toggling a card open
  // calls setItems (to flip card.open), which used to yank the view to the
  // bottom and force the user to scroll back up to read what they just opened.
  // Stick-to-bottom that RESPECTS the user's scroll. A scroll listener records
  // whether the view is pinned to the bottom; the transcript only auto-follows
  // the live stream while pinned. The moment the user scrolls up to read we stop
  // snapping them back -- following resumes only once they scroll back down to
  // the bottom (which re-pins). A programmatic scroll-to-bottom lands at the
  // bottom, so it never un-pins itself, and there is no fight with the stream.
  const pinnedToBottomRef = useRef(true);
  // Hermes session-switch settle: while true, force-glue to bottom and ignore
  // unpin until layout height stabilizes (avoids ~10 scroll jumps on hydrate).
  const scrollSettlingRef = useRef(false);
  useEffect(() => {
    const el = feedRef.current;
    if (!el) return;
    const onScroll = () => {
      if (scrollSettlingRef.current) {
        pinnedToBottomRef.current = true;
        return;
      }
      pinnedToBottomRef.current =
        el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    };
    // Unpin on the first upward wheel/touch before the next thinking token
    // re-runs stick-to-bottom -- otherwise long reasoning streams keep yanking
    // the feed back to the end and the user cannot scroll the Thought block.
    const onWheel = (e: WheelEvent) => {
      if (scrollSettlingRef.current) return;
      if (e.deltaY < 0) pinnedToBottomRef.current = false;
    };
    let touchY: number | null = null;
    const onTouchStart = (e: TouchEvent) => {
      touchY = e.touches[0]?.clientY ?? null;
    };
    const onTouchMove = (e: TouchEvent) => {
      const y = e.touches[0]?.clientY;
      if (touchY != null && y != null && y > touchY + 2) {
        if (!scrollSettlingRef.current) pinnedToBottomRef.current = false;
      }
      touchY = y ?? touchY;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    el.addEventListener("wheel", onWheel, { passive: true });
    el.addEventListener("touchstart", onTouchStart, { passive: true });
    el.addEventListener("touchmove", onTouchMove, { passive: true });
    return () => {
      el.removeEventListener("scroll", onScroll);
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("touchstart", onTouchStart);
      el.removeEventListener("touchmove", onTouchMove);
    };
  }, []);
  useEffect(() => {
    const el = feedRef.current;
    if (!el) return;
    if (pinnedToBottomRef.current || scrollSettlingRef.current) {
      el.scrollTo(0, el.scrollHeight);
    }
  }, [items]);

  // On session switch: stop follow thrash, glue to true bottom until height is
  // stable for ~5 frames, then re-lock stick-to-bottom (Hermes list.tsx settle).
  useLayoutEffect(() => {
    const el = feedRef.current;
    if (!el || !activeSessionId) return;
    pinnedToBottomRef.current = true;
    scrollSettlingRef.current = true;
    el.scrollTop = el.scrollHeight;
    let frame = 0;
    let stableFrames = 0;
    let lastHeight = el.scrollHeight;
    let rafId = 0;
    const settle = () => {
      const node = feedRef.current;
      if (!node) {
        scrollSettlingRef.current = false;
        return;
      }
      const height = node.scrollHeight;
      stableFrames = height === lastHeight ? stableFrames + 1 : 0;
      lastHeight = height;
      node.scrollTop = height;
      pinnedToBottomRef.current = true;
      if (stableFrames >= 5 || ++frame > 90) {
        scrollSettlingRef.current = false;
        return;
      }
      rafId = requestAnimationFrame(settle);
    };
    rafId = requestAnimationFrame(settle);
    return () => {
      cancelAnimationFrame(rafId);
      scrollSettlingRef.current = false;
    };
  }, [activeSessionId]);

  const fetchContextUsage = () => {
    if (!activeSessionId) return;
    return api.getContextUsage()
      .then((res) => {
        setContextUsage(res);
      })
      .catch((err) => console.error("Failed to fetch context usage:", err));
  };

  useEffect(() => {
    fetchContextUsage();
    
    const h = () => fetchContextUsage();
    window.addEventListener("harness-context-changed", h);
    return () => window.removeEventListener("harness-context-changed", h);
  }, [activeSessionId]);

  usePolling(fetchContextUsage, 5000, { enabled: showContextPanel && !!activeSessionId });

  // Warm-cache session switch: save outgoing transcript, hydrate incoming from
  // cache immediately, detach any open EventSource (backend keeps the turn
  // alive -- do NOT interrupt/stop), then refresh from sessionTranscript in the
  // background without blanking a cache hit.
  //
  // Busy chrome: do NOT force idle on switch. If the target session's runner is
  // still running, keep/show thinking so Stop/Steer stay available (slices B/C/D).
  useEffect(() => {
    const prevId = cachedSessionIdRef.current;
    if (prevId && prevId !== activeSessionId && !transcriptStaleRef.current) {
      // Only cache when the visible rows belong to prevId. Stale bleed (prior
      // session still painted) must not poison the warm cache.
      writeTranscriptCache(prevId, itemsRef.current);
    }

    // Rewind-edit chrome is session-local; never carry Revert/prefill across ids.
    setEditingIndex(null);
    setCanRevertEdit(false);
    setEditNotice(null);
    setEditBusy(false);
    if (prevId && prevId !== activeSessionId) {
      setInput("");
    }

    // Detach SSE only -- closing EventSource is OK; interrupt would kill the turn.
    // Bump streamGen so any late onmessage from the closed stream is ignored.
    streamGenRef.current += 1;
    streamSessionIdRef.current = null;
    if (cancelRef.current) {
      cancelRef.current();
      cancelRef.current = null;
    }
    localStreamActiveRef.current = false;
    detachedBusyRef.current = false;
    // Reset mid-turn reattach cursor/poll so the next session starts clean.
    clearChatEventsPoll();
    lastAppliedCursorRef.current = 0;
    ringGenerationRef.current = undefined;
    // Drop the typewriter loop without flushing into items (would race the
    // cache hydrate below). Authoritative text comes back via sessionTranscript.
    if (typeRafRef.current != null) {
      cancelAnimationFrame(typeRafRef.current);
      typeRafRef.current = null;
    }
    typeBufRef.current = "";
    typeDoneRef.current = false;
    // Intentionally do NOT setStatus("idle") here -- runner poll below decides
    // busy vs idle so a mid-turn session switch keeps Stop/thinking chrome.

    const loadGen = ++transcriptLoadGenRef.current;
    cachedSessionIdRef.current = activeSessionId;

    if (!activeSessionId) {
      // Project/session list may briefly report no active id while the next
      // root's sessions load. Keep prior transcript dimmed instead of flashing
      // the first-run empty placeholder; clear only when there was nothing.
      const emptySwitch = emptySessionSwitchState(itemsRef.current.length);
      if (emptySwitch.clearItems) {
        setItems([]);
      }
      setTranscriptStale(emptySwitch.stale);
      setTurnOpen(false);
      setStatus("idle");
      setCompactingStatus(null);
      return;
    }

    const cachedItems = peekTranscriptCache(activeSessionId);
    const hadCache = cachedItems !== undefined;
    const resolved = resolveSwitchTranscript({
      nextId: activeSessionId,
      cached: cachedItems,
      priorItems: itemsRef.current,
    });
    // Always apply resolved items so a cache miss blanks prior session rows
    // instead of leaving A's transcript painted under B's id.
    setItems(resolved.items);
    itemsRef.current = resolved.items;
    transcriptFpRef.current = transcriptFingerprint(resolved.items);
    setTranscriptStale(resolved.stale);

    // Immediately reflect runner busy state for the session we switched TO
    // (warm cache + Stop chrome) before the background transcript refresh.
    let cancelled = false;
    const applyRunnerBusy = (
      runners: Record<string, "running" | "idle" | "attaching" | "missing"> | undefined,
    ) => {
      if (cancelled || localStreamActiveRef.current) return;
      if (!activeSessionId) return;
      const decision = runnerBusySwitchDecision({
        runnerState: runners?.[activeSessionId],
        localStreamActive: false,
        switchedSession: prevId !== activeSessionId,
      });
      if (decision.kind === "busy") {
        detachedBusyRef.current = true;
        setTurnOpen(true);
        setStatus((prev) => (shouldPreserveBusyStatus(prev) ? prev : "thinking"));
      } else if (decision.kind === "idle") {
        // Idle or cold-attaching: never flash turn-thinking on New Session.
        detachedBusyRef.current = false;
        setTurnOpen(false);
        setStatus("idle");
        setCompactingStatus(null);
      }
    };

    api.getSessionState()
      .then((res) => {
        if (cancelled) return;
        applyRunnerBusy(res?.runners);
      })
      .catch(() => {});

    api.sessionTranscript(activeSessionId)
      .then((res) => {
        if (loadGen !== transcriptLoadGenRef.current) return;
        if (cachedSessionIdRef.current !== activeSessionId) return;

        const loadedItems = transcriptResponseToItems(res);
        setItems(loadedItems);
        itemsRef.current = loadedItems;
        transcriptFpRef.current = transcriptFingerprint(loadedItems);
        writeTranscriptCache(activeSessionId, loadedItems);
        setTranscriptStale(false);

        // Gather all artifacts from (a) card entries in res.display
        const displayArtifacts = collectDisplayArtifacts(res.display);

        const mergeAndEmit = (fetchedArts: { type: string; headline: string }[]) => {
          const unique = mergeUniqueArtifacts(displayArtifacts, fetchedArts);
          if (unique.length > 0) {
            onArtifacts(unique);
          }
        };

        if (res.job_ids && res.job_ids.length > 0) {
          Promise.all(
            res.job_ids.map((jid: string) =>
              api.artifacts(jid)
                .then((arts) => (Array.isArray(arts) ? arts : []))
                .catch((err) => {
                  console.error("Failed to fetch artifacts for job", jid, err);
                  return [];
                })
            )
          ).then((allJobArts) => {
            if (loadGen !== transcriptLoadGenRef.current) return;
            mergeAndEmit(allJobArts.flat());
          });
        } else {
          mergeAndEmit([]);
        }

        // Mid-turn reattach: if the runner is still busy and we have no local
        // EventSource, replay retained SSE frames through the same handler path
        // as live streaming, then lightly poll until the turn settles.
        const reattachSid = activeSessionId;
        const reattachGen = streamGenRef.current;
        const pullChatEvents = async (generationMismatchRetried = false): Promise<boolean> => {
          if (cancelled) return false;
          if (loadGen !== transcriptLoadGenRef.current) return false;
          if (streamGenRef.current !== reattachGen) return false;
          if (cachedSessionIdRef.current !== reattachSid) return false;
          if (localStreamActiveRef.current || userStoppedRef.current) return false;
          try {
            const replay = await api.chatEvents({
              session: reattachSid,
              since: lastAppliedCursorRef.current,
              ...(ringGenerationRef.current != null
                ? { generation: ringGenerationRef.current }
                : {}),
            });
            if (cancelled) return false;
            if (loadGen !== transcriptLoadGenRef.current) return false;
            if (streamGenRef.current !== reattachGen) return false;
            if (cachedSessionIdRef.current !== reattachSid) return false;
            if (localStreamActiveRef.current || userStoppedRef.current) return false;

            if (isChatEventReplayMiss(replay)) {
              const prevGen = ringGenerationRef.current;
              ringGenerationRef.current = ringGenerationAfterReplayMiss(replay, prevGen);
              // Evicted / wrong-generation frames: do not treat as catch-up.
              lastAppliedCursorRef.current = cursorAfterReplayMiss(
                replay,
                lastAppliedCursorRef.current,
              );
              // Busy-poll skips disk refresh while chatEvents poll is armed —
              // hydrate once (per miss) so mid-turn UI does not freeze.
              if (shouldHydrateTranscriptOnReplayMiss(replay)) {
                const missHydrateGen = ++runnerBusyPollGenRef.current;
                const missSid = reattachSid;
                void api.sessionTranscript(missSid).then((tres) => {
                  if (missHydrateGen !== runnerBusyPollGenRef.current) return;
                  if (cancelled) return;
                  if (loadGen !== transcriptLoadGenRef.current) return;
                  if (streamGenRef.current !== reattachGen) return;
                  if (cachedSessionIdRef.current !== missSid) return;
                  if (localStreamActiveRef.current) return;
                  const loadedItems = transcriptResponseToItems(tres);
                  const next = mergeTranscriptItems(itemsRef.current, loadedItems);
                  const fp = transcriptFingerprint(next);
                  if (fp === transcriptFpRef.current) return;
                  transcriptFpRef.current = fp;
                  setItems(next);
                  itemsRef.current = next;
                  writeTranscriptCache(missSid, next);
                  setTranscriptStale(false);
                  // Keep detached-busy chrome; do not clear status / poll.
                }).catch(() => {});
              }
              if (
                replay.code === "generation_mismatch"
                && !generationMismatchRetried
                && ringGenerationRef.current != null
                && ringGenerationRef.current !== prevGen
              ) {
                return pullChatEvents(true);
              }
              return shouldPollChatEvents({
                detachedBusy: detachedBusyRef.current,
                localStreamActive: localStreamActiveRef.current,
                userStopped: userStoppedRef.current,
                sawTerminal: false,
              });
            }

            if (typeof replay.generation === "number" && replay.generation > 0) {
              ringGenerationRef.current = replay.generation;
            }

            let sawTerminal = false;
            const frames = Array.isArray(replay.events) ? replay.events : [];
            for (const frame of frames) {
              if (streamGenRef.current !== reattachGen) return false;
              if (cachedSessionIdRef.current !== reattachSid) return false;
              applyStreamEventRef.current(chatFrameToStreamEvent(frame));
              if (isTerminalStreamKind(frame.kind)) sawTerminal = true;
            }
            if (shouldAdvanceReplayCursor(replay)) {
              lastAppliedCursorRef.current = nextAppliedCursor(
                lastAppliedCursorRef.current,
                frames,
                replay.cursor,
              );
            }

            if (sawTerminal) {
              flushTypewriterRef.current();
              detachedBusyRef.current = false;
              clearChatEventsPoll();
              maybeRunQueuedResumeRef.current();
              maybeDrainQueueRef.current();
              return false;
            }
            return shouldPollChatEvents({
              detachedBusy: detachedBusyRef.current,
              localStreamActive: localStreamActiveRef.current,
              userStopped: userStoppedRef.current,
              sawTerminal: false,
            });
          } catch {
            return shouldPollChatEvents({
              detachedBusy: detachedBusyRef.current,
              localStreamActive: localStreamActiveRef.current,
              userStopped: userStoppedRef.current,
              sawTerminal: false,
            });
          }
        };

        const startChatEventsReattach = async () => {
          if (cancelled || localStreamActiveRef.current || userStoppedRef.current) return;
          let running = detachedBusyRef.current;
          if (!running) {
            try {
              const st = await api.getSessionState();
              if (cancelled) return;
              if (cachedSessionIdRef.current !== reattachSid) return;
              running = st?.runners?.[reattachSid] === "running";
              if (running) {
                detachedBusyRef.current = true;
                setTurnOpen(true);
                setStatus((prev) =>
                  prev === "thinking" || prev === "executing" || prev === "streaming"
                    ? prev
                    : "thinking"
                );
              }
            } catch {
              return;
            }
          }
          if (!running) return;

          const keepPolling = await pullChatEvents();
          if (!keepPolling || cancelled) return;
          if (streamGenRef.current !== reattachGen) return;
          if (chatEventsPollTimerRef.current != null) return;
          chatEventsPollTimerRef.current = window.setInterval(() => {
            void pullChatEvents().then((cont) => {
              if (!cont) clearChatEventsPoll();
            });
          }, CHAT_EVENTS_POLL_MS);
        };
        ensureChatEventsReattachRef.current = () => {
          void startChatEventsReattach();
        };
        void startChatEventsReattach();
      })
      .catch(() => {
        if (loadGen !== transcriptLoadGenRef.current) return;
        if (cachedSessionIdRef.current !== activeSessionId) return;
        // Cache hit: keep showing that session's cached rows on refresh failure.
        // Cache miss: clear — never leave another session's relics on screen.
        if (!hadCache) {
          setItems([]);
          itemsRef.current = [];
          setTranscriptStale(false);
        }
      });

    return () => {
      cancelled = true;
      clearChatEventsPoll();
      ensureChatEventsReattachRef.current = () => {};
    };
  }, [activeSessionId]);

  // Poll runners so composer shows Stop/Steer while the active session's
  // backend runner is busy -- even after SSE detach on session switch.
  usePolling(() => {
    if (!activeSessionId) return;
    if (localStreamActiveRef.current) return;
    if (userStoppedRef.current) {
      // Stop must stick: ignore runners=running while the abandoned generator
      // unwinds; keep chrome idle until the user sends again.
      detachedBusyRef.current = false;
      clearChatEventsPoll();
      setStatus((prev) =>
        prev === "thinking" || prev === "executing" || prev === "streaming"
          ? "idle"
          : prev
      );
      return;
    }
    const sid = activeSessionId;
    return api.getSessionState().then((res) => {
      if (cachedSessionIdRef.current !== sid || localStreamActiveRef.current) return;
      if (userStoppedRef.current) return;
      const runners = res?.runners || {};
      const running = runners[sid] === "running";
      if (running) {
        detachedBusyRef.current = true;
        setTurnOpen(true);
        setStatus((prev) =>
          prev === "thinking" || prev === "executing" || prev === "streaming"
            ? prev
            : "thinking"
        );
        // Queue/bridge turns start without this tab's EventSource. Arm the
        // chatEvents ring poll so tokens paint live (not only after restart).
        if (
          shouldArmChatEventsFromRunners({
            runnerBusy: true,
            localStreamActive: localStreamActiveRef.current,
            userStopped: userStoppedRef.current,
            chatEventsPollArmed: chatEventsPollTimerRef.current != null,
          })
        ) {
          ensureChatEventsReattachRef.current();
          return;
        }
        // While chatEvents reattach poll owns mid-turn UI, skip disk replace
        // that would wipe in-flight deltas not yet persisted.
        if (chatEventsPollTimerRef.current != null) return;
        // Slice C: while detached-but-busy, refresh transcript so eventual
        // dump lands without blanking thinking chrome.
        const pollGen = ++runnerBusyPollGenRef.current;
        return api.sessionTranscript(sid).then((tres) => {
          if (pollGen !== runnerBusyPollGenRef.current) return;
          if (cachedSessionIdRef.current !== sid) return;
          if (localStreamActiveRef.current) return;
          const loadedItems = transcriptResponseToItems(tres);
          const local = itemsRef.current;
          const next = mergeTranscriptItems(local, loadedItems);
          const fp = transcriptFingerprint(next);
          // Identical payload: keep existing object identities so React does not
          // remount every Investigated/card row (the periodic blink).
          if (fp === transcriptFpRef.current) return;
          transcriptFpRef.current = fp;
          setItems(next);
          itemsRef.current = next;
          writeTranscriptCache(sid, next);
          setTranscriptStale(false);
        }).catch(() => {});
      } else if (detachedBusyRef.current) {
        // Runner went idle after a detached busy view -- finalize + refresh.
        // Do not clear busy chrome while live tool rows are still painted;
        // a lagging runners map was wiping Investigating → idle mid-command.
        if (turnHasLiveInvestigation(itemsRef.current, true)) {
          return;
        }
        detachedBusyRef.current = false;
        clearChatEventsPoll();
        setTurnOpen(false);
        setStatus("idle");
        setCompactingStatus(null);
        return api.sessionTranscript(sid)
          .then((tres) => {
            if (cachedSessionIdRef.current !== sid) return;
            if (localStreamActiveRef.current) return;
            const loadedItems = transcriptResponseToItems(tres);
            const next = mergeTranscriptItems(itemsRef.current, loadedItems);
            const fp = transcriptFingerprint(next);
            if (fp === transcriptFpRef.current) return;
            transcriptFpRef.current = fp;
            setItems(next);
            itemsRef.current = next;
            writeTranscriptCache(sid, next);
            setTranscriptStale(false);
          })
          .catch(() => {});
      }
    });
  }, 1500, { enabled: !!activeSessionId });

  useEffect(() => {
    setPendingJobIds([]);
    processedSwarmJobIdsRef.current = [];
    setBackendPendingSwarms(false);
    if (activeSessionId) {
      api.getSessionState()
        .then((res) => {
          if (res) {
            setBackendPendingSwarms(res.pending_swarms);
            // resume_pending is an EXPLICIT one-shot latch from the self-edit
            // restart path (backend /api/session/persist or /api/restart) -- NOT
            // "transcript ends on a user turn". Only schedule auto-resume when
            // the freshly-fetched state says so; mere session open/switch must
            // never ghost-continue a past unanswered message.
            if (res.resume_pending) {
              setSafeTimeout(() => resumeTriggerRef.current(), 300);
            }
          }
        })
        .catch(() => {});
    }
  }, [activeSessionId]);

  const setCard = (id: string, patch: Partial<Card>) =>
    setItems((prev) => patchCardInItems(prev, id, patch));

  useEffect(() => {
    const onFocus = () => { taRef.current?.focus(); };
    window.addEventListener("harness-focus-input", onFocus);
    return () => window.removeEventListener("harness-focus-input", onFocus);
  }, []);

  // Auto-grow textarea (Cursor-like). Keep overflow hidden until we hit the
  // max height -- overflow-y-auto on an empty/short field paints a useless
  // Windows classic scrollbar gutter inside the rounded composer.
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    const contentH = ta.scrollHeight;
    const maxH = 200;
    ta.style.height = Math.min(contentH, maxH) + "px";
    ta.style.overflowY = contentH > maxH ? "auto" : "hidden";
  }, [input]);

  // Load workspace files for @-mention dropdown
  useEffect(() => {
    api.getWorkspaceFiles()
      .then((res) => {
        if (res && res.files) {
          setAllFiles(res.files);
          setMentionListingCap(
            res.truncated
              ? { total: res.total, capped: res.capped }
              : null,
          );
        }
      })
      .catch((err) => {
        console.error("Failed to load workspace files:", err);
      });
  }, [activeSessionId]);

  // Filter files based on @-mention search text
  useEffect(() => {
    if (mentionSearch !== null) {
      const query = mentionSearch.toLowerCase();
      const filtered = allFiles.filter(f => f.toLowerCase().includes(query)).slice(0, 10);
      setFilteredFiles(filtered);
      setSelectedFileIndex(0);
    } else {
      setFilteredFiles([]);
    }
  }, [mentionSearch, allFiles]);

  // Fetch symbol suggestions with debounce to avoid hammering
  useEffect(() => {
    if (mentionSearch !== null && mentionSearch.trim().length >= 1) {
      const delayDebounceFn = setTimeout(() => {
        api.searchSymbols(mentionSearch)
          .then((res) => {
            if (res) {
              setSymbolResults(res.symbols || []);
              if (res.status) {
                setCodegraphStatus(res.status);
              }
            }
          })
          .catch((err) => {
            console.error("Failed to search symbols:", err);
            setSymbolResults([]);
          });
      }, 150);

      return () => clearTimeout(delayDebounceFn);
    } else {
      setSymbolResults([]);
    }
  }, [mentionSearch]);

  // Keep selectedFileIndex bounded within combined total mentions count
  useEffect(() => {
    const total = filteredFiles.length + symbolResults.length;
    if (selectedFileIndex >= total && total > 0) {
      setSelectedFileIndex(total - 1);
    }
  }, [filteredFiles, symbolResults, selectedFileIndex]);

  const insertMention = (fileName: string) => {
    if (mentionIndex === -1) return;
    const before = input.slice(0, mentionIndex);
    const after = input.slice(taRef.current?.selectionStart || mentionIndex);
    const completed = before + "@" + fileName + " " + after;
    setInput(completed);
    setMentionSearch(null);
    setMentionIndex(-1);
    
    setTimeout(() => {
      if (taRef.current) {
        taRef.current.focus();
        const cursorPosition = mentionIndex + fileName.length + 2; // +1 for @, +1 for space
        taRef.current.setSelectionRange(cursorPosition, cursorPosition);
      }
    }, 10);
  };

  const insertSymbol = (symbolName: string) => {
    if (mentionIndex === -1) return;
    const before = input.slice(0, mentionIndex);
    const after = input.slice(taRef.current?.selectionStart || mentionIndex);
    const completed = before + "@symbol:" + symbolName + " " + after;
    setInput(completed);
    setMentionSearch(null);
    setMentionIndex(-1);
    
    setTimeout(() => {
      if (taRef.current) {
        taRef.current.focus();
        const cursorPosition = mentionIndex + symbolName.length + 9; // +1 for @, +7 for symbol:, +1 for space
        taRef.current.setSelectionRange(cursorPosition, cursorPosition);
      }
    }, 10);
  };

  const insertSlashCommand = (cmd: string) => {
    setInput(cmd + " ");
    setSlashSearch(null);
    
    setTimeout(() => {
      if (taRef.current) {
        taRef.current.focus();
        taRef.current.setSelectionRange(cmd.length + 1, cmd.length + 1);
      }
    }, 10);
  };

  const handleInputChange = (val: string, cursorPosition: number) => {
    setInput(val);
    
    // Detect Slash Command trigger: input starts with '/' and cursor is within the command
    if (val.startsWith("/") && !val.includes("\n") && cursorPosition <= val.length) {
      const spaceIdx = val.indexOf(" ");
      if (spaceIdx === -1 || cursorPosition <= spaceIdx) {
        setSlashSearch(val.slice(1));
        setMentionSearch(null);
        setMentionIndex(-1);
        return;
      }
    }
    setSlashSearch(null);

    // Detect Mention trigger
    const lastAt = val.lastIndexOf("@", cursorPosition - 1);
    if (lastAt !== -1) {
      const prefix = lastAt === 0 ? "" : val[lastAt - 1];
      if (prefix === "" || /\s/.test(prefix)) {
        const textAfterAt = val.slice(lastAt + 1, cursorPosition);
        if (!/\s/.test(textAfterAt)) {
          setMentionSearch(textAfterAt);
          setMentionIndex(lastAt);
          return;
        }
      }
    }
    setMentionSearch(null);
    setMentionIndex(-1);
  };

  const handlePaste = async (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items) return;

    let addedCount = attachedImages.length;
    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      if (item.type.startsWith("image/")) {
        const file = item.getAsFile();
        if (file) {
          e.preventDefault(); // prevent pasting binary junk text
          if (addedCount >= 8) {
            flashUploadError("Maximum 8 images allowed per message");
            continue;
          }
          setUploadError(null);
          try {
            const previewUrl = URL.createObjectURL(file);
            const uploaded = await api.uploadImage(file);
            setAttachedImages((prev) => {
              if (prev.length >= 8) {
                return prev;
              }
              return [
                ...prev,
                { path: uploaded.path, name: uploaded.name, previewUrl }
              ];
            });
            addedCount++;
          } catch (err) {
            console.error("Failed to upload pasted image:", err);
            flashUploadError("Image upload failed");
          }
        }
      }
    }
  };

  const handleComposerDragOver = (e: React.DragEvent) => {
    if (e.dataTransfer.types.includes("Files")) {
      e.preventDefault();
      e.stopPropagation();
      try { e.dataTransfer.dropEffect = "copy"; } catch {}
      setIsDragOver(true);
    }
  };

  const handleComposerDragLeave = () => {
    setIsDragOver(false);
  };

  const handleComposerDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragOver(false);
    const files = Array.from(e.dataTransfer.files);
    if (files.length === 0) return;

    setUploadError(null);
    const repo = (config?.repo || "").replace(/\/+$/, "");
    const mentions: string[] = [];
    let addedCount = attachedImages.length;

    for (const file of files) {
      const isImage = file.type.startsWith("image/");
      // Electron exposes the real OS path on dropped files; the browser does not.
      const osPath: string = (file as any).path || "";

      if (isImage) {
        // Images attach as visual context (upload + thumbnail), as before.
        if (addedCount >= 8) {
          flashUploadError("Maximum 8 images allowed per message");
          continue;
        }
        try {
          const previewUrl = URL.createObjectURL(file);
          const uploaded = await api.uploadImage(file);
          setAttachedImages((prev) => {
            if (prev.length >= 8) return prev;
            return [...prev, { path: uploaded.path, name: uploaded.name, previewUrl }];
          });
          addedCount++;
        } catch (err) {
          console.error("Failed to upload dropped image:", err);
          flashUploadError("Image upload failed");
        }
        continue;
      }

      // Non-image files become an @-mention the agent reads. If the file lives
      // INSIDE the open workspace, use a plain repo-relative @path (the backend
      // resolves it directly). Otherwise upload it into the workspace-readable
      // store and reference the uploaded path -- so external drops work too.
      const insideRepo = osPath && repo && (osPath === repo || osPath.startsWith(repo + "/"));
      if (insideRepo) {
        const rel = osPath.slice(repo.length + 1);
        // The backend mention regex matches @<path> tokens without spaces; a path
        // with spaces is uploaded instead so it resolves reliably.
        if (!/\s/.test(rel)) {
          mentions.push(`@${rel}`);
          continue;
        }
      }
      try {
        const uploaded = await api.uploadImage(file); // generic file upload endpoint
        // uploaded.path is absolute; the backend reads it back for the mention.
        const rel = repo && uploaded.path.startsWith(repo + "/")
          ? uploaded.path.slice(repo.length + 1)
          : uploaded.path;
        if (!/\s/.test(rel)) mentions.push(`@${rel}`);
        else flashUploadError("Dropped file path has spaces -- rename and retry");
      } catch (err) {
        console.error("Failed to upload dropped file:", err);
        flashUploadError("File upload failed");
      }
    }

    if (mentions.length > 0) {
      setInput((prev) => {
        const sep = prev && !prev.endsWith(" ") ? " " : "";
        return prev + sep + mentions.join(" ") + " ";
      });
      setTimeout(() => taRef.current?.focus(), 10);
    }
  };

  const handleEditMessage = (idx: number, originalText: string) => {
    if (composerBusy || editBusy) {
      setEditNotice("Stop the current turn before editing a prior message.");
      return;
    }
    // Count user messages before this items-index so UI-only rows (thinking,
    // steer, etc.) do not skew the backend display ordinal.
    const userOrdinal = items
      .slice(0, idx)
      .filter((it) => it.kind === "msg" && it.msg.role === "user").length;

    setEditBusy(true);
    api.rewindSession(userOrdinal)
      .then((res) => {
        if (!res?.ok) {
          setEditNotice(res?.error || "Could not rewind transcript for edit.");
          return;
        }
        // Truncate the visible transcript to the same spot; message reappears
        // when the user resubmits. Revert restores the stashed tail.
        setItems((prev) => prev.slice(0, idx));
        setEditingIndex(idx);
        setInput(res.prefill || originalText);
        setCanRevertEdit(true);
        setEditNotice(res.notice || "Editing — resubmit, or Revert to restore.");
        setTimeout(() => taRef.current?.focus(), 10);
      })
      .catch((err) => {
        setEditNotice((err as Error)?.message || "Rewind failed.");
      })
      .finally(() => setEditBusy(false));
  };

  const handleRevertEdit = () => {
    if (editBusy) return;
    setEditBusy(true);
    api.restoreRewind()
      .then((res) => {
        if (!res?.ok) {
          setEditNotice(res?.error || "Nothing to revert.");
          return;
        }
        const restored = transcriptResponseToItems({
          display: res.display,
          history: res.history,
        });
        setItems(restored);
        writeTranscriptCache(activeSessionId || "", restored);
        setEditingIndex(null);
        setInput("");
        setCanRevertEdit(false);
        setEditNotice(null);
      })
      .catch((err) => {
        setEditNotice((err as Error)?.message || "Revert failed.");
      })
      .finally(() => setEditBusy(false));
  };

  const handleCancelEdit = () => {
    if (canRevertEdit) {
      handleRevertEdit();
      return;
    }
    setEditingIndex(null);
    setInput("");
    setEditNotice(null);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Escape") {
      if (mentionSearch !== null || slashSearch !== null) {
        setMentionSearch(null);
        setMentionIndex(-1);
        setSlashSearch(null);
        e.preventDefault();
        return;
      }
      if (editingIndex !== null || canRevertEdit) {
        handleCancelEdit();
        e.preventDefault();
        return;
      }
    }

    const totalMentions = filteredFiles.length + symbolResults.length;
    if (mentionSearch !== null && totalMentions > 0) {
      if (e.key === "ArrowDown") {
        setSelectedFileIndex((prev) => (prev + 1) % totalMentions);
        e.preventDefault();
        return;
      }
      if (e.key === "ArrowUp") {
        setSelectedFileIndex((prev) => (prev - 1 + totalMentions) % totalMentions);
        e.preventDefault();
        return;
      }
      if (e.key === "Enter") {
        if (selectedFileIndex < filteredFiles.length) {
          insertMention(filteredFiles[selectedFileIndex]);
        } else {
          const symIdx = selectedFileIndex - filteredFiles.length;
          if (symbolResults[symIdx]) {
            insertSymbol(symbolResults[symIdx].name);
          }
        }
        e.preventDefault();
        return;
      }
    }

    if (slashSearch !== null) {
      const matchingSlash = allSlashCommands.filter(s => s.cmd.toLowerCase().startsWith("/" + slashSearch.toLowerCase()));
      if (matchingSlash.length > 0) {
        if (e.key === "ArrowDown") {
          setSelectedSlashIndex((prev) => (prev + 1) % matchingSlash.length);
          e.preventDefault();
          return;
        }
        if (e.key === "ArrowUp") {
          setSelectedSlashIndex((prev) => (prev - 1 + matchingSlash.length) % matchingSlash.length);
          e.preventDefault();
          return;
        }
        if (e.key === "Enter") {
          insertSlashCommand(matchingSlash[selectedSlashIndex].cmd);
          e.preventDefault();
          return;
        }
      }
    }

    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      const busy = status === "thinking" || status === "executing" || status === "streaming";
      // While a turn is running, plain Enter STEERS (redirects the current turn);
      // Cmd/Ctrl+Enter QUEUES (runs after the current turn finishes). When idle,
      // Enter always sends a normal turn.
      if (composerEnterAction({ busy, metaOrCtrl: e.metaKey || e.ctrlKey }) === "queue") {
        handleQueueAdd();
        return;
      }
      send();
    }
  };

  const handleSwarmResult = (d: any) => {
    const job_id = d.job_id;
    if (!job_id) return;

    if (processedSwarmJobIdsRef.current.includes(job_id)) return;
    processedSwarmJobIdsRef.current.push(job_id);

    setPendingJobIds((p) => p.filter(id => id !== job_id));

    setItems((prevItems) => applySwarmResultToItems(prevItems, d));
  };

  const swarmResultsPending = pendingJobIds.length > 0 || backendPendingSwarms;
  // Guarded via usePolling: each tick fires two sequential backend calls
  // (results + session state), so during a swarm this was the single heaviest
  // always-on poller. The in-flight guard keeps at most one round-trip pair
  // outstanding instead of stacking them onto an already-busy backend.
  usePolling(
    () =>
      api.getSwarmResults()
        .then((res) => {
          if (res && res.results && res.results.length > 0) {
            // At most one triggerResume per poll tick (first pilot_resume wins;
            // extras only set resumeQueuedRef). Mid-stream path already coalesces.
            let pollResumeFired = false;
            res.results.forEach((evt) => {
              const anyEvt = evt as any;
              if (anyEvt.kind === "swarm_result" && anyEvt.data) {
                handleSwarmResult(anyEvt.data);
              } else if (anyEvt.kind === "pilot_resume") {
                // Background job finished while the session was idle. The backend
                // already extended history with the result + continuation; kick
                // off a keep-alive turn so the pilot continues without a prompt.
                if (!pollResumeFired) {
                  pollResumeFired = true;
                  resumeTriggerRef.current();
                } else {
                  resumeQueuedRef.current = true;
                }
              } else if (anyEvt.kind === "distilled" && anyEvt.data) {
                const notice = formatDistilledNotice(anyEvt.data);
                if (notice) {
                  setDistillNotice(notice);
                  setSafeTimeout(() => setDistillNotice((cur) => (cur === notice ? null : cur)), 8000);
                }
              } else if (anyEvt.kind === "wiki_prepared" && anyEvt.data) {
                const d = anyEvt.data;
                const pages = d.pages || [];
                if (pages.length > 0) {
                  if (d.auto_ingested) {
                    const notice = formatWikiAutoIngestNotice(pages.length);
                    setDistillNotice(notice);
                    setSafeTimeout(() => setDistillNotice((cur) => (cur === notice ? null : cur)), 8000);
                  } else {
                    setWikiPrepared({ pages, autoIngested: false });
                  }
                }
              } else if (anyEvt.kind === "memory_propose" && anyEvt.data) {
                const d = anyEvt.data;
                const id = d.id || "";
                const text = (d.text || "").trim();
                if (id && text) {
                  setMemoryProposals((prev) => (
                    prev.some((p) => p.id === id)
                      ? prev
                      : [...prev, { id, text, category: d.category || "general" }]
                  ));
                }
              }
            });
          }
          return api.getSessionState();
        })
        .then((stateRes) => {
          if (stateRes) {
            setBackendPendingSwarms(stateRes.pending_swarms);
          }
        })
        .catch((err) => {
          console.error("Failed to poll swarm results:", err);
        }),
    2500,
    { enabled: swarmResultsPending },
  );

  // Append decoded text to the streaming assistant bubble (one state update).
  // findStreamingBubbleIdx scans back past decoration items so mid-drain
  // thinking/tool events do not split the stream into a second bubble.
  const appendStreamingText = (chunk: string) => {
    if (!chunk) return;
    setItems((p) => appendStreamingTextToItems(p, chunk, { isPlan: planTurnRef.current }));
  };

  // Drain the typewriter buffer at a steady cadence. While the stream is live we
  // reveal a fixed number of chars per frame (smooths bursty network arrival);
  // once the stream has ended we accelerate so we never lag behind the model.
  const pumpTypewriter = () => {
    typeRafRef.current = null;
    const buf = typeBufRef.current;
    if (!buf) {
      if (!typeDoneRef.current) typeRafRef.current = requestAnimationFrame(pumpTypewriter);
      return;
    }
    // Reveal speed scales with backlog (see typewriterCharsPerFrame).
    const perFrame = typewriterCharsPerFrame(buf.length, typeDoneRef.current);
    const take = buf.slice(0, perFrame);
    typeBufRef.current = buf.slice(perFrame);
    appendStreamingText(take);
    if (typeBufRef.current || !typeDoneRef.current) {
      typeRafRef.current = requestAnimationFrame(pumpTypewriter);
    }
  };

  const startTypewriter = () => {
    typeDoneRef.current = false;
    if (typeRafRef.current == null) {
      typeRafRef.current = requestAnimationFrame(pumpTypewriter);
    }
  };

  // Flush any buffered text immediately + stop the loop (on done/error/finalize).
  const flushTypewriter = () => {
    typeDoneRef.current = true;
    if (typeBufRef.current) {
      appendStreamingText(typeBufRef.current);
      typeBufRef.current = "";
    }
    if (typeRafRef.current != null) {
      cancelAnimationFrame(typeRafRef.current);
      typeRafRef.current = null;
    }
  };
  flushTypewriterRef.current = flushTypewriter;

  // Shared path for live SSE and mid-turn chatEvents reattach. Callers must
  // enforce session/generation guards before invoking.
  // Item transforms live in conversation/streamApply.ts (pure); chrome/side
  // effects stay here.
  const applyStreamEvent = (ev: { kind: string; data?: any }) => {
      const d = ev.data || {};
      if (ev.kind === "compacting") {
        setCompactingStatus(d.message || "Summarizing chat context");
      } else if (ev.kind === "command_blocked") {
        setItems((p) => appendCommandBlocked(p, d));
      } else if (ev.kind === "swarm_auth_failure") {
        // A provider rejected the API key. Surface it as a loud, persistent
        // banner so a dead/revoked key is never silently read as a generic
        // "completed without findings" degrade. Deduped by action id.
        setItems((p) => appendAuthFailure(p, d.message || "", d.id));
      } else if (ev.kind === "wiki_prepared") {
        const pages = d.pages || [];
        if (pages.length > 0) {
          if (d.auto_ingested) {
            // Silent-auto mode already ingested -- just a quiet confirmation footnote.
            const notice = formatWikiAutoIngestNotice(pages.length);
            setDistillNotice(notice);
            setSafeTimeout(() => setDistillNotice((cur) => (cur === notice ? null : cur)), 8000);
          } else {
            // Prepare-and-approve: surface the pages for one-click ingest.
            setWikiPrepared({ pages, autoIngested: false });
          }
        }
      } else if (ev.kind === "memory_propose") {
        // Non-blocking Save/Skip after the final answer. Does not affect
        // composer busy state; ignore/dismiss is fine.
        const id = d.id || "";
        const text = (d.text || "").trim();
        if (id && text) {
          setMemoryProposals((prev) => (
            prev.some((p) => p.id === id)
              ? prev
              : [...prev, { id, text, category: d.category || "general" }]
          ));
        }
      } else if (ev.kind === "codegraph_context") {
        setItems((p) => appendCodegraphContext(p, d.symbols || 0, d.query || ""));
      } else if (ev.kind === "compaction") {
        setCompactingStatus(null);
        setItems((p) => appendCompaction(p, d.before_tokens, d.after_tokens));
        window.dispatchEvent(new Event("harness-context-changed"));
      } else if (ev.kind === "notice" && (d.kind === "wait" || !d.kind)) {
        const hint = truncateWaitHint(d.message || "");
        if (hint) setWaitHint(hint);
      } else if (ev.kind === "thinking") {
        // Live reasoning deltas (delta:true) paint mid-turn so GLM/OR token
        // climbs are visible. Full post-answer reasoning dumps (no delta) stay
        // suppressed -- the answer is already on screen.
        setCompactingStatus(null);
        const { painting, chunk } = shouldPaintThinking(d);
        if (!painting) return;
        setStatus((prev) =>
          prev === "streaming" || prev === "executing" ? prev : "thinking"
        );
        if (d.delta && chunk) {
          setItems((p) => upsertStreamingThinking(p, chunk));
        } else if (chunk.trim()) {
          setItems((p) => appendNonStreamingThinking(p, chunk));
        }
      } else if (ev.kind === "tool_prep") {
        const name = String(d.name || "").trim();
        const callId = String(d.id || "").trim();
        if (!name && !callId) return;
        setCompactingStatus(null);
        setStatus((prev) =>
          prev === "streaming" || prev === "executing" ? prev : "thinking"
        );
        setItems((p) =>
          upsertToolPrep(p, name || "tool_call", {
            goal: d.goal != null ? String(d.goal) : undefined,
            id: callId || undefined,
            status: d.status != null ? String(d.status) : undefined,
          })
        );
      } else if (ev.kind === "message_delta") {
        setCompactingStatus(null);
        setStatus("streaming");
        // Ensure a streaming bubble exists. When the turn already has tool
        // cards (Cursor CLI / investigation), paint deltas instantly — the
        // typewriter over an open Investigating fold reads as chat "loading
        // from top to bottom" after hard commands. Bare prose turns still
        // use the cadence typewriter.
        const investigating = turnHasLiveInvestigation(itemsRef.current, true);
        setItems((p) => ensureAssistantStreamingBubble(p, { isPlan: planTurnRef.current }));
        const chunk = d.text || "";
        if (!chunk) return;
        if (investigating) {
          flushTypewriter();
          appendStreamingText(chunk);
        } else {
          typeBufRef.current += chunk;
          startTypewriter();
        }
      } else if (ev.kind === "worker_delta") {
        // Live token stream from an inline swarm worker (the agentic adapter).
        // This is an EPHEMERAL preview: it renders in a height-capped, auto-
        // scrolling window (see Bubble) and is dropped when the action finalizes,
        // because the worker's real output arrives as swarm artifacts/summary.
        // Reuse only a prior workerStream bubble -- never merge worker tokens into
        // the pilot's own message bubble, and never let several workers pile into
        // one unbounded permanent bubble.
        if (d.kind === "text" && d.text) {
          setCompactingStatus(null);
          setStatus("streaming");
          setItems((p) => ensureWorkerStreamingBubble(p, { isPlan: planTurnRef.current }));
          typeBufRef.current += (d.text || "");
          startTypewriter();
        }
      } else if (ev.kind === "message") {
        setCompactingStatus(null);
        setStatus("thinking");
        // Drain any queued typed text before finalizing, so the bubble is whole.
        flushTypewriter();
        setItems((p0) => finalizePilotMessage(p0, d.text, { isPlan: planTurnRef.current }));
      } else if (ev.kind === "action_start") {
        setCompactingStatus(null);
        setStatus("executing");
        // Idempotent: a late/replayed action_start with the same id must not
        // stack another card (session-switch SSE race → infinite Investigated).
        // Default tool cards to collapsed always: they used to mount open while
        // running and snap shut on action_result, which read as a flicker.
        setItems((p) => appendActionStartCard(p, d));
      } else if (ev.kind === "action_result") {
        setCompactingStatus(null);
        setStatus("thinking");
        // The swarm is done: its structured artifacts/summary land below. Drop
        // the ephemeral worker-stream PREVIEW entirely -- do not convert it into
        // a trailing "reasoning" row (that duplicated the answer and burned
        // scroll/attention). A non-worker streaming bubble (the pilot's own
        // narration) is still finalized in place.
        flushTypewriter();
        setItems((p) => finalizeStreamingBubbleOnActionResult(p));
        // Fallback: if the card carries an auth_failure but the dedicated
        // swarm_auth_failure event was missed, still raise the loud banner so a
        // dead key is never buried in a quiet "completed" card. Deduped by id.
        if (d.auth_failure) {
          setItems((p) => appendAuthFailure(p, d.auth_failure, d.id));
        }
        setCard(d.id, { running: false, open: false, result: d });
        if (d.artifacts && !d.error) onArtifacts(d.artifacts);
        onJobChange();
        setItems((prev) => {
          const cardItem = prev.find((it) => it.kind === "card" && it.card.id === d.id);
          if (
            cardItem
            && cardItem.kind === "card"
            && (cardItem.card.kind === "open_project" || cardItem.card.kind === "relocate_session")
            && !d.error
          ) {
            window.dispatchEvent(new Event("harness-config-changed"));
            // Prefer the resolved path from action_result (workspace_root/path).
            // Card.goal can be "(workspace root)" when relocate used
            // workspace_root without path — that used to skip the left-rail
            // expand/refresh and leave the project invisible until a tab flip.
            const root = workspaceRootFromActionResult(d, cardItem.card.goal);
            if (root && root !== "(workspace root)") {
              window.dispatchEvent(new CustomEvent("harness-session-relocated", {
                detail: { workspace_root: root },
              }));
            }
          }
          return prev;
        });
      } else if (ev.kind === "auto_status") {
        setStatus("executing");
      } else if (ev.kind === "distilled") {
        // Only surface self-learning when it produced something WORTH the user's
        // attention -- a newly PROPOSED skill or rule(s). Skips, duplicates, and
        // "insufficient findings" are the 99% case and stay silent (they are not
        // actionable; announcing them is pure noise).
        const notice = formatDistilledNotice(d);
        if (notice) {
          setDistillNotice(notice);
          // Quiet footnote: auto-fade after 8s so it never lingers like a push notif.
          setSafeTimeout(() => setDistillNotice((cur) => (cur === notice ? null : cur)), 8000);
        }
      } else if (ev.kind === "auto_halt") {
        turnSettledRef.current = true;
        setTurnOpen(false);
        setStatus("done");
        setItems((p) => appendAutoHalt(p, d.reason || ""));
      } else if (ev.kind === "swarm_pending") {
        const job_ids = d.job_ids || [];
        setPendingJobIds((p) => [...p, ...job_ids]);
        setItems((p) => appendSwarmPending(p, job_ids, d.objective || ""));
      } else if (ev.kind === "checkpoint") {
        setItems((p) => appendCheckpoint(p, d));
        window.dispatchEvent(new Event("harness-repo-mutated"));
      } else if (ev.kind === "swarm_result") {
        handleSwarmResult(d);
      } else if (ev.kind === "pilot_resume") {
        // A background job finished and the backend injected a continuation into
        // history. Queue a keep-alive turn; it fires from this turn's onDone.
        resumeQueuedRef.current = true;
      } else if (ev.kind === "queued_prompt") {
        // The backend drained one item off the server-side prompt queue and
        // started running it as the next turn IN THE SAME STREAM. It already
        // appended the prompt to history, but the transcript UI never saw a
        // user message for it -- so the queued turn ran invisibly (no "you
        // said X" bubble). Render the user bubble here so the auto-run queued
        // prompt shows up in chat exactly like a normally-sent turn. Then
        // refetch so the chip list drops it and promotes the next item.
        if (d.text) {
          const qImgs: string[] = Array.isArray(d.images) ? d.images : [];
          setItems((p) => appendQueuedPromptUserBubble(p, d.text, qImgs));
        }
        refreshQueue();
      } else if (ev.kind === "assistant_done") {
        turnSettledRef.current = true;
        setTurnOpen(false);
        setWaitHint(null);
        setStatus("done");
        setItems((p) => finalizeStreamingThinking(p));
        fetchContextUsage();
        // Backend may also set_title_if_default; refresh meters/title if the
        // optimistic first-send rename missed or the server derived a different slug.
        window.dispatchEvent(new Event("harness-config-changed"));
      } else if (ev.kind === "error") {
        turnSettledRef.current = true;
        setTurnOpen(false);
        setCompactingStatus(null);
        setWaitHint(null);
        setStatus("error");
        setItems((p) => appendStreamError(p, d.error || ""));
      }
  };
  applyStreamEventRef.current = applyStreamEvent;

  const executeSend = (msg: string, useAuto: boolean, usePlan: boolean = false, resume: boolean = false, imagesOverride?: { path: string; name: string; previewUrl: string }[]) => {
    // Stale transcript = prior session still on screen while B hydrates.
    // Never send into the wrong session.
    const gate = executeSendGate({
      transcriptStale,
      resume,
      userStopped: userStoppedRef.current,
    });
    if (gate === "stale") return;
    if (gate === "stopped_resume") {
      // Keep-alive after Stop must not re-arm the turn.
      resumeQueuedRef.current = false;
      return;
    }
    if (!resume) {
      // Real user/autopilot send clears the Stop hold so thinking can run again.
      userStoppedRef.current = false;
    }
    planTurnRef.current = usePlan;
    turnSettledRef.current = false;
    // imagesOverride lets the idle queue-drain path (maybeDrainQueue) carry a
    // queued prompt's image attachments even though they were never placed in
    // the live attachedImages composer state.
    const imgsToSend = resume ? [] : (imagesOverride ? imagesOverride : [...attachedImages]);
    const imgPaths = imgsToSend.map((img) => img.path);
    if (!resume) {
      // A resume turn carries no new user message -- the pilot is continuing off
      // a finished background job, so we don't add a user bubble or send images.
      setAttachedImages([]);
      setItems((p) => [...p, { kind: "msg", msg: { role: "user", text: msg, images: imgsToSend } }]);
      const hasPriorUserTurn = itemsRef.current.some(
        (it) => it.kind === "msg" && it.msg.role === "user",
      );
      if (activeSessionId && msg.trim() && !hasPriorUserTurn) {
        void renameDefaultSessionIfNeeded(
          activeSessionId,
          msg,
          repoRoot || config?.repo,
        );
      }
    }
    setTurnOpen(true);
    setStatus("thinking");
    const streamer = resume
      ? (cb: any, done: any, err: any) => api.resume(cb, done, err)
      : useAuto
      ? (cb: any, done: any, err: any) => api.auto(msg, cb, done, err)
      : (cb: any, done: any, err: any) => api.chat(msg, cb, done, err, usePlan, imgPaths);
    clearChatEventsPoll();
    localStreamActiveRef.current = true;
    detachedBusyRef.current = false;
    const streamSid = activeSessionId;
    const streamGen = ++streamGenRef.current;
    streamSessionIdRef.current = streamSid;
    const streamLive = () =>
      streamGenRef.current === streamGen
      && streamSessionIdRef.current === streamSid
      && cachedSessionIdRef.current === streamSid;
    cancelRef.current = streamer((ev: any) => {
      // Drop late events after session switch / SSE detach so tool cards from
      // session A never append onto B (bleed) or re-append onto A (infinite
      // Investigated repeats while the busy poll also replaces from disk).
      if (!streamLive()) return;
      applyStreamEvent(ev);
    }, () => {
         if (!streamLive()) return;
         flushTypewriter();
         // Stream closed without assistant_done / error / Stop -- explicit abort
         // so the UI never looks like a silent hang after "thinking".
         if (!turnSettledRef.current && !userStoppedRef.current) {
           turnSettledRef.current = true;
           setTurnOpen(false);
           setStatus("error");
           setItems((p) => [...p, {
             kind: "msg",
             msg: {
               role: "assistant",
               text: "[aborted] Connection closed before the turn finished. Send again to retry.",
             },
           }]);
         } else {
           setTurnOpen(false);
           setStatus("done");
         }
         cancelRef.current = null;
         localStreamActiveRef.current = false;
         setCompactingStatus(null);
         maybeRunQueuedResume();
         maybeDrainQueue();
       },
       () => {
         if (!streamLive()) return;
         flushTypewriter();
         if (!turnSettledRef.current && !userStoppedRef.current) {
           turnSettledRef.current = true;
           setTurnOpen(false);
           setItems((p) => [...p, {
             kind: "msg",
             msg: {
               role: "assistant",
               text: "[aborted] Connection closed before the turn finished. Send again to retry.",
             },
           }]);
           setStatus("error");
         } else if (!userStoppedRef.current) {
           // EventSource often fires onerror when the stream closes after a
           // normal assistant_done -- do not paint a false error over success.
           setTurnOpen(false);
           setStatus((prev) => (prev === "error" ? prev : "done"));
         }
         cancelRef.current = null;
         localStreamActiveRef.current = false;
         setCompactingStatus(null);
         maybeRunQueuedResume();
         maybeDrainQueue();
       });
  };

  // AUTO-QUEUE ("playlist") from idle: the backend auto-drains the server-side
  // prompt queue only WITHIN a running turn's completion loop. When a turn ends
  // and the session goes IDLE with items still queued (the user lined up a
  // playlist while nothing ran, or added items after the turn ended), nothing
  // would kick off the next one. Fire it here -- from the stream's TERMINAL
  // callback (cancelRef already nulled), so it never collides with the still-open
  // stream. Pop the next item, remove it server-side, and send it as a normal
  // turn. Each turn's terminal callback re-invokes this, so the whole ordered
  // queue drains by itself, one turn after the next. Resume takes priority: if a
  // background-job continuation is pending, let it run first (it re-enters here
  // when it finishes).
  const maybeDrainQueue = () => {
    if (cancelRef.current) return;            // a turn is (re)starting -- not idle
    if (resumeQueuedRef.current) return;      // keep-alive continuation wins
    const next = queueItemsRef.current[0];
    if (!next || !next.text) return;
    setSafeTimeout(() => {
      if (cancelRef.current || resumeQueuedRef.current) return;
      setQueueItems((prev) => prev.filter((it) => it.id !== next.id));
      queueItemsRef.current = queueItemsRef.current.filter((it) => it.id !== next.id);
      api.queueRemove(next.id).catch(() => {}).finally(() => refreshQueue());
      const nextImgs = (next.images || []).map((p: string) => ({
        path: p,
        name: (p.split(/[\\/]/).pop() || p),
        previewUrl: p,
      }));
      // Per-item model stamp (Hermes-style): apply before kicking the turn so a
      // playlist queued under deepseek does not run under a later kimi pick.
      const kick = async () => {
        const stamped = next.model;
        if (stamped) {
          try {
            await api.swapPilot(stamped);
            window.dispatchEvent(new Event("harness-config-changed"));
          } catch {
            /* best-effort; stream start also reconciles _cfg vs live pilot */
          }
        }
        executeSendRef.current(next.text, auto, plan, false, nextImgs);
      };
      void kick();
    }, 60);
  };
  maybeDrainQueueRef.current = maybeDrainQueue;

  // Keep-alive driver: after a turn ends, if a background swarm finished while it
  // was running (resumeQueuedRef), fire a continuation turn so the pilot assesses
  // the result and takes the next step on its own -- no user prompt, no autopilot.
  // Chains naturally: each continuation can dispatch more work whose completion
  // queues the next resume, so the pilot "runs run runs" until the work is done.
  const maybeRunQueuedResume = () => {
    if (userStoppedRef.current) {
      resumeQueuedRef.current = false;
      return;
    }
    if (!resumeQueuedRef.current) return;
    // Still busy? Leave the flag set -- the next turn's onDone (or the poll) will
    // pick it up. Only clear it once we've actually committed to running.
    if (cancelRef.current) return;
    resumeQueuedRef.current = false;
    setSafeTimeout(() => {
      if (userStoppedRef.current || cancelRef.current) {
        if (!userStoppedRef.current) resumeQueuedRef.current = true;
        return;
      }
      executeSendRef.current("", false, false, true);
    }, 60);
  };
  maybeRunQueuedResumeRef.current = maybeRunQueuedResume;

  // A pilot_resume can also arrive via the swarm-results poll while the session is
  // idle (the common background-job case). Trigger a continuation immediately.
  const triggerResume = () => {
    if (userStoppedRef.current) {
      resumeQueuedRef.current = false;
      return;
    }
    if (cancelRef.current) { resumeQueuedRef.current = true; return; }
    executeSendRef.current("", false, false, true);
  };
  resumeTriggerRef.current = triggerResume;

  const send = () => {
    const msg = input.trim();
    // Allow a send/steer that is only attached image(s) with no text -- the
    // backend accepts text OR images.
    if (shouldBlockEmptySend({
      transcriptStale,
      text: msg,
      imageCount: attachedImages.length,
    })) return;

    // Intercept slash commands locally
    if (msg.startsWith("/")) {
      const parts = msg.split(/\s+/);
      const cmd = parts[0];
      
      if (cmd === "/clear" || cmd === "/new") {
        setInput("");
        setEditingIndex(null);
        window.dispatchEvent(new Event("harness-new-session"));
        return;
      }
      
      if (cmd === "/compact") {
        setInput("");
        setEditingIndex(null);
        setStatus("thinking");
        setItems((p) => [...p, { kind: "thinking", text: "Compacting session context on backend...", id: newThinkingId() }]);
        api.compactSession()
          .then((res) => {
            setStatus("done");
            setItems((p) => [
              ...p,
              {
                kind: "msg",
                msg: {
                  role: "assistant",
                  text: formatCompactCompleteMessage(res.before_tokens, res.after_tokens),
                }
              }
            ]);
          })
          .catch((err) => {
            setStatus("error");
            setItems((p) => [
              ...p,
              {
                kind: "msg",
                msg: {
                  role: "assistant",
                  text: formatCompactErrorMessage(err),
                }
              }
            ]);
          });
        return;
      }
      
      if (cmd === "/model") {
        setInput("");
        setEditingIndex(null);
        window.dispatchEvent(new Event("harness-open-model-picker"));
        return;
      }
      
      if (cmd === "/help") {
        setInput("");
        setEditingIndex(null);
        const helpText = formatHelpSlashReply(allSlashCommands);
        setItems((p) => [
          ...p,
          {
            kind: "msg",
            msg: {
              role: "assistant",
              text: helpText
            }
          }
        ]);
        return;
      }

      const isBuiltIn = isBuiltInSlashCommand(cmd);
      if (!isBuiltIn) {
        const customCmdName = cmd.startsWith("/") ? cmd.slice(1) : cmd;
        const isCustom = customCommands.some(c => c.name === customCmdName);
        if (isCustom) {
          const restOfLine = msg.substring(cmd.length).trim();
          setStatus("thinking");
          api.renderCommand(customCmdName, restOfLine)
            .then((res) => {
              setStatus("done");
              setInput(res.prompt);
              setEditingIndex(null);
              setTimeout(() => {
                if (taRef.current) {
                  taRef.current.focus();
                }
              }, 10);
            })
            .catch((err) => {
              setStatus("error");
              setItems((p) => [
                ...p,
                {
                  kind: "msg",
                  msg: {
                    role: "assistant",
                    text: formatRenderCommandErrorMessage(err),
                  }
                }
              ]);
            });
          return;
        }
      }
    }

    // After a rewind-edit, clear the editing chrome but keep Revert available
    // so the user can restore the prior branch (Hermes/Cursor pattern).
    setEditingIndex(null);
    setEditNotice(editNoticeAfterSend(canRevertEdit));

    if (composerBusy) {
      // Snapshot the attached image paths BEFORE clearing input/attachments or
      // making the async call, so we never read a stale/cleared closure value
      // and images are never silently dropped from the steer request. The
      // backend transcribes them into the steer text.
      const steerImages = attachedImages.map((img) => img.path).filter(Boolean);
      setInput("");
      setAttachedImages([]);
      api.steerSession(msg, steerImages)
        .then(() => {
          setItems((prev) => [...prev, { kind: "steer", text: msg }]);
        })
        .catch((err) => {
          console.error("Failed to steer session:", err);
          setItems((prev) => [
            ...prev,
            {
              kind: "msg",
              msg: {
                role: "assistant",
                text: formatSteerErrorMessage(err),
              }
            }
          ]);
        });
      return;
    }

    setInput("");
    executeSend(msg, auto, plan);
  };

  const stop = () => {
    userStoppedRef.current = true;
    turnSettledRef.current = true;
    resumeQueuedRef.current = false;
    detachedBusyRef.current = false;
    clearChatEventsPoll();
    // Invalidate in-flight reattach pulls / late SSE frames.
    streamGenRef.current += 1;
    cancelRef.current?.();
    cancelRef.current = null;
    localStreamActiveRef.current = false;
    flushTypewriter();
    setTurnOpen(false);
    setStatus("idle");
    setCompactingStatus(null);
    api.interruptSession().catch((e) => console.error("Failed to interrupt session on backend:", e));
  };

  // PERF: Stabilize the callbacks handed to the memoized TranscriptList. The
  // underlying functions (handleEditMessage, executeSend, ...) are recreated on
  // every render, which would defeat React.memo. We route through refs holding
  // the latest implementation and expose useCallback wrappers with EMPTY deps,
  // so the prop identities never change across renders -- keeping the memo
  // boundary intact even while `input`/streaming state churns in the parent.
  const handleEditMessageRef = useRef(handleEditMessage);
  handleEditMessageRef.current = handleEditMessage;
  const executeSendRef = useRef(executeSend);
  executeSendRef.current = executeSend;
  const setCardRef = useRef(setCard);
  setCardRef.current = setCard;

  const stableEditMessage = useCallback(
    (idx: number, originalText: string) => handleEditMessageRef.current(idx, originalText),
    []
  );
  const stableExecuteSend = useCallback(
    (msg: string, useAuto: boolean, usePlan?: boolean) => executeSendRef.current(msg, useAuto, usePlan),
    []
  );
  const stableSetCard = useCallback(
    (id: string, patch: Partial<Card>) => setCardRef.current(id, patch),
    []
  );
  const handleTranscriptImageClick = useCallback((url: string) => setLightboxUrl(url), []);
  const handleTranscriptExecutePlan = useCallback((planText: string) => {
    setAuto(true);
    setPlan(false);
    executeSendRef.current(
      "Execute the following approved plan. Implement it fully, using run_implement/run_parallel as needed:\n\n" + planText,
      true,
      false
    );
  }, []);

  return (
    <main className="flex flex-col h-full min-w-0 bg-transparent">
      {/* Brand + idle share equal inset so they line up with the floating dock. */}
      <header
        className="flex items-center justify-between border-b border-edge/60 shrink-0 px-6"
        style={{ paddingTop: 8, paddingBottom: 7, WebkitAppRegion: "drag" } as React.CSSProperties}
      >
        <span className="flex items-baseline gap-1.5 select-none min-w-0" style={{ WebkitAppRegion: "no-drag" } as React.CSSProperties}>
          <span className="font-semibold text-[12px] text-txt/90 tracking-tight">Marionette</span>
          <span className="text-faint/70 text-[9px] font-normal">|</span>
          <span className="text-muted/80 text-[9px] font-medium tracking-wide uppercase truncate">
            The Puppetmaster Harness
          </span>
        </span>
        <div className="shrink-0" style={{ WebkitAppRegion: "no-drag" } as React.CSSProperties}>
          <StatusPill
            status={pillStatus}
            detail={
              !transcriptStale && !answerChromeIdle && pillStatus !== "idle" && busyProgress.label
                ? busyProgress.pill
                : undefined
            }
          />
        </div>
      </header>

      {openTabs.length > 0 && (
        <div className="flex items-center gap-1 px-4 bg-panel border-b border-edge h-9 shrink-0 overflow-x-auto scrollbar-none select-none">
          <button
            onClick={() => setActiveTab("chat")}
            className={`flex items-center h-full px-3 text-[12px] font-medium transition-colors border-b-2 ${
              activeTab === "chat"
                ? "border-accent text-accent bg-bg/50"
                : "border-transparent text-muted hover:text-txt"
            }`}
          >
            Chat
          </button>
          {openTabs.map((t) => {
            const filename = t.path.split(/[/\\]/).pop() || t.path;
            const isSelected = activeTab === t.path;
            return (
              <div
                key={t.path}
                className={`flex items-center h-full px-2 text-[12px] font-medium transition-colors border-b-2 group relative ${
                  isSelected
                    ? "border-accent text-accent bg-bg/50"
                    : "border-transparent text-muted hover:text-txt"
                }`}
                onContextMenu={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setTabContextMenu({ x: e.clientX, y: e.clientY, path: t.path });
                }}
              >
                <button
                  onClick={() => setActiveTab(t.path)}
                  className="flex items-center gap-1.5 h-full max-w-[150px]"
                  title={t.path}
                >
                  {t.isDirty && (
                    <span className="w-1.5 h-1.5 rounded-full bg-warn shrink-0" />
                  )}
                  <span className="truncate">{filename}</span>
                </button>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    handleCloseTab(t.path);
                  }}
                  className="ml-2 p-0.5 rounded hover:bg-panel2 text-muted hover:text-txt opacity-60 group-hover:opacity-100 transition-opacity"
                >
                  <X size={10} />
                </button>
              </div>
            );
          })}
        </div>
      )}

      {tabContextMenu && (
        <div
          className="fixed z-50 bg-panel border border-edge rounded shadow-lg text-[12px] py-1 min-w-[160px]"
          style={{ top: tabContextMenu.y, left: tabContextMenu.x }}
          onClick={(e) => e.stopPropagation()}
        >
          <button
            onClick={async () => {
              const path = tabContextMenu.path;
              setTabContextMenu(null);
              const res = await revealWorkspacePath(repoRoot, path);
              if (!res.ok) {
                window.dispatchEvent(
                  new CustomEvent("harness-toast", {
                    detail: res.error || "Could not reveal path",
                  }),
                );
              }
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            {revealInFolderLabel()}
          </button>
          <button
            onClick={async () => {
              const path = tabContextMenu.path;
              setTabContextMenu(null);
              const abs = toAbsoluteWorkspacePath(repoRoot, path);
              try {
                await navigator.clipboard.writeText(abs);
                window.dispatchEvent(new CustomEvent("harness-toast", { detail: "Path copied" }));
              } catch {
                window.dispatchEvent(new CustomEvent("harness-toast", { detail: "Could not copy path" }));
              }
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            Copy Path
          </button>
          <button
            onClick={async () => {
              const path = tabContextMenu.path;
              setTabContextMenu(null);
              try {
                await navigator.clipboard.writeText(path.replace(/\\/g, "/"));
                window.dispatchEvent(new CustomEvent("harness-toast", { detail: "Relative path copied" }));
              } catch {
                window.dispatchEvent(new CustomEvent("harness-toast", { detail: "Could not copy path" }));
              }
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            Copy Relative Path
          </button>
          <div className="border-t border-edge my-1" />
          <button
            onClick={() => {
              const path = tabContextMenu.path;
              setTabContextMenu(null);
              handleCloseTab(path);
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            Close
          </button>
          <button
            onClick={() => {
              const path = tabContextMenu.path;
              setTabContextMenu(null);
              handleCloseOtherTabs(path);
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            Close others
          </button>
          <button
            onClick={() => {
              setTabContextMenu(null);
              handleCloseAllTabs();
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            Close all
          </button>
        </div>
      )}

      {activeTab === "chat" ? (
        <div className="flex flex-col flex-1 min-h-0">
          <div ref={feedRef} className={`flex-1 overflow-y-auto ${panelOpacityClass(transcriptStale)}`}>
        <div className="max-w-3xl mx-auto px-6 py-6 flex flex-col gap-1">
          {items.length === 0 && !transcriptStale && (
            <div className="text-muted text-[13px] mt-32 text-center leading-relaxed">
              Message the pilot. It plans, investigates via swarms, and explains.
            </div>
          )}
          {transcriptStale && items.length === 0 && (
            <div className="text-muted text-[13px] mt-32 text-center leading-relaxed">
              Loading session…
            </div>
          )}
          {/*
            PERF: The transcript is rendered by TranscriptList, a React.memo
            component whose props are deliberately independent of the composer
            `input` state. Because typing only mutates `input` (which lives in
            this parent) and none of TranscriptList's props change per keystroke,
            React skips re-rendering the transcript on every keystroke. This
            breaks the old coupling where items.map ran on the ENTIRE transcript
            for each character typed (cost grew with message count).
          */}
          <TranscriptList
            items={items}
            status={status}
            compactingStatus={compactingStatus}
            editingIndex={editingIndex}
            auto={auto}
            plan={plan}
            busyElapsedMs={busyElapsedMs}
            turnOpen={turnOpen}
            scrollContainerRef={feedRef}
            onEditMessage={stableEditMessage}
            onExecuteSend={stableExecuteSend}
            onImageClick={handleTranscriptImageClick}
            onSetCard={stableSetCard}
            onExecutePlan={handleTranscriptExecutePlan}
          />
        </div>
      </div>

      <div className="px-6 pb-3 pt-0.5">
        <div className="max-w-3xl mx-auto">
          {wikiPrepared && wikiPrepared.pages.length > 0 && (
            <div className="mb-2 px-2.5 py-1.5 rounded-lg bg-accent/5 border border-accent/20 flex items-center gap-2 text-[11px] text-txt/85">
              <Share2 size={11} className="text-accent shrink-0" />
              <span className="flex-1">
                Wiki: {wikiPrepared.pages.length} structured page{wikiPrepared.pages.length === 1 ? "" : "s"} ready
                <span className="text-faint"> ({wikiPrepared.pages.map((p: any) => p.kind).filter((v: any, i: number, a: any[]) => a.indexOf(v) === i).join(", ")})</span>
              </span>
              <button
                onClick={async () => {
                  const pages = wikiPrepared.pages;
                  setWikiPrepared(null);
                  try {
                    const res = await api.wikiIngestPrepared(pages);
                    const notice = `Wiki: ${res.ingested} page${res.ingested === 1 ? "" : "s"} ingested`;
                    setDistillNotice(notice);
                    setSafeTimeout(() => setDistillNotice((cur) => (cur === notice ? null : cur)), 6000);
                  } catch {
                    setDistillNotice("Wiki ingest failed");
                  }
                }}
                className="shrink-0 px-2 py-0.5 rounded bg-accent/15 hover:bg-accent/25 text-accent font-medium transition text-[10.5px]"
              >
                Ingest
              </button>
              <button
                onClick={() => setWikiPrepared(null)}
                className="shrink-0 text-faint/60 hover:text-muted transition"
                title="dismiss"
              >
                x
              </button>
            </div>
          )}
          {memoryProposals.length > 0 && (
            <div className="mb-2 space-y-1.5">
              {memoryProposals.map((prop) => (
                <div
                  key={prop.id}
                  className="px-2.5 py-1.5 rounded-lg bg-accent/5 border border-accent/20 flex items-start gap-2 text-[11px] text-txt/85"
                >
                  <Brain size={11} className="text-accent shrink-0 mt-0.5" />
                  <div className="flex-1 min-w-0">
                    <div className="text-faint text-[10px] mb-0.5">
                      Save to durable memory?
                      <span className="ml-1 text-muted">({prop.category})</span>
                    </div>
                    <div className="truncate italic">&ldquo;{prop.text}&rdquo;</div>
                  </div>
                  <button
                    onClick={async () => {
                      setMemoryProposals((prev) => prev.filter((p) => p.id !== prop.id));
                      try {
                        const res = await api.memoryProposeAccept(prop.id);
                        if (res.ok) {
                          const notice = "Memory saved";
                          setDistillNotice(notice);
                          setSafeTimeout(() => setDistillNotice((cur) => (cur === notice ? null : cur)), 4000);
                        }
                      } catch {
                        setDistillNotice("Memory save failed");
                      }
                    }}
                    className="shrink-0 px-2 py-0.5 rounded bg-accent/15 hover:bg-accent/25 text-accent font-medium transition text-[10.5px]"
                  >
                    Save
                  </button>
                  <button
                    onClick={async () => {
                      setMemoryProposals((prev) => prev.filter((p) => p.id !== prop.id));
                      try {
                        await api.memoryProposeDismiss(prop.id);
                      } catch {
                        /* ignore -- card already dismissed locally */
                      }
                    }}
                    className="shrink-0 px-2 py-0.5 rounded text-faint hover:text-muted transition text-[10.5px]"
                  >
                    Skip
                  </button>
                </div>
              ))}
            </div>
          )}
          {distillNotice && (
            <div className="mb-2 px-1 flex items-center gap-2 text-[10.5px] text-faint/80">
              <span className="flex-1 truncate">
                {distillNotice}
              </span>
              <button
                onClick={() => setDistillNotice(null)}
                className="text-faint/50 hover:text-muted transition shrink-0"
                title="dismiss"
              >
                x
              </button>
            </div>
          )}
          {msgQueue.length > 0 && (
            <div className="mb-3 space-y-1.5">
              <div className="flex items-center justify-between mb-1 px-1">
                <span className="text-[10px] uppercase tracking-wider text-faint font-semibold">
                  Queued ({msgQueue.length})
                </span>
                <button
                  onClick={() => setMsgQueue([])}
                  className="text-[10px] text-faint hover:text-muted transition font-semibold"
                >
                  Clear all
                </button>
              </div>
              {msgQueue.map((qm, idx) => {
                const isDragOver = dragOverIndex === idx;
                const isDragging = dragIndex === idx;

                return (
                  <div
                    key={idx}
                    draggable
                    onDragStart={() => handleDragStart(idx)}
                    onDragOver={(e) => handleDragOver(e, idx)}
                    onDragLeave={() => handleDragLeave(idx)}
                    onDrop={(e) => handleDrop(e, idx)}
                    onDragEnd={handleDragEnd}
                    className={`flex items-center justify-between bg-panel2/60 border rounded-lg px-3 py-1.5 text-[12px] text-muted transition-all duration-150 select-none
                      ${isDragging ? "opacity-40" : ""}
                      ${isDragOver ? "border-accent/40 bg-accent/5" : "border-edge/60 hover:border-edge2"}`}
                  >
                    <div className="flex items-center gap-2 min-w-0 flex-1">
                      {/* Grip handle */}
                      <div className="text-faint hover:text-muted cursor-grab active:cursor-grabbing flex items-center justify-center p-0.5">
                        <GripVertical size={12} />
                      </div>
                      {/* Position number */}
                      <span className="text-faint text-[10px] font-mono select-none">
                        {idx + 1}
                      </span>
                      {/* Message text with Click-to-edit */}
                      <span
                        onClick={() => {
                          setInput(qm.text);
                          setAuto(qm.auto);
                          setPlan(qm.plan || false);
                          setMsgQueue((prev) => prev.filter((_, i) => i !== idx));
                          taRef.current?.focus();
                        }}
                        title="Click to edit message"
                        className="truncate max-w-md cursor-pointer hover:text-txt hover:underline transition-colors select-none"
                      >
                        {qm.text}
                      </span>
                      {/* Badges */}
                      {qm.plan && (
                        <span className="text-[9px] uppercase font-bold px-1.5 py-0.5 bg-accent/15 text-accent rounded whitespace-nowrap">
                          plan
                        </span>
                      )}
                      {qm.auto && (
                        <span className="text-[9px] uppercase font-bold px-1.5 py-0.5 bg-warn/15 text-warn rounded whitespace-nowrap">
                          auto
                        </span>
                      )}
                    </div>

                    {/* Controls (Up / Down / Cancel) */}
                    <div className="flex items-center gap-1 ml-2 flex-shrink-0">
                      <button
                        onClick={() => moveQueueItem(idx, "up")}
                        disabled={idx === 0}
                        title="Move up"
                        className="p-1 rounded text-faint hover:text-muted hover:bg-panel border border-transparent hover:border-edge/40 disabled:opacity-30 disabled:pointer-events-none transition-all"
                      >
                        <ChevronUp size={12} />
                      </button>
                      <button
                        onClick={() => moveQueueItem(idx, "down")}
                        disabled={idx === msgQueue.length - 1}
                        title="Move down"
                        className="p-1 rounded text-faint hover:text-muted hover:bg-panel border border-transparent hover:border-edge/40 disabled:opacity-30 disabled:pointer-events-none transition-all"
                      >
                        <ChevronDown size={12} />
                      </button>
                      <button
                        onClick={() => {
                          setMsgQueue((prev) => prev.filter((_, i) => i !== idx));
                        }}
                        title="Cancel/Remove"
                        className="p-1 rounded text-faint hover:text-risk hover:bg-risk/10 border border-transparent hover:border-risk/20 transition-all"
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          {/* Server-side PROMPT QUEUE, stacked ABOVE the composer (Cursor-style)
              so the "runs next" items are always visible right over the input.
              These prompts are drained by the backend one full turn at a time. */}
          {queueItems.length > 0 && (
            <div className="mb-2 space-y-1">
              <div className="flex items-center justify-between px-1">
                <span className="text-[10px] uppercase tracking-wider text-faint font-semibold">
                  {queueItems.length} queued to send
                </span>
                {queueItems.length >= 2 && (
                  <button
                    onClick={handleQueueClearAll}
                    className="text-[10px] text-faint hover:text-muted transition font-semibold"
                  >
                    Clear all
                  </button>
                )}
              </div>
              {queueItems.map((item, idx) => {
                const isDragging = queueDragIndex === idx;
                const isDragOverQ = queueDragOverIndex === idx;
                return (
                  <div
                    key={item.id}
                    draggable
                    onDragStart={() => handleQueueDragStart(idx)}
                    onDragOver={(e) => handleQueueDragOver(e, idx)}
                    onDragLeave={() => handleQueueDragLeave(idx)}
                    onDrop={(e) => handleQueueDrop(e, idx)}
                    onDragEnd={handleQueueDragEnd}
                    className={`flex items-center gap-2 bg-panel2/60 border rounded-lg px-2.5 py-1 text-[11px] text-muted transition-all duration-150 select-none
                      ${isDragging ? "opacity-40" : ""}
                      ${isDragOverQ ? "border-accent/40 bg-accent/5" : "border-edge/60 hover:border-edge2"}`}
                  >
                    <div className="text-faint hover:text-muted cursor-grab active:cursor-grabbing flex items-center justify-center shrink-0">
                      <GripVertical size={11} />
                    </div>
                    {idx === 0 && (
                      <span
                        title="Runs next"
                        className="shrink-0 text-[9px] uppercase font-bold px-1 py-0.5 bg-accent/15 text-accent rounded"
                      >
                        next
                      </span>
                    )}
                    <span
                      onClick={() => handleQueueEdit(item)}
                      title={item.text}
                      className="truncate flex-1 min-w-0 cursor-pointer hover:text-txt hover:underline transition-colors"
                    >
                      {item.text}
                    </span>
                    {item.images && item.images.length > 0 && (
                      <span
                        title={`${item.images.length} image attachment(s)`}
                        className="shrink-0 flex items-center gap-0.5 text-[9px] font-semibold px-1 py-0.5 bg-panel border border-edge/60 text-faint rounded"
                      >
                        <ImageIcon size={9} />{item.images.length}
                      </span>
                    )}
                    <button
                      onClick={() => handleQueueRemove(item.id)}
                      title="Remove from queue"
                      className="p-0.5 rounded text-faint hover:text-risk hover:bg-risk/10 border border-transparent hover:border-risk/20 transition-all shrink-0"
                    >
                      <X size={11} />
                    </button>
                  </div>
                );
              })}
            </div>
          )}
          {/* compact composer: input + a single tidy control row */}
          <WorkspaceChip />
          <div
            onDragOver={handleComposerDragOver}
            onDragLeave={handleComposerDragLeave}
            onDrop={handleComposerDrop}
            className={`relative bg-panel2/80 border rounded-2xl focus-within:border-edge2 shadow-lg shadow-black/20 transition ${
              isDragOver ? "border-accent ring-1 ring-accent" : "border-edge"
            }`}
          >
            {/* Editing / Revert chrome (Hermes-style rewind) */}
            {(editingIndex !== null || canRevertEdit || editNotice) && (
              <div className="flex items-center justify-between gap-2 px-3.5 py-1.5 bg-panel border-b border-edge text-[11.5px] text-accent select-none rounded-t-2xl">
                <span className="flex items-center gap-1.5 min-w-0">
                  <Pencil size={11} className="shrink-0" />
                  <span className="truncate">
                    {editingIndex !== null
                      ? (editNotice || `Editing message #${editingIndex + 1}`)
                      : (editNotice || "Prior turns set aside")}
                  </span>
                </span>
                <span className="flex items-center gap-1 shrink-0">
                  {canRevertEdit && (
                    <button
                      type="button"
                      disabled={editBusy}
                      onClick={() => handleRevertEdit()}
                      className="text-accent hover:text-txt transition font-semibold text-[10px] px-1.5 py-0.5 rounded border border-accent/40 bg-accent/10 hover:bg-accent/20 disabled:opacity-50"
                      title="Restore the conversation from before this edit"
                    >
                      Revert?
                    </button>
                  )}
                  {editingIndex !== null && (
                    <button
                      type="button"
                      disabled={editBusy}
                      onClick={() => handleCancelEdit()}
                      className="text-faint hover:text-muted transition font-medium text-[10px] px-1.5 py-0.5 rounded border border-edge bg-panel2/50 hover:bg-panel2 disabled:opacity-50"
                    >
                      Cancel
                    </button>
                  )}
                  {editingIndex === null && canRevertEdit && (
                    <button
                      type="button"
                      onClick={() => { setCanRevertEdit(false); setEditNotice(null); }}
                      className="text-faint hover:text-muted transition font-medium text-[10px] px-1.5 py-0.5 rounded border border-edge bg-panel2/50 hover:bg-panel2"
                    >
                      Dismiss
                    </button>
                  )}
                </span>
              </div>
            )}

            {/* Context Usage expandable panel */}
            {showContextPanel && !contextUsage && (
              <div className="flex items-center justify-between p-3.5 bg-panel border-b border-edge text-[11.5px] select-none rounded-t-2xl animate-in slide-in-from-bottom duration-150">
                <div className="flex items-center gap-2 text-faint">
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  <span className="font-semibold text-txt">Context Usage</span>
                  <span className="text-muted">loading...</span>
                </div>
                <button onClick={() => setShowContextPanel(false)} className="text-faint hover:text-muted transition p-0.5 rounded hover:bg-panel2" title="Close">
                  <X size={13} />
                </button>
              </div>
            )}
            {showContextPanel && contextUsage && (
              <div className="flex flex-col p-3.5 bg-panel border-b border-edge text-[11.5px] select-none rounded-t-2xl animate-in slide-in-from-bottom duration-150">
                <div className="flex items-center justify-between font-medium mb-2.5">
                  <div className="flex items-center gap-1.5">
                    <span className="font-semibold text-txt">Context Usage</span>
                    <span className="text-[10px] bg-accent/15 text-accent px-1.5 py-0.5 rounded-full font-mono">
                      {Math.min(100, Math.round((contextUsage.total / contextUsage.limit) * 100))}% Full
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-faint font-mono text-[11px]">
                      ~{(contextUsage.total / 1000).toFixed(1)}K / {(contextUsage.limit / 1000).toFixed(0)}K Tokens
                    </span>
                    <button
                      onClick={() => setShowContextPanel(false)}
                      className="text-faint hover:text-muted transition p-0.5 rounded hover:bg-panel2"
                      title="Close"
                    >
                      <ChevronDown size={14} />
                    </button>
                  </div>
                </div>

                {/* Segmented/stacked progress bar */}
                <div className="w-full h-2 bg-panel2 border border-edge/60 rounded-full overflow-hidden flex mb-3">
                  {(() => {
                    const colors = [
                      "bg-blue-500",    // System prompt
                      "bg-emerald-500", // Tool definitions
                      "bg-purple-500",  // Rules
                      "bg-amber-500",   // Skills
                      "bg-teal-500",    // MCP
                      "bg-rose-500",    // Subagent
                      "bg-pink-500",    // Summarized conversation
                      "bg-indigo-500",  // Conversation
                    ];
                    
                    return contextUsage.categories.map((cat, idx) => {
                      if (cat.tokens <= 0) return null;
                      const pct = (cat.tokens / contextUsage.limit) * 100;
                      return (
                        <div
                          key={cat.name}
                          className={`${colors[idx % colors.length]} h-full transition-all duration-300`}
                          style={{ width: `${pct}%` }}
                          title={`${cat.name}: ${(cat.tokens / 1000).toFixed(1)}K tokens (${Math.round(pct)}%)`}
                        />
                      );
                    });
                  })()}
                </div>

                {/* Categories breakdown grid */}
                <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-txt/90">
                  {(() => {
                    const colors = [
                      "bg-blue-500",    // System prompt
                      "bg-emerald-500", // Tool definitions
                      "bg-purple-500",  // Rules
                      "bg-amber-500",   // Skills
                      "bg-teal-500",    // MCP
                      "bg-rose-500",    // Subagent
                      "bg-pink-500",    // Summarized conversation
                      "bg-indigo-500",  // Conversation
                    ];

                    return contextUsage.categories.map((cat, idx) => {
                      if (cat.tokens <= 0) return null;
                      return (
                        <div key={cat.name} className="flex items-center justify-between text-[11px] font-mono py-0.5 border-b border-edge/10">
                          <div className="flex items-center gap-1.5 truncate">
                            <span className={`w-2 h-2 rounded-full ${colors[idx % colors.length]} shrink-0`} />
                            <span className="truncate text-muted">{cat.name}</span>
                          </div>
                          <span className="text-txt font-medium shrink-0">
                            {(cat.tokens / 1000).toFixed(1)}K
                          </span>
                        </div>
                      );
                    });
                  })()}
                </div>
              </div>
            )}

            {/* Mention autocomplete dropdown */}
            {mentionSearch !== null && (filteredFiles.length > 0 || symbolResults.length > 0 || mentionListingCap) && (
              <div className="absolute left-2 bottom-full mb-1.5 z-50 max-h-[250px] w-[340px] overflow-y-auto bg-panel border border-edge rounded-xl shadow-2xl py-1">
                {filteredFiles.length > 0 && (
                  <>
                    <div className="px-2.5 py-1 text-[10px] uppercase font-bold tracking-wider text-faint border-b border-edge/30 select-none">
                      Files
                    </div>
                    {filteredFiles.map((file, idx) => {
                      const isSelected = idx === selectedFileIndex;
                      return (
                        <div
                          key={file}
                          onClick={() => insertMention(file)}
                          onMouseEnter={() => setSelectedFileIndex(idx)}
                          className={`flex items-center gap-2 px-3 py-1.5 text-[11.5px] cursor-pointer transition select-none ${
                            isSelected ? "bg-panel2 text-accent font-medium" : "text-txt/90 hover:bg-panel2/50"
                          }`}
                        >
                          <FileText size={11.5} className="shrink-0 opacity-60" />
                          <span className="truncate flex-1 font-mono">{file}</span>
                        </div>
                      );
                    })}
                  </>
                )}

                {symbolResults.length > 0 && (
                  <>
                    <div className="px-2.5 py-1 text-[10px] uppercase font-bold tracking-wider text-faint border-b border-edge/30 mt-1 select-none flex items-center justify-between">
                      <span>Symbols</span>
                      {codegraphStatus === "indexing" && (
                        <span className="text-[9px] text-muted normal-case font-normal animate-pulse">indexing...</span>
                      )}
                    </div>
                    {symbolResults.map((sym, idx) => {
                      const globalIdx = filteredFiles.length + idx;
                      const isSelected = globalIdx === selectedFileIndex;
                      return (
                        <div
                          key={`${sym.path}:${sym.line}:${sym.name}`}
                          onClick={() => insertSymbol(sym.name)}
                          onMouseEnter={() => setSelectedFileIndex(globalIdx)}
                          className={`flex flex-col gap-0.5 px-3 py-1.5 text-[11.5px] cursor-pointer transition select-none ${
                            isSelected ? "bg-panel2 text-accent" : "text-txt/90 hover:bg-panel2/50"
                          }`}
                        >
                          <div className="flex items-center gap-1.5">
                            <Code size={11.5} className="shrink-0 opacity-60" />
                            <span className="font-mono font-medium truncate flex-1 text-left">{sym.name}</span>
                            <span className="text-[9px] font-mono px-1 py-0.2 bg-edge/30 rounded text-muted shrink-0 lowercase">
                              {sym.kind}
                            </span>
                          </div>
                          <span className="text-[10px] text-muted font-mono truncate pl-5 text-left">
                            {sym.path}:{sym.line}
                          </span>
                        </div>
                      );
                    })}
                  </>
                )}

                {filteredFiles.length > 0 && symbolResults.length === 0 && codegraphStatus === "indexing" && (
                  <div className="px-3 py-1 text-[10px] text-muted/60 select-none italic text-right">
                    symbols indexing...
                  </div>
                )}

                {mentionListingCap && (
                  <div className="px-3 py-1.5 text-[10px] text-muted border-t border-edge/20 select-none">
                    {formatMentionListingCapMessage(mentionListingCap)}
                  </div>
                )}
              </div>
            )}

            {/* Slash commands autocomplete dropdown */}
            {slashSearch !== null && (() => {
              const matchingSlash = allSlashCommands.filter(s => s.cmd.toLowerCase().startsWith("/" + slashSearch.toLowerCase()));
              if (matchingSlash.length === 0) return null;
              return (
                <div className="absolute left-2 bottom-full mb-1.5 z-50 max-h-[220px] w-[320px] overflow-y-auto bg-panel border border-edge rounded-xl shadow-2xl py-1">
                  <div className="px-2.5 py-1 text-[10px] uppercase font-bold tracking-wider text-faint border-b border-edge/30 select-none">
                    Commands
                  </div>
                  {matchingSlash.map((s, idx) => {
                    const isSelected = idx === selectedSlashIndex;
                    return (
                      <div
                        key={s.cmd}
                        onClick={() => insertSlashCommand(s.cmd)}
                        onMouseEnter={() => setSelectedSlashIndex(idx)}
                        className={`flex flex-col px-3 py-1.5 cursor-pointer transition select-none ${
                          isSelected ? "bg-panel2 text-accent font-medium" : "text-txt/90 hover:bg-panel2/50"
                        }`}
                      >
                        <div className="flex items-center gap-1.5 text-[11.5px] font-mono font-semibold">
                          <span>{s.cmd}</span>
                        </div>
                        <span className="text-[10px] text-muted leading-tight">{s.desc}</span>
                      </div>
                    );
                  })}
                </div>
              );
            })()}

            {/* Attached images preview chips */}
            {attachedImages.length > 0 && (
              <div className="flex flex-wrap items-center gap-2 px-3 pt-2.5">
                {attachedImages.map((img, idx) => (
                  <div
                    key={idx}
                    className="relative group/thumb w-[40px] h-[40px] rounded-lg overflow-hidden border border-edge bg-panel/50 select-none animate-in fade-in zoom-in duration-150"
                  >
                    <img
                      src={img.previewUrl}
                      alt={img.name}
                      onClick={() => setLightboxUrl(img.previewUrl)}
                      className="w-full h-full object-cover cursor-pointer hover:opacity-90 transition-opacity"
                    />
                    <button
                      onClick={() => {
                        setAttachedImages((prev) => prev.filter((_, i) => i !== idx));
                        URL.revokeObjectURL(img.previewUrl);
                        setUploadError(null);
                      }}
                      className="absolute top-0 right-0 p-0.5 bg-black/60 text-txt hover:text-risk opacity-0 group-hover/thumb:opacity-100 flex items-center justify-center transition rounded-bl"
                      title="Remove image"
                    >
                      <X size={11} />
                    </button>
                  </div>
                ))}
                {attachedImages.length > 1 && (
                  <span className="text-[10px] text-muted self-center ml-1 select-none font-medium">
                    {attachedImages.length} images
                  </span>
                )}
              </div>
            )}

            {uploadError && (
              <div className="text-[11px] text-risk px-3 pt-1">
                {uploadError}
              </div>
            )}

            <textarea ref={taRef} value={input} 
              onChange={(e) => handleInputChange(e.target.value, e.target.selectionStart)}
              onKeyDown={handleKeyDown}
              onPaste={handlePaste}
              rows={1} placeholder={auto ? "Give the pilot an objective..." : "Message the pilot..."}
              className="w-full bg-transparent px-3 pt-2.5 pb-1 text-[0.8125rem] resize-none focus:outline-none overflow-hidden placeholder:text-faint" />
            <div className="flex items-center gap-1.5 px-3 pb-2">
              <button onClick={() => {
                setAuto((a) => {
                  const next = !a;
                  if (next) setPlan(false);
                  return next;
                });
              }} title="Autopilot: the pilot plans and executes autonomously (vs. you steering each step)"
                className={`px-1.5 h-[20px] rounded-md text-[10.5px] flex items-center gap-1 transition
                  ${auto ? "bg-warn/15 text-warn" : "text-faint hover:text-muted"}`}>
                <Zap size={11} /> Autopilot
              </button>
              <button onClick={() => {
                setPlan((p) => {
                  const next = !p;
                  if (next) setAuto(false);
                  return next;
                });
              }} title="Plan mode -- get an actionable plan instead of execution (read-only)"
                className={`px-1.5 h-[20px] rounded-md text-[10.5px] flex items-center gap-1 transition
                  ${plan ? "bg-accent/15 text-accent" : "text-faint hover:text-muted"}`}>
                <ListChecks size={11} /> Plan
              </button>
              <PilotPicker config={config} />
              <button
                onClick={() => {
                  setShowContextPanel(!showContextPanel);
                  if (!showContextPanel) {
                    fetchContextUsage();
                  }
                }}
                title="View context window usage breakdown"
                className={`px-1.5 h-[20px] rounded-md text-[10.5px] font-mono flex items-center gap-1 transition
                  ${showContextPanel ? "bg-accent/15 text-accent border border-accent/20" : "text-faint hover:text-muted bg-panel2/40 border border-edge/30 hover:bg-panel2/80"}`}
              >
                <FileText size={11} />
                <span>
                  {contextUsage
                    ? `${Math.min(100, Math.round((contextUsage.total / contextUsage.limit) * 100))}%`
                    : "Usage"}
                </span>
              </button>
              <div className="flex-1" />
              {input.trim() && (
                <button
                  onClick={handleQueueAdd}
                  title="Queue: runs after the current turn finishes (same as Cmd/Ctrl+Enter)"
                  className="px-2 h-[20px] rounded-md bg-panel2/60 border border-edge/60 text-faint hover:text-muted hover:border-edge2 text-[10.5px] font-medium flex items-center gap-1 transition"
                >
                  <ListChecks size={9} />Queue
                </button>
              )}
              {composerBusy
                ? <>
                    <button onClick={stop} className="px-2 h-[20px] rounded-md bg-risk/15 text-risk text-[10.5px] font-medium flex items-center gap-1"><Square size={9} />Stop</button>
                    <button onClick={send} disabled={transcriptStale || (!input.trim() && attachedImages.length === 0)}
                      title="Steer: redirect the current turn now (Enter). Cmd/Ctrl+Enter or Queue = run after this turn finishes."
                      className="px-2.5 h-[20px] rounded-md bg-accent text-black/90 text-[10.5px] font-semibold flex items-center gap-1 hover:brightness-110 disabled:opacity-40 disabled:cursor-default transition">
                      <Send size={9} />Steer</button>
                  </>
                : <button onClick={send} disabled={transcriptStale || (!input.trim() && attachedImages.length === 0)}
                    className="px-2.5 h-[20px] rounded-md bg-accent text-black/90 text-[10.5px] font-semibold flex items-center gap-1 hover:brightness-110 disabled:opacity-40 disabled:cursor-default transition">
                    <Send size={9} />{auto ? "Run" : plan ? "Plan" : "Send"}</button>}
            </div>
          </div>

        </div>
      </div>
    </div>
  ) : (
    <FileEditorPane
      path={activeTab}
      line={openTabs.find((t) => t.path === activeTab)?.line}
      col={openTabs.find((t) => t.path === activeTab)?.col}
      onClose={() => handleCloseTab(activeTab)}
      onDirtyChange={(dirty) => handleTabDirtyChange(activeTab, dirty)}
    />
  )}

      {lightboxUrl && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 backdrop-blur-sm transition-opacity animate-in fade-in duration-200"
          onClick={() => setLightboxUrl(null)}
        >
          <div className="relative max-w-[90vw] max-h-[90vh] flex flex-col items-center justify-center" onClick={(e) => e.stopPropagation()}>
            <button
              onClick={() => setLightboxUrl(null)}
              className="absolute -top-10 right-0 p-1.5 text-faint hover:text-txt bg-panel border border-edge rounded-full transition-all focus:outline-none"
              title="Close"
            >
              <X size={16} />
            </button>
            <img
              src={lightboxUrl}
              alt="Enlarged screenshot"
              className="max-w-full max-h-[80vh] object-contain rounded-lg border border-edge shadow-2xl"
            />
          </div>
        </div>
      )}

    </main>
  );
}

