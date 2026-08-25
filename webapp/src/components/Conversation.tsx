import { useEffect, useLayoutEffect, useRef, useState, useCallback } from "react";
import { api, type Config, type Job } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import FileEditorPane from "./FileEditorPane";
import {
  type Card,
  type CommandApprovalItem,
  type SecretRequestItem,
  type Item,
} from "./TranscriptList";
import {
  deriveBusyProgress,
  turnHasLiveInvestigation,
} from "../lib/turnProgress";
import {
  CONTINUE_PROMPT,
  hasPartialAssistantAnswer,
  latestUserAsk,
  recoveryControlsAvailable,
  recoveryDispatchAllowed,
  settleFromStaleLocalAbandon,
  settleFromStreamError,
  settleFromTransportEof,
  settleFromUserStop,
  type RecoveryContext,
  type TerminalCause,
  type TurnLifecycle,
  type TurnSettle,
} from "../lib/turnTerminal";
import { renameDefaultSessionIfNeeded } from "../lib/sessionTitle";
import { notifyWorkspaceMutated } from "../lib/workspaceMutationEvents";

import { writeTranscriptCache } from "./conversation/transcriptCache";
import {
  mergeTranscriptItems,
  transcriptFingerprint,
  transcriptResponseToItems,
} from "./conversation/transcriptItems";
import { hoistCardsBeforeTrailingFinals, newThinkingId } from "./conversation/thinkingToolPrep";
import {
  type MentionListingCap,
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
} from "./conversation/streamBubbles";
import {
  derivePillBusyDetail,
  derivePillStatus,
  isSwarmPausePoint,
} from "./conversation/pillStatus";
import { isAgentLoopOpen, isPilotMouthBusy } from "./conversation/runnersBusy";
import {
  appendPendingReview,
  appendStopHonestyNotice,
  appendTurnTerminal,
  applySwarmResultToItems,
  finalizeOrphanSwarmPills,
  focusReviewTabAndRefresh,
  mergeJobActionsIntoItems,
  noticeIsStopHonesty,
  patchCardInItems,
  reconcileOrphanInvestigationCards,
  sealOpenStreamSurfaces,
  shouldApplySwarmLiveMerge,
  updateCommandApproval,
  updateSecretRequest,
} from "./conversation/streamApply";
import {
  classifyLocalSlashCommand,
  composerEnterAction,
  editNoticeAfterSend,
  EDIT_BUSY_PROGRESS_NOTICE,
  executeSendGate,
  formatCompactCompleteMessage,
  formatCompactErrorMessage,
  shouldApplyCompactSettle,
  formatRenderCommandErrorMessage,
  formatSteerErrorMessage,
  formatInterruptErrorMessage,
  localSlashChromeAction,
  localSlashPaletteAction,
  runEditMessageFlow,
  runStopFlow,
  shouldBlockEmptySend,
  shouldClearSteerDraftOnResult,
  shouldSteerWhileBusy,
  steerResultChrome,
  steerTranscriptItem,
  userOrdinalBeforeIndex,
} from "./conversation/composerSend";
import { runCommandPaletteAction } from "../lib/commandPalette";
import { focusSettingsPage } from "./SettingsShell";
import {
  FEED_GESTURE_IDLE_MS,
  FEED_REPIN_THRESHOLD_PX,
  FEED_TAIL_EPSILON_PX,
  chooseFeedFollowFlush,
  feedResizeScrollFollowDecision,
  isAtFeedTail,
  nextFeedPinState,
  scrollToFeedEnd,
  settleFrameResult,
  shouldShowJumpToBottom,
  shouldUnpinOnTouchMove,
  FEED_UNPIN_BUBBLE_EVENT,
  feedWheelUnpinListenerOptions,
  shouldUnpinOnWheel,
  type FeedResizeObservationSnapshot,
} from "./conversation/feedScroll";
import {
  ADD_TERMINAL_SELECTION_EVENT,
  appendTerminalMention,
  applyTerminalSelectionsToMessage,
  terminalLabelsFromDraft,
} from "../lib/terminalSelection";
import {
  dropTerminalLabels,
  peekTerminalSelections,
  putTerminalSelection,
} from "./conversation/terminalSelectionCache";
import {
  chatColumnMountClass,
  isChatColumnActive,
  isOccludedScrollParentSize,
  restoreFeedScrollAfterFocus,
} from "./conversation/transcriptVirtualWindow";
import {
  alreadySettledOnDoneStatus,
  streamErrorText,
  streamOnDoneDecision,
  streamOnErrorDecision,
} from "./conversation/streamTerminal";
import ConversationChatColumn from "./conversation/ConversationChatColumn";
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
  collectFilesFromDirectoryEntry,
  DROP_FOLDER_FILE_CAP,
  droppedDirectoryPlan,
  droppedPathIsDirectory,
  mentionTokenForDroppedPath,
  resolveDroppedOsPath,
  uploadErrorMessage,
  type DirectoryEntryLike,
} from "./conversation/composerInput";
import { openAgentWorkspace } from "../lib/agentLinks";
import { AUTH_FAILURE, sharedReadinessNotice, fromBackendDiagnostic } from "../lib/operationalDiagnostic";
import { getActiveDiagnostic, publishDiagnostic } from "../lib/operationalDiagnosticBus";
import {
  executeDiagnosticRecovery,
  clearDiagnosticAfterSuccess,
  syncConversationTurnFailureDiagnostic,
} from "../lib/operationalRecovery";
import { useOperationalDiagnostic } from "../lib/useOperationalDiagnostic";
import {
  blankMsgQueueOnSessionSwitch,
  blankQueueItemsOnSessionSwitch,
  moveItem,
  QUEUE_LOAD_FAIL_NOTICE,
  reorderByDrag,
  shouldApplyQueueRefresh,
} from "./conversation/queueOps";
import type { ComposerAttachedImage } from "./conversation/composerAttachmentCache";
import {
  notifyPrefEnabled,
  queueMessagesPrefEnabled,
  shouldShowCompletionNotification,
  soundPrefEnabled,
} from "./conversation/completionNotify";
import { createApplyStreamEvent } from "./conversation/streamEventHandler";
import EditorTabStrip from "./conversation/EditorTabStrip";
import ComposerDock, { type MemoryProposal } from "./conversation/ComposerDock";
import ConversationHeader from "./conversation/ConversationHeader";
import ImageLightbox from "./conversation/ImageLightbox";
import SpillPreviewModal, {
  clearedSessionOverlays,
  shouldApplySpillPreview,
  type SpillPreviewState,
} from "./conversation/SpillPreviewModal";
import { useSessionSwitch } from "./conversation/useSessionSwitch";
import { useRunnersBusyPoll } from "./conversation/useRunnersBusyPoll";
import {
  appendMemoryProposal,
  classifySwarmPollEvent,
  clearSwarmAwaitWaitHint,
  PILOT_LOOKING_HINT,
  pilotResumePollAction,
  pruneTerminalJobIds,
  sessionStateShowsAwaitingSwarm,
  shouldHoldSwarmAwaitChrome,
  SWARM_AWAIT_HINT,
  swarmResultsAwaitChromeClear,
  terminalJobIdsFromSwarmLive,
  terminalJobIdsNeedingResultRecovery,
  triggerResumeGate,
} from "./conversation/swarmPoll";
import { armResumeKick } from "./conversation/sessionResumeLatch";
import {
  cancelStreamPaint,
  flushTypewriterBuffer,
  scheduleStreamPaint,
  startTypewriterLoop,
} from "./conversation/streamTypewriter";
import {
  chooseResolvedFilePath,
  closeTabResult,
  otherTabsHaveDirty,
  setTabDirty,
  tabHasDirty,
  upsertOpenTab,
} from "./conversation/openFileTabs";
import { normalizeContextUsage } from "./conversation/contextUsageColors";

// Re-export pure helpers so existing test / LeftRail import paths keep working.
export * from "./conversation/reexports";

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
  // Mid-turn reattach: last applied /api/session/events store cursor (unified).
  const lastAppliedCursorRef = useRef(0);
  // Ring generation from the last successful stream event (pin subsequent polls).
  const ringGenerationRef = useRef<number | undefined>(undefined);
  // setInterval handle for the single store-event cursor poll.
  const chatEventsPollTimerRef = useRef<number | null>(null);
  // Legacy live-watch cancel slot (store cursor owns reattach; kept for armed()).
  const chatEventsLiveCancelRef = useRef<null | (() => void)>(null);
  // Shared live-SSE + reattach event applicator (assigned where handlers live).
  const applyStreamEventRef = useRef<(ev: { kind: string; data?: any }) => void>(() => {});
  const flushTypewriterRef = useRef<() => void>(() => {});
  const maybeRunQueuedResumeRef = useRef<() => void>(() => {});
  const maybeRunApprovedCommandRetryRef = useRef<() => void>(() => {});
  const approvedCommandRetryRef = useRef<string | null>(null);
  const maybeDrainQueueRef = useRef<() => void>(() => {});
  // Session-load effect installs the reattach starter; runners-poll calls it when
  // a turn begins without a local EventSource (e.g. Discord Bridge queue drain).
  const ensureChatEventsReattachRef = useRef<() => void>(() => {});
  const abandonStaleLocalStreamRef = useRef<() => void>(() => {});

  const clearChatEventsPoll = () => {
    if (chatEventsPollTimerRef.current != null) {
      window.clearInterval(chatEventsPollTimerRef.current);
      chatEventsPollTimerRef.current = null;
    }
    if (chatEventsLiveCancelRef.current) {
      const cancelLive = chatEventsLiveCancelRef.current;
      chatEventsLiveCancelRef.current = null;
      cancelLive();
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
    if (tabHasDirty(openTabs, path)) {
      if (!window.confirm(`Discard unsaved changes for ${path}?`)) {
        return;
      }
    }
    const next = closeTabResult(openTabs, path, activeTab);
    setOpenTabs(next.tabs);
    setActiveTab(next.activeTab);
  };

  const handleCloseOtherTabs = (keepPath: string) => {
    if (otherTabsHaveDirty(openTabs, keepPath)) {
      if (!window.confirm("Discard unsaved changes in other tabs?")) return;
    }
    setOpenTabs((prev) => prev.filter((t) => t.path === keepPath));
    setActiveTab(keepPath);
  };

  const handleCloseAllTabs = () => {
    if (tabHasDirty(openTabs)) {
      if (!window.confirm("Discard unsaved changes in all tabs?")) return;
    }
    setOpenTabs([]);
    setActiveTab("chat");
  };

  const handleTabDirtyChange = (path: string, isDirty: boolean) => {
    setOpenTabs((prev) => setTabDirty(prev, path, isDirty));
  };

  useEffect(() => {
    const handleOpenFile = (e: CustomEvent<{ path: string; line?: number; col?: number; trusted?: boolean }>) => {
      const filePath = e.detail.path;
      if (!filePath) return;
      const line = e.detail.line;
      const col = e.detail.col;
      const trusted = !!e.detail.trusted;
      const openResolved = (resolvedPath: string) => {
        setOpenTabs((prev) => upsertOpenTab(prev, resolvedPath, line, col));
        setActiveTab(resolvedPath);
      };
      const applyChoice = (choice: ReturnType<typeof chooseResolvedFilePath>) => {
        if (!choice) return;
        if ("toast" in choice) {
          window.dispatchEvent(new CustomEvent("harness-toast", { detail: choice.toast }));
          return;
        }
        openResolved(choice.path);
      };
      // Transcript clicks fail closed when resolve cannot find a unique file.
      // File-tree clicks are trusted and may open the given path if resolve is down.
      void api.resolveFile(filePath)
        .then((resolved) => {
          applyChoice(chooseResolvedFilePath(filePath, resolved, { trusted }));
        })
        .catch(() => {
          applyChoice(chooseResolvedFilePath(filePath, null, { trusted }));
        });
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

  useEffect(() => {
    const onClose = (e: Event) => {
      const path = (e as CustomEvent<{ path?: string }>).detail?.path;
      if (!path) return;
      handleCloseTab(path);
    };
    window.addEventListener("harness-close-editor-tab", onClose as EventListener);
    return () => window.removeEventListener("harness-close-editor-tab", onClose as EventListener);
  }, [openTabs, activeTab]);

  const [input, setInput] = useState("");
  // Live composer text for per-session draft cache across useSessionSwitch.
  const composerInputRef = useRef("");
  useEffect(() => {
    composerInputRef.current = input;
  }, [input]);
  // Live session id for async queue fences (Clear All / late refresh).
  const activeSessionIdRef = useRef<string | null>(activeSessionId);
  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);
  const [status, setStatus] = useState<"idle"|"thinking"|"executing"|"done"|"error"|"streaming"|"awaiting_swarm">("idle");
  // Wall clock for the live busy footer ("running · read_file · step 3 · 2m 14s").
  // Starts when we enter a busy phase; clears on idle/done/error. A 1s tick keeps
  // the elapsed label honest without re-rendering the whole app on a fast interval.
  const [busyStartedAt, setBusyStartedAt] = useState<number | null>(null);
  const [busyNow, setBusyNow] = useState(() => Date.now());
  // busyStartedAt tracks status phases; holdSwarmAwait is folded into
  // agentLoopOpen below once pendingJobIds/backendPendingSwarms exist.
  useEffect(() => {
    const busy =
      status === "thinking"
      || status === "executing"
      || status === "streaming"
      || status === "awaiting_swarm";
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
  // awaiting_swarm: model turn closed after background dispatch, but workers
  // still fly — Cursor-style "Still working…" until keep-alive resume.
  const [turnOpen, setTurnOpen] = useState(false);
  const [turnLifecycle, setTurnLifecycle] = useState<TurnLifecycle>("settled_complete");
  const [terminalCause, setTerminalCause] = useState<TerminalCause | null>(null);
  const [sessionSwitchPending, setSessionSwitchPending] = useState(false);
  const recoveryDispatchingRef = useRef(false);
  const recoveryContextRef = useRef<RecoveryContext | null>(null);
  const lastSettleRef = useRef<TurnSettle | null>(null);
  const [waitHint, setWaitHint] = useState<string | null>(null);
  // True while visible items belong to a prior session (or are awaiting hydrate).
  // Dims the feed and blocks send so stale A is never treated as B.
  const [transcriptStale, setTranscriptStale] = useState(false);
  const transcriptStaleRef = useRef(false);
  useEffect(() => { transcriptStaleRef.current = transcriptStale; }, [transcriptStale]);
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
  const [memoryProposals, setMemoryProposals] = useState<MemoryProposal[]>([]);
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
  const feedContentRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const planTurnRef = useRef(false);
  // Keep-alive: set when a background swarm finishes (pilot_resume) while a turn
  // is still streaming. The in-flight turn's onDone drains it so the pilot
  // continues automatically instead of going to sleep after dispatching work.
  const resumeQueuedRef = useRef(false);
  // Stable indirection so the always-on swarm-results poll (defined before the
  // trigger) can fire a keep-alive turn without a declaration-order dependency.
  const resumeTriggerRef = useRef<() => void>(() => {});
  // Typewriter buffer: network deltas arrive in bursts. Codex paints the
  // arrived chunk (no char drip). Hermes coalesces on a 33ms timer — not rAF
  // — so a hidden/minimized renderer cannot park the queue until refocus.
  const typeBufRef = useRef<string>("");          // undrained characters
  const typeRafRef = useRef<number | null>(null); // active paint-timer handle
  const typeDoneRef = useRef<boolean>(false);     // stream ended -> drain then stop

  // Cancel any in-flight paint timer on unmount so the loop never leaks.
  useEffect(() => {
    return () => {
      if (typeRafRef.current != null) {
        cancelStreamPaint(typeRafRef.current);
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
  const [queueLoadError, setQueueLoadError] = useState<string | null>(null);
  const operationalDiagnostic = useOperationalDiagnostic();
  const queueFetchGenRef = useRef(0);
  const [queueDragIndex, setQueueDragIndex] = useState<number | null>(null);
  const [queueDragOverIndex, setQueueDragOverIndex] = useState<number | null>(null);

  const [pendingJobIds, setPendingJobIds] = useState<string[]>([]);
  const pendingJobIdsRef = useRef<string[]>([]);
  useEffect(() => { pendingJobIdsRef.current = pendingJobIds; }, [pendingJobIds]);
  const processedSwarmJobIdsRef = useRef<string[]>([]);
  const [backendPendingSwarms, setBackendPendingSwarms] = useState(false);
  const [swarmLiveJobs, setSwarmLiveJobs] = useState<Job[]>([]);

  // Hold investigation / Still working… after switch/hydrate while background
  // jobs fly, even if status briefly flaps idle before awaiting_swarm paints.
  const holdSwarmAwait = shouldHoldSwarmAwaitChrome({
    pendingJobIds,
    backendPendingSwarms,
    userStopped: userStoppedRef.current,
  });
  // Bare holdSwarmAwait keeps agentLoopOpen (investigation / Still working…).
  // The mouth is a second bit: Stop/Steer only while the pilot turn is open.
  const agentLoopOpen =
    isAgentLoopOpen(turnOpen, status) || holdSwarmAwait;
  const liveInvestigation = turnHasLiveInvestigation(items, agentLoopOpen);
  const busyProgress = deriveBusyProgress(items, status, busyElapsedMs, {
    modelLabel: config?.driver || "",
    waitHint,
  });
  // Runner/SSE can briefly report idle while a card is still running (or the
  // reverse). Prefer investigation / open-turn truth for the header pill.
  // StatusPill follows agentLoopOpen (workers still visible). The mouth is
  // a second bit and may already be Send.
  const swarmPausePoint = isSwarmPausePoint({
    status,
    holdSwarmAwait,
    turnOpen,
  });
  // Mouth ≠ runner. awaiting_swarm / holdSwarmAwait keep the fold, not Stop.
  const composerBusy = isPilotMouthBusy(turnOpen, status, sessionSwitchPending);
  const derivedPillStatus: string = derivePillStatus({
    transcriptStale,
    answerChromeIdle: false,
    liveInvestigation,
    turnOpen,
    status,
    awaitingSwarm: swarmPausePoint,
    agentLoopOpen,
  });
  // Operational diagnostic is settled failure, not a live turn lifecycle.
  const pillStatus: string = (
    operationalDiagnostic
    && operationalDiagnostic.severity === "error"
    && !composerBusy
      ? "error"
      : derivedPillStatus
  );
  // Keep the busy footer clock alive while holdSwarmAwait outlives status flaps.
  useEffect(() => {
    if (holdSwarmAwait) {
      setBusyStartedAt((prev) => prev ?? Date.now());
    }
  }, [holdSwarmAwait]);

  const [attachedImages, setAttachedImages] = useState<ComposerAttachedImage[]>([]);
  // Live composer attachments for per-session cache across useSessionSwitch.
  const attachedImagesRef = useRef<ComposerAttachedImage[]>([]);
  useEffect(() => {
    attachedImagesRef.current = attachedImages;
  }, [attachedImages]);
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
  const clearSafeTimeouts = () => {
    timeoutsRef.current.forEach(clearTimeout);
    timeoutsRef.current.clear();
  };

  useEffect(() => {
    return () => {
      clearSafeTimeouts();
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
  const [spillPreview, setSpillPreview] = useState<SpillPreviewState | null>(null);

  // Spill peek + image lightbox are Conversation-local overlays — never carry
  // session A's body/URL into B (or leave them painted after a project switch).
  useEffect(() => {
    const cleared = clearedSessionOverlays();
    setSpillPreview(cleared.spillPreview);
    setLightboxUrl(cleared.lightboxUrl);
  }, [activeSessionId]);

  // Spilled tool stdout (spill://) → read-only peek modal via /api/spill/read.
  useEffect(() => {
    const handleOpenSpill = (e: Event) => {
      const uri = String((e as CustomEvent<{ uri?: string }>).detail?.uri || "").trim();
      if (!uri) return;
      const requestSessionId = activeSessionIdRef.current;
      setSpillPreview({
        uri,
        content: "Loading…",
        chars: 0,
        truncated: false,
      });
      void api.readSpill(uri).then((res) => {
        if (!shouldApplySpillPreview({
          requestSessionId,
          activeSessionId: activeSessionIdRef.current,
        })) return;
        if (!res?.ok) {
          setSpillPreview({
            uri,
            content: "",
            chars: 0,
            truncated: false,
            error: res?.error || "Failed to read spill",
          });
          return;
        }
        setSpillPreview({
          uri: res.uri || uri,
          content: res.content || "",
          chars: typeof res.chars === "number" ? res.chars : (res.content || "").length,
          truncated: !!res.truncated,
        });
      }).catch((err: unknown) => {
        if (!shouldApplySpillPreview({
          requestSessionId,
          activeSessionId: activeSessionIdRef.current,
        })) return;
        setSpillPreview({
          uri,
          content: "",
          chars: 0,
          truncated: false,
          error: err instanceof Error ? err.message : "Failed to read spill",
        });
      });
    };
    window.addEventListener("harness-open-spill", handleOpenSpill as EventListener);
    return () => {
      window.removeEventListener("harness-open-spill", handleOpenSpill as EventListener);
    };
  }, []);

  // Agent markdown / ActionCard image clicks → lightbox (http(s), data:, or
  // uploaded/repo paths resolved via api.imageUrl).
  useEffect(() => {
    const handleOpenImage = (e: CustomEvent<{ path?: string; url?: string }>) => {
      const url = String(e.detail?.url || "").trim();
      const path = String(e.detail?.path || "").trim();
      if (url) {
        setLightboxUrl(url);
        return;
      }
      if (!path) return;
      if (/^https?:\/\//i.test(path) || path.startsWith("data:")) {
        setLightboxUrl(path);
        return;
      }
      try {
        setLightboxUrl(api.imageUrl(path));
      } catch {
        /* ignore */
      }
    };
    window.addEventListener("harness-open-image", handleOpenImage as EventListener);
    return () => {
      window.removeEventListener("harness-open-image", handleOpenImage as EventListener);
    };
  }, []);

  // cwd / open_project → same workspace open path as WorkspaceChip.
  useEffect(() => {
    const handleOpenWorkspace = (e: CustomEvent<{ path?: string }>) => {
      const path = String(e.detail?.path || "").trim();
      if (!path) return;
      void api.openWorkspace(path)
        .then((res) => {
          if ((res as { ok?: boolean }).ok) {
            window.dispatchEvent(new Event("harness-config-changed"));
          } else {
            const err = (res as { error?: string }).error || `Could not open ${path}`;
            window.dispatchEvent(new CustomEvent("harness-toast", { detail: err }));
          }
        })
        .catch((err) => {
          const msg = err instanceof Error ? err.message : String(err || `Could not open ${path}`);
          window.dispatchEvent(new CustomEvent("harness-toast", { detail: msg }));
        });
    };
    window.addEventListener("harness-open-workspace", handleOpenWorkspace as EventListener);
    return () => {
      window.removeEventListener("harness-open-workspace", handleOpenWorkspace as EventListener);
    };
  }, []);

  // Compacting & Context breakdown states
  const [compactingStatus, setCompactingStatus] = useState<string | null>(null);
  const [showContextPanel, setShowContextPanel] = useState(false);
  const [contextUsage, setContextUsage] = useState<import("../lib/api").ContextUsageResponse | null>(null);

  // Ergonomics states
  const [allFiles, setAllFiles] = useState<string[]>([]);
  const [allFolders, setAllFolders] = useState<string[]>([]);
  const [mentionListingCap, setMentionListingCap] = useState<MentionListingCap | null>(null);
  const [mentionSearch, setMentionSearch] = useState<string | null>(null);
  const [mentionIndex, setMentionIndex] = useState<number>(-1);
  const [filteredFiles, setFilteredFiles] = useState<string[]>([]);
  const [filteredFolders, setFilteredFolders] = useState<string[]>([]);
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

  // PROMPT QUEUE: light refresh -- on session change, on a small poll interval,
  // and after any local mutation (add/remove/reorder/clear). Soft-fail: never
  // treat an errored fetch as authoritative empty; fence by session + gen.
  const refreshQueue = (forSessionId: string | null = activeSessionIdRef.current) => {
    const requestSessionId = forSessionId;
    const requestGen = ++queueFetchGenRef.current;
    api.queueList()
      .then((res) => {
        if (!shouldApplyQueueRefresh({
          requestSessionId,
          activeSessionId: activeSessionIdRef.current,
          requestGen,
          currentGen: queueFetchGenRef.current,
        })) {
          return;
        }
        if (res && Array.isArray(res.items)) {
          setQueueItems(res.items);
          setQueueLoadError(null);
        }
      })
      .catch((err) => {
        if (!shouldApplyQueueRefresh({
          requestSessionId,
          activeSessionId: activeSessionIdRef.current,
          requestGen,
          currentGen: queueFetchGenRef.current,
        })) {
          return;
        }
        console.error("Failed to load prompt queue:", err);
        setQueueLoadError(sharedReadinessNotice(QUEUE_LOAD_FAIL_NOTICE, getActiveDiagnostic()));
      });
  };

  useEffect(() => {
    // Immediate honesty: blank A's playlist (and soft client msgQueue) before
    // the new session's refresh returns — Clear All must not wipe B by accident.
    setQueueItems(blankQueueItemsOnSessionSwitch());
    setMsgQueue(blankMsgQueueOnSessionSwitch());
    setQueueLoadError(null);
    setQueueDragIndex(null);
    setQueueDragOverIndex(null);
    refreshQueue(activeSessionId);
    const t = window.setInterval(() => refreshQueue(activeSessionIdRef.current), 3000);
    return () => window.clearInterval(t);
    // refreshQueue closes over refs; re-arm only when the active pilot changes.
  }, [activeSessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  const moveQueueItem = (index: number, direction: "up" | "down") => {
    setMsgQueue((prev) => moveItem(prev, index, direction));
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
    setMsgQueue((prev) => reorderByDrag(prev, dragIndex, targetIdx));
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
    const sid = activeSessionIdRef.current;
    setQueueItems((prev) => {
      const next = reorderByDrag(prev, fromIdx, targetIdx);
      api.queueReorder(next.map((it) => it.id))
        .catch((err) => {
          console.error("Failed to reorder prompt queue:", err);
          if (activeSessionIdRef.current !== sid) return;
          refreshQueue(sid);
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
    const sid = activeSessionIdRef.current;
    setInput(item.text);
    setEditingIndex(null);
    setQueueItems((prev) => prev.filter((it) => it.id !== item.id));
    api.queueRemove(item.id).catch((err) => {
      console.error("Failed to remove queued prompt for edit:", err);
      if (activeSessionIdRef.current !== sid) return;
      refreshQueue(sid);
    });
    taRef.current?.focus();
  };

  const handleQueueRemove = (id: string) => {
    const sid = activeSessionIdRef.current;
    setQueueItems((prev) => prev.filter((it) => it.id !== id));
    api.queueRemove(id)
      .then(() => {
        if (activeSessionIdRef.current !== sid) return;
        refreshQueue(sid);
      })
      .catch((err) => {
        console.error("Failed to remove queued prompt:", err);
        if (activeSessionIdRef.current !== sid) return;
        refreshQueue(sid);
      });
  };

  const handleQueueClearAll = () => {
    const sid = activeSessionIdRef.current;
    setQueueItems([]);
    api.queueClear()
      .then(() => {
        if (activeSessionIdRef.current !== sid) return;
        refreshQueue(sid);
      })
      .catch((err) => {
        console.error("Failed to clear prompt queue:", err);
        if (activeSessionIdRef.current !== sid) return;
        refreshQueue(sid);
      });
  };

  const handleQueueAdd = () => {
    const raw = input.trim();
    const sessionKey = activeSessionIdRef.current || "_draft";
    const text = applyTerminalSelectionsToMessage(raw, peekTerminalSelections(sessionKey));
    if (!text) return;
    // Snapshot the attached image paths BEFORE clearing input/attachments, so a
    // queued prompt carries its images just like a normal turn. The backend
    // delivers them as real image content when the prompt drains.
    const sid = activeSessionIdRef.current;
    const queueImages = attachedImages.map((img) => img.path).filter(Boolean);
    setInput("");
    dropTerminalLabels(sessionKey, terminalLabelsFromDraft(raw));
    setAttachedImages([]);
    api.queueAdd(text, queueImages)
      .then(() => {
        if (activeSessionIdRef.current !== sid) return;
        refreshQueue(sid);
      })
      .catch((err) => {
        console.error("Failed to add prompt to queue:", err);
      });
  };

  // Request notifications permission on mount
  useEffect(() => {
    const isNotifyEnabled = notifyPrefEnabled();
    if (isNotifyEnabled && typeof Notification !== "undefined" && Notification.permission === "default") {
      Notification.requestPermission().catch(() => {});
    }
  }, []);

  const triggerCompletionEffects = () => {
    const isNotifyEnabled = notifyPrefEnabled();
    const isSoundEnabled = soundPrefEnabled();

    const isHidden = document.hidden || !document.hasFocus();
    if (shouldShowCompletionNotification({ notifyEnabled: isNotifyEnabled, isHidden })) {
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

      const isQueueEnabled = queueMessagesPrefEnabled();

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
  // After an upward trackpad/touch gesture, stay unpinned until the user
  // scrolls back toward the true bottom — do not re-pin from the soft
  // "near bottom" band (that fight feels like scroll stutter while streaming).
  const scrollReleasedByGestureRef = useRef(false);
  const userScrollGestureRef = useRef(false);
  const programmaticScrollRef = useRef(false);
  const gestureIdleTimerRef = useRef<number | null>(null);
  const prevFeedScrollTopRef = useRef<number | null>(null);
  // Hermes session-switch settle: while true, height-driven follow keeps
  // scrolling to bottom until height stabilizes (or wall-clock timeout).
  // onScroll still tracks real geometry so keyboard/scrollbar unpin is not swallowed.
  const scrollSettlingRef = useRef(false);
  const [feedSettled, setFeedSettled] = useState(true);
  const [showJumpToBottom, setShowJumpToBottom] = useState(false);
  const scrollFeedToEndRef = useRef<(() => void) | null>(null);
  const publishJumpVisibilityRef = useRef(() => {});
  publishJumpVisibilityRef.current = () => {
    const next = shouldShowJumpToBottom({
      pinned: pinnedToBottomRef.current,
      settling: scrollSettlingRef.current,
    });
    setShowJumpToBottom((prev) => (prev === next ? prev : next));
  };
  const jumpToLatest = () => {
    pinnedToBottomRef.current = true;
    scrollReleasedByGestureRef.current = false;
    setShowJumpToBottom(false);
    const scrollToEnd = scrollFeedToEndRef.current;
    if (scrollToEnd) {
      programmaticScrollRef.current = true;
      scrollToEnd();
    } else if (feedRef.current) {
      programmaticScrollRef.current = true;
      const el = feedRef.current;
      el.scrollTop = scrollToFeedEnd(el.scrollHeight, el.clientHeight);
    }
  };
  useEffect(() => {
    const el = feedRef.current;
    if (!el) return;
    const clearGestureIdleTimer = () => {
      if (gestureIdleTimerRef.current != null) {
        window.clearTimeout(gestureIdleTimerRef.current);
        gestureIdleTimerRef.current = null;
      }
    };
    const snapToBottomIfNear = () => {
      const node = feedRef.current;
      if (!node) return;
      if (!isAtFeedTail(
        node.scrollHeight,
        node.scrollTop,
        node.clientHeight,
        FEED_TAIL_EPSILON_PX,
      )) {
        return;
      }
      scrollReleasedByGestureRef.current = false;
      pinnedToBottomRef.current = true;
      const maxScrollTop = scrollToFeedEnd(node.scrollHeight, node.clientHeight);
      if (Math.abs(node.scrollTop - maxScrollTop) >= FEED_TAIL_EPSILON_PX) {
        programmaticScrollRef.current = true;
        node.scrollTop = maxScrollTop;
      }
      prevFeedScrollTopRef.current = node.scrollTop;
      publishJumpVisibilityRef.current();
    };
    const endUserScrollGesture = () => {
      userScrollGestureRef.current = false;
      snapToBottomIfNear();
    };
    const markUserScrollGesture = () => {
      userScrollGestureRef.current = true;
      clearGestureIdleTimer();
      gestureIdleTimerRef.current = window.setTimeout(() => {
        gestureIdleTimerRef.current = null;
        endUserScrollGesture();
      }, FEED_GESTURE_IDLE_MS);
    };
    const applyPinState = () => {
      const next = nextFeedPinState({
        wasPinned: pinnedToBottomRef.current,
        releasedByGesture: scrollReleasedByGestureRef.current,
        scrollHeight: el.scrollHeight,
        scrollTop: el.scrollTop,
        clientHeight: el.clientHeight,
        prevScrollTop: prevFeedScrollTopRef.current,
        settling: scrollSettlingRef.current,
        repinPx: FEED_REPIN_THRESHOLD_PX,
        userGestureActive: userScrollGestureRef.current,
      });
      pinnedToBottomRef.current = next.pinned;
      scrollReleasedByGestureRef.current = next.releasedByGesture;
      prevFeedScrollTopRef.current = el.scrollTop;
      publishJumpVisibilityRef.current();
    };
    const onScroll = () => {
      if (programmaticScrollRef.current) {
        programmaticScrollRef.current = false;
        applyPinState();
        return;
      }
      markUserScrollGesture();
      applyPinState();
    };
    // Fast-path unpin on upward wheel/touch before the next thinking token
    // re-runs stick-to-bottom -- otherwise long reasoning streams keep yanking
    // the feed back to the end and the user cannot scroll the Thought block.
    const onWheel = (e: WheelEvent) => {
      if (e.deltaY !== 0) {
        markUserScrollGesture();
      }
      if (shouldUnpinOnWheel(e.deltaY, scrollSettlingRef.current)) {
        scrollSettlingRef.current = false;
        scrollReleasedByGestureRef.current = true;
        pinnedToBottomRef.current = false;
        publishJumpVisibilityRef.current();
      }
    };
    const onNestedFeedUnpin = () => {
      scrollSettlingRef.current = false;
      scrollReleasedByGestureRef.current = true;
      pinnedToBottomRef.current = false;
      publishJumpVisibilityRef.current();
    };
    let touchY: number | null = null;
    const onTouchStart = (e: TouchEvent) => {
      touchY = e.touches[0]?.clientY ?? null;
    };
    const onTouchMove = (e: TouchEvent) => {
      markUserScrollGesture();
      const y = e.touches[0]?.clientY;
      if (shouldUnpinOnTouchMove(touchY, y ?? null, scrollSettlingRef.current)) {
        scrollSettlingRef.current = false;
        scrollReleasedByGestureRef.current = true;
        pinnedToBottomRef.current = false;
        publishJumpVisibilityRef.current();
      }
      touchY = y ?? touchY;
    };
    const onTouchEnd = () => {
      markUserScrollGesture();
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    // Capture phase: nested ThinkingBlock stops wheel bubble while scrolling
    // inside its pane — unpin must run first or stream tokens re-yank the feed.
    el.addEventListener("wheel", onWheel, feedWheelUnpinListenerOptions());
    el.addEventListener(FEED_UNPIN_BUBBLE_EVENT, onNestedFeedUnpin);
    el.addEventListener("touchstart", onTouchStart, { passive: true });
    el.addEventListener("touchmove", onTouchMove, { passive: true });
    el.addEventListener("touchend", onTouchEnd, { passive: true });
    el.addEventListener("touchcancel", onTouchEnd, { passive: true });
    return () => {
      clearGestureIdleTimer();
      el.removeEventListener("scroll", onScroll);
      el.removeEventListener("wheel", onWheel, feedWheelUnpinListenerOptions().capture);
      el.removeEventListener(FEED_UNPIN_BUBBLE_EVENT, onNestedFeedUnpin);
      el.removeEventListener("touchstart", onTouchStart);
      el.removeEventListener("touchmove", onTouchMove);
      el.removeEventListener("touchend", onTouchEnd);
      el.removeEventListener("touchcancel", onTouchEnd);
    };
  }, []);
  const applyFeedResizeFollow = (
    snapshot: FeedResizeObservationSnapshot | null,
  ) => {
    const el = feedRef.current;
    if (!el || !snapshot) return;
    const result = feedResizeScrollFollowDecision({
      scrollHeight: el.scrollHeight,
      scrollTop: el.scrollTop,
      clientHeight: el.clientHeight,
      offsetHeight: el.offsetHeight,
      snapshotPinned: snapshot.pinned,
      snapshotSettling: snapshot.settling,
      snapshotScrollTop: snapshot.scrollTop,
      snapshotScrollHeight: snapshot.scrollHeight,
      releasedByGesture: scrollReleasedByGestureRef.current,
      userGestureActive: userScrollGestureRef.current,
    });
    if (result.kind === "noop") return;
    if (result.kind === "follow") {
      programmaticScrollRef.current = true;
      el.scrollTop = result.scrollTop;
    }
    pinnedToBottomRef.current = true;
    prevFeedScrollTopRef.current = el.scrollTop;
    publishJumpVisibilityRef.current();
  };
  useLayoutEffect(() => {
    const viewport = feedRef.current;
    if (!viewport) return;
    const content = feedContentRef.current;
    const scheduleFollow = () => {
      const el = feedRef.current;
      if (!el) return;
      if (isOccludedScrollParentSize(el.clientHeight, el.offsetHeight)) return;
      // Flush in the RO callback (before paint). rAF-deferred follow is the
      // stream-at-bottom lurch: tokens/chrome grow, one frame paints off-tail,
      // then we snap. chooseFeedFollowFlush documents the policy.
      void chooseFeedFollowFlush();
      applyFeedResizeFollow({
        pinned: pinnedToBottomRef.current,
        settling: scrollSettlingRef.current,
        scrollTop: el.scrollTop,
        scrollHeight: el.scrollHeight,
      });
    };
    const ro =
      typeof ResizeObserver !== "undefined"
        ? new ResizeObserver(scheduleFollow)
        : null;
    ro?.observe(viewport);
    if (content) ro?.observe(content);
    return () => {
      ro?.disconnect();
    };
  }, []);
  useEffect(() => {
    if (typeof ResizeObserver !== "undefined") return;
    const el = feedRef.current;
    if (!el) return;
    if (isOccludedScrollParentSize(el.clientHeight, el.offsetHeight)) return;
    applyFeedResizeFollow({
      pinned: pinnedToBottomRef.current,
      settling: scrollSettlingRef.current,
      scrollTop: el.scrollTop,
      scrollHeight: el.scrollHeight,
    });
  }, [items]);

  // On session switch: stop follow thrash, glue to true bottom until height is
  // stable for ~5 frames (or ~1s wall-clock), then re-lock stick-to-bottom.
  useLayoutEffect(() => {
    const el = feedRef.current;
    if (!el || !activeSessionId) return;
    pinnedToBottomRef.current = true;
    scrollReleasedByGestureRef.current = false;
    prevFeedScrollTopRef.current = null;
    scrollSettlingRef.current = true;
    setFeedSettled(false);
    setShowJumpToBottom(false);
    const scrollToEnd = scrollFeedToEndRef.current;
    if (scrollToEnd) {
      programmaticScrollRef.current = true;
      scrollToEnd();
    } else {
      programmaticScrollRef.current = true;
      el.scrollTop = scrollToFeedEnd(el.scrollHeight, el.clientHeight);
    }
    let frame = 0;
    let stableFrames = 0;
    let lastHeight = el.scrollHeight;
    let rafId = 0;
    const startedAtMs = performance.now();
    const settle = () => {
      const node = feedRef.current;
      if (!node) {
        scrollSettlingRef.current = false;
        setFeedSettled(true);
        return;
      }
      if (!scrollSettlingRef.current || scrollReleasedByGestureRef.current) {
        return;
      }
      const height = node.scrollHeight;
      const step = settleFrameResult({
        height,
        lastHeight,
        stableFrames,
        frame,
        startedAtMs,
        nowMs: performance.now(),
      });
      stableFrames = step.stableFrames;
      frame = step.frame;
      lastHeight = height;
      const scrollToEnd = scrollFeedToEndRef.current;
      if (scrollToEnd) {
        programmaticScrollRef.current = true;
        scrollToEnd();
      } else {
        programmaticScrollRef.current = true;
        node.scrollTop = height;
      }
      pinnedToBottomRef.current = true;
      if (step.done) {
        scrollSettlingRef.current = false;
        setFeedSettled(true);
        publishJumpVisibilityRef.current();
        return;
      }
      rafId = requestAnimationFrame(settle);
    };
    rafId = requestAnimationFrame(settle);
    return () => {
      cancelAnimationFrame(rafId);
      scrollSettlingRef.current = false;
      setFeedSettled(true);
    };
  }, [activeSessionId]);

  // Alt-tab / blur can zero the feed height and reset scrollTop. Restore the
  // last offset (or re-stick to bottom if still pinned) after focus paints.
  useEffect(() => {
    let saved = 0;
    let raf1 = 0;
    let raf2 = 0;
    const remember = () => {
      const node = feedRef.current;
      if (node) saved = node.scrollTop;
    };
    const restore = () => {
      window.cancelAnimationFrame(raf1);
      window.cancelAnimationFrame(raf2);
      raf1 = window.requestAnimationFrame(() => {
        raf2 = window.requestAnimationFrame(() => {
          const node = feedRef.current;
          if (!node) return;
          programmaticScrollRef.current = true;
          node.scrollTop = restoreFeedScrollAfterFocus({
            savedScrollTop: saved,
            pinned: pinnedToBottomRef.current,
            settling: scrollSettlingRef.current,
            scrollHeight: node.scrollHeight,
          });
        });
      });
    };
    const onVisibility = () => {
      if (document.hidden) remember();
      else restore();
    };
    window.addEventListener("blur", remember);
    window.addEventListener("focus", restore);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.cancelAnimationFrame(raf1);
      window.cancelAnimationFrame(raf2);
      window.removeEventListener("blur", remember);
      window.removeEventListener("focus", restore);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  // Opening a file used to unmount the chat column; coming back remounted the
  // virtualizer at 0. Chat stays mounted (hidden). Still restore the last
  // offset in case the hide pass reported a 0-height scroll parent.
  const fileTabScrollTopRef = useRef(0);
  const prevActiveTabRef = useRef(activeTab);
  useLayoutEffect(() => {
    const prev = prevActiveTabRef.current;
    prevActiveTabRef.current = activeTab;
    const node = feedRef.current;
    if (!node) return;
    if (isChatColumnActive(prev) && !isChatColumnActive(activeTab)) {
      fileTabScrollTopRef.current = node.scrollTop;
      return;
    }
    if (!isChatColumnActive(prev) && isChatColumnActive(activeTab)) {
      programmaticScrollRef.current = true;
      node.scrollTop = restoreFeedScrollAfterFocus({
        savedScrollTop: fileTabScrollTopRef.current,
        pinned: pinnedToBottomRef.current,
        settling: scrollSettlingRef.current,
        scrollHeight: node.scrollHeight,
      });
    }
  }, [activeTab]);

  const contextUsageFetchGenRef = useRef(0);
  const fetchContextUsage = () => {
    if (!activeSessionId) return;
    const fetchSid = activeSessionId;
    const fetchGen = contextUsageFetchGenRef.current;
    return api.getContextUsage()
      .then((res) => {
        if (fetchGen !== contextUsageFetchGenRef.current) return;
        if (activeSessionIdRef.current !== fetchSid) return;
        // Fresh sessions can return partial/non-finite payloads; keep the
        // panel in its loading state rather than rendering NaN or crashing.
        const usage = normalizeContextUsage(res);
        if (!usage) {
          console.warn("Ignoring malformed context usage payload:", res);
        }
        setContextUsage(usage);
      })
      .catch((err) => console.error("Failed to fetch context usage:", err));
  };

  useEffect(() => {
    // Blank prior session meters immediately (StatusBar tok/$ already clears
    // on harness-session-changed); fence ignores late responses for A under B.
    contextUsageFetchGenRef.current += 1;
    setContextUsage(null);
    fetchContextUsage();

    const h = () => fetchContextUsage();
    window.addEventListener("harness-context-changed", h);
    return () => window.removeEventListener("harness-context-changed", h);
  }, [activeSessionId]);

  usePolling(fetchContextUsage, 5000, { enabled: showContextPanel && !!activeSessionId });

  useSessionSwitch({
    activeSessionId,
    onArtifacts,
    clearChatEventsPoll,
    itemsRef,
    transcriptStaleRef,
    cachedSessionIdRef,
    transcriptLoadGenRef,
    transcriptFpRef,
    streamGenRef,
    streamSessionIdRef,
    lastAppliedCursorRef,
    ringGenerationRef,
    chatEventsPollTimerRef,
    chatEventsLiveCancelRef,
    applyStreamEventRef,
    flushTypewriterRef,
    maybeRunQueuedResumeRef,
    maybeDrainQueueRef,
    ensureChatEventsReattachRef,
    cancelRef,
    localStreamActiveRef,
    detachedBusyRef,
    userStoppedRef,
    turnSettledRef,
    abandonStaleLocalStreamRef,
    resumeQueuedRef,
    approvedCommandRetryRef,
    runnerBusyPollGenRef,
    typeRafRef,
    typeBufRef,
    typeDoneRef,
    setItems,
    setTranscriptStale,
    setTurnOpen,
    setStatus,
    setCompactingStatus,
    setEditingIndex,
    setCanRevertEdit,
    setEditNotice,
    setEditBusy,
    setInput,
    composerInputRef,
    setAttachedImages,
    attachedImagesRef,
    setWikiPrepared,
    setMemoryProposals,
    setDistillNotice,
    setUploadError,
    setWaitHint,
    setPendingJobIds,
    setBackendPendingSwarms,
    setSessionSwitchPending,
    clearSafeTimeouts,
    setTurnLifecycle,
    setTerminalCause,
    recoveryDispatchingRef,
    recoveryContextRef,
  });


  useRunnersBusyPoll({
    activeSessionId,
    clearChatEventsPoll,
    itemsRef,
    cachedSessionIdRef,
    transcriptFpRef,
    localStreamActiveRef,
    detachedBusyRef,
    userStoppedRef,
    runnerBusyPollGenRef,
    chatEventsPollTimerRef,
    chatEventsLiveCancelRef,
    ensureChatEventsReattachRef,
    turnSettledRef,
    abandonStaleLocalStreamRef,
    setItems,
    setTranscriptStale,
    setTurnOpen,
    setStatus,
    setCompactingStatus,
    setWaitHint,
    setBackendPendingSwarms,
  });


  useEffect(() => {
    setPendingJobIds([]);
    processedSwarmJobIdsRef.current = [];
    setBackendPendingSwarms(false);
    setSwarmLiveJobs([]);
    if (activeSessionId) {
      // Peek first for pending_swarms / latch visibility. Consume only once we
      // commit to scheduling resume so a mid-flight switch cannot steal it.
      const requestSid = activeSessionId;
      const requestGen = transcriptLoadGenRef.current;
      const stillCurrent = () => (
        activeSessionIdRef.current === requestSid
        && transcriptLoadGenRef.current === requestGen
      );
      api.getSessionState({ sessionId: requestSid })
        .then((res) => {
          if (!stillCurrent() || !res) return;
          setBackendPendingSwarms(!!res.pending_swarms);
          // Restore Still working… when switching into a pause-point session
          // (pending_swarms / state===awaiting_swarm). Stop suppresses restore.
          if (
            sessionStateShowsAwaitingSwarm({
              state: res.state,
              pendingSwarms: !!res.pending_swarms,
              userStopped: userStoppedRef.current,
            })
          ) {
            setTurnOpen(false);
            setStatus("awaiting_swarm");
            setWaitHint(SWARM_AWAIT_HINT);
          }
          // resume_pending is an EXPLICIT one-shot latch from the self-edit
          // restart path (backend /api/session/persist or /api/restart) -- NOT
          // "transcript ends on a user turn". Peek only here; consume happens
          // inside the delayed kick so clearSafeTimeouts / switch cannot steal it.
          if (!res.resume_pending) return;
          armResumeKick({
            getSessionState: api.getSessionState,
            resume: () => resumeTriggerRef.current(),
            stillCurrent,
            ownerStillActive: () => activeSessionIdRef.current === requestSid,
            sessionId: requestSid,
            schedule: setSafeTimeout,
          });
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

  useEffect(() => {
    const onAdd = (e: Event) => {
      const detail = (e as CustomEvent<{ text?: string; label?: string }>).detail || {};
      const text = String(detail.text || "").trim();
      const label = String(detail.label || "").trim();
      if (!text || !label) return;
      const sid = activeSessionIdRef.current || "_draft";
      putTerminalSelection(sid, label, text);
      setInput((prev) => appendTerminalMention(prev, label));
      taRef.current?.focus();
    };
    window.addEventListener(ADD_TERMINAL_SELECTION_EVENT, onAdd as EventListener);
    return () => window.removeEventListener(ADD_TERMINAL_SELECTION_EVENT, onAdd as EventListener);
  }, []);

  // Command palette (and other chrome) can clear/compact without going through
  // the composer slash path. Clear must stay distinct from harness-new-session.
  useEffect(() => {
    const onClearTranscript = () => {
      setEditingIndex(null);
      setItems([]);
      itemsRef.current = [];
      transcriptFpRef.current = "";
      if (activeSessionId) writeTranscriptCache(activeSessionId, []);
      setTurnOpen(false);
      setWaitHint(null);
      setStatus("idle");
      setCompactingStatus(null);
    };
    const onCompactSession = () => {
      const compactSid = activeSessionIdRef.current;
      const thinkingId = newThinkingId();
      setEditingIndex(null);
      setStatus("thinking");
      setItems((p) => [
        ...p,
        {
          kind: "thinking",
          text: "Compacting session context on backend...",
          id: thinkingId,
        },
      ]);
      api.compactSession()
        .then((res) => {
          if (!shouldApplyCompactSettle({
            requestSessionId: compactSid,
            activeSessionId: activeSessionIdRef.current,
          })) return;
          setStatus("done");
          setItems((p) => [
            ...p.filter((it) => !(it.kind === "thinking" && it.id === thinkingId)),
            {
              kind: "msg",
              msg: {
                role: "assistant",
                text: formatCompactCompleteMessage(res.before_tokens, res.after_tokens),
              },
            },
          ]);
        })
        .catch((err) => {
          if (!shouldApplyCompactSettle({
            requestSessionId: compactSid,
            activeSessionId: activeSessionIdRef.current,
          })) return;
          setStatus("error");
          setItems((p) => [
            ...p.filter((it) => !(it.kind === "thinking" && it.id === thinkingId)),
            {
              kind: "msg",
              msg: {
                role: "assistant",
                text: formatCompactErrorMessage(err),
              },
            },
          ]);
        });
    };
    window.addEventListener("harness-clear-transcript", onClearTranscript);
    window.addEventListener("harness-compact-session", onCompactSession);
    return () => {
      window.removeEventListener("harness-clear-transcript", onClearTranscript);
      window.removeEventListener("harness-compact-session", onCompactSession);
    };
  }, [activeSessionId]);

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

  // Load workspace files + folders for @-mention dropdown
  useEffect(() => {
    api.getWorkspaceFiles()
      .then((res) => {
        if (res && res.files) {
          setAllFiles(res.files);
          setAllFolders(Array.isArray(res.folders) ? res.folders : []);
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

  // Filter files + folders based on @-mention search text (capped; no full tree dump)
  useEffect(() => {
    if (mentionSearch !== null) {
      setFilteredFiles(filterMentionPaths(allFiles, mentionSearch, 10));
      setFilteredFolders(filterMentionPaths(allFolders, mentionSearch, 8));
      setSelectedFileIndex(0);
    } else {
      setFilteredFiles([]);
      setFilteredFolders([]);
    }
  }, [mentionSearch, allFiles, allFolders]);

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

  const showCodebaseMention =
    mentionSearch !== null && codebaseMentionMatches(mentionSearch);
  const codebaseMentionOffset = showCodebaseMention ? 1 : 0;

  // Keep selectedFileIndex bounded within combined total mentions count
  useEffect(() => {
    const total =
      codebaseMentionOffset +
      filteredFiles.length +
      filteredFolders.length +
      symbolResults.length;
    if (selectedFileIndex >= total && total > 0) {
      setSelectedFileIndex(clampSelectIndex(selectedFileIndex, total));
    }
  }, [
    codebaseMentionOffset,
    filteredFiles,
    filteredFolders,
    symbolResults,
    selectedFileIndex,
  ]);

  const insertMention = (fileName: string) => {
    if (mentionIndex === -1) return;
    const { next, cursor } = buildMentionInsert(
      input,
      mentionIndex,
      taRef.current?.selectionStart || mentionIndex,
      fileName,
    );
    setInput(next);
    setMentionSearch(null);
    setMentionIndex(-1);

    setTimeout(() => {
      if (taRef.current) {
        taRef.current.focus();
        taRef.current.setSelectionRange(cursor, cursor);
      }
    }, 10);
  };

  const insertFolder = (folderPath: string) => {
    if (mentionIndex === -1) return;
    const { next, cursor } = buildFolderInsert(
      input,
      mentionIndex,
      taRef.current?.selectionStart || mentionIndex,
      folderPath,
    );
    setInput(next);
    setMentionSearch(null);
    setMentionIndex(-1);

    setTimeout(() => {
      if (taRef.current) {
        taRef.current.focus();
        taRef.current.setSelectionRange(cursor, cursor);
      }
    }, 10);
  };

  const insertSymbol = (symbolName: string) => {
    if (mentionIndex === -1) return;
    const { next, cursor } = buildSymbolInsert(
      input,
      mentionIndex,
      taRef.current?.selectionStart || mentionIndex,
      symbolName,
    );
    setInput(next);
    setMentionSearch(null);
    setMentionIndex(-1);

    setTimeout(() => {
      if (taRef.current) {
        taRef.current.focus();
        taRef.current.setSelectionRange(cursor, cursor);
      }
    }, 10);
  };

  const insertCodebase = () => {
    if (mentionIndex === -1) return;
    const filter =
      mentionSearch !== null
        ? codebaseQueryFromMentionSearch(mentionSearch)
        : undefined;
    const { next, cursor } = buildCodebaseInsert(
      input,
      mentionIndex,
      taRef.current?.selectionStart || mentionIndex,
      filter,
    );
    setInput(next);
    setMentionSearch(null);
    setMentionIndex(-1);

    setTimeout(() => {
      if (taRef.current) {
        taRef.current.focus();
        taRef.current.setSelectionRange(cursor, cursor);
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
    const trigger = detectComposerTrigger(val, cursorPosition);
    if (trigger.kind === "slash") {
      setSlashSearch(trigger.query);
      setMentionSearch(null);
      setMentionIndex(-1);
      return;
    }
    setSlashSearch(null);
    if (trigger.kind === "mention") {
      setMentionSearch(trigger.query);
      setMentionIndex(trigger.atIndex);
      return;
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
    const items = Array.from(e.dataTransfer.items || []);

    setUploadError(null);
    const repo = (config?.repo || "").replace(/\/+$/, "");
    const mentions: string[] = [];
    let addedCount = attachedImages.length;

    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      const isImage = file.type.startsWith("image/");
      const osPath = resolveDroppedOsPath(file as { path?: string });
      let entry: DirectoryEntryLike | null = null;
      try {
        entry = (items[i]?.webkitGetAsEntry?.() as DirectoryEntryLike | null) || null;
      } catch {
        entry = null;
      }
      const isDirectory = !!(entry && entry.isDirectory)
        || await droppedPathIsDirectory(osPath);

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
          flashUploadError(uploadErrorMessage(err, "Image upload failed"));
        }
        continue;
      }

      if (isDirectory) {
        const plan = droppedDirectoryPlan({ osPath, repo });
        if (plan.kind === "mention") {
          mentions.push(plan.token);
          continue;
        }
        if (entry?.isDirectory) {
          try {
            const collected = await collectFilesFromDirectoryEntry(entry);
            if (collected.files.length > 0) {
              for (const inner of collected.files) {
                try {
                  const uploaded = await api.uploadImage(inner.file);
                  const token = mentionTokenForDroppedPath({
                    osPath: "",
                    repo,
                    uploadedPath: uploaded.path,
                  });
                  if (token) mentions.push(token);
                  else flashUploadError("Dropped file could not be attached");
                } catch (err) {
                  console.error("Failed to upload dropped folder file:", err);
                  flashUploadError(uploadErrorMessage(err, "File upload failed"));
                }
              }
              if (collected.truncated) {
                flashUploadError(
                  `Attached the first ${DROP_FOLDER_FILE_CAP} files from that folder.`,
                );
              }
              continue;
            }
          } catch (err) {
            console.error("Failed to read dropped folder:", err);
          }
        }
        if (plan.kind === "open-workspace") {
          openAgentWorkspace(plan.path);
          continue;
        }
        flashUploadError("Could not open that folder.");
        continue;
      }

      // Non-image files become an @-mention the agent reads. If the file lives
      // INSIDE the open workspace, use a plain repo-relative @path (the backend
      // resolves it directly). Otherwise upload it into the workspace-readable
      // store and reference the uploaded path -- so external drops work too.
      const insideToken = mentionTokenForDroppedPath({ osPath, repo });
      if (insideToken) {
        mentions.push(insideToken);
        continue;
      }
      try {
        const uploaded = await api.uploadImage(file);
        const token = mentionTokenForDroppedPath({
          osPath: "",
          repo,
          uploadedPath: uploaded.path,
        });
        if (token) mentions.push(token);
        else flashUploadError("Dropped file could not be attached");
      } catch (err) {
        console.error("Failed to upload dropped file:", err);
        flashUploadError(uploadErrorMessage(err, "File upload failed"));
      }
    }

    if (mentions.length > 0) {
      setInput((prev) => appendMentionsToInput(prev, mentions));
      setTimeout(() => taRef.current?.focus(), 10);
    }
  };

  const handleEditMessage = (idx: number, originalText: string) => {
    if (editBusy) return;
    const userOrdinal = userOrdinalBeforeIndex(items, idx);
    const busyEdit = composerBusy;
    if (busyEdit) {
      setEditNotice(EDIT_BUSY_PROGRESS_NOTICE);
    }
    setEditBusy(true);
    runEditMessageFlow({
      composerBusy: busyEdit,
      idx,
      userOrdinal,
      originalText,
      stopLocal,
      interruptSession: () => api.interruptSession(),
      rewindSession: (ordinal) => api.rewindSession(ordinal),
    })
      .then((result) => {
        if (result.kind === "interrupt_failed" || result.kind === "rewind_failed") {
          setEditNotice(result.notice);
          return;
        }
        setItems((prev) => prev.slice(0, result.truncateToIndex));
        setEditingIndex(result.truncateToIndex);
        setInput(result.prefill);
        setCanRevertEdit(true);
        setEditNotice(result.notice);
        // Checkpoint restore mutates the worktree; refresh Files / SCM / editors.
        if (result.workspace_restored) {
          notifyWorkspaceMutated();
        }
        setTimeout(() => taRef.current?.focus(), 10);
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
        if (res.workspace_restored) {
          notifyWorkspaceMutated();
        }
      })
      .catch((err) => {
        setEditNotice((err as Error)?.message || "Revert failed.");
      })
      .finally(() => setEditBusy(false));
  };

  const handleCancelEdit = () => {
    // Cancel always restores the stashed pre-edit branch when one exists.
    // Resubmit (Send / Resubmit button) is what restarts the agent loop.
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

    const totalMentions =
      codebaseMentionOffset +
      filteredFiles.length +
      filteredFolders.length +
      symbolResults.length;
    if (mentionSearch !== null && totalMentions > 0) {
      if (e.key === "ArrowDown") {
        setSelectedFileIndex((prev) => cycleSelectIndex(prev, 1, totalMentions));
        e.preventDefault();
        return;
      }
      if (e.key === "ArrowUp") {
        setSelectedFileIndex((prev) => cycleSelectIndex(prev, -1, totalMentions));
        e.preventDefault();
        return;
      }
      if (e.key === "Enter") {
        if (showCodebaseMention && selectedFileIndex === 0) {
          insertCodebase();
        } else {
          const idx = selectedFileIndex - codebaseMentionOffset;
          if (idx < filteredFiles.length) {
            insertMention(filteredFiles[idx]);
          } else if (idx < filteredFiles.length + filteredFolders.length) {
            const folderIdx = idx - filteredFiles.length;
            insertFolder(filteredFolders[folderIdx]);
          } else {
            const symIdx = idx - filteredFiles.length - filteredFolders.length;
            if (symbolResults[symIdx]) {
              insertSymbol(symbolResults[symIdx].name);
            }
          }
        }
        e.preventDefault();
        return;
      }
    }

    if (slashSearch !== null) {
      const matchingSlash = filterSlashCommands(allSlashCommands, slashSearch);
      if (matchingSlash.length > 0) {
        if (e.key === "ArrowDown") {
          setSelectedSlashIndex((prev) => cycleSelectIndex(prev, 1, matchingSlash.length));
          e.preventDefault();
          return;
        }
        if (e.key === "ArrowUp") {
          setSelectedSlashIndex((prev) => cycleSelectIndex(prev, -1, matchingSlash.length));
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
      // Mouth is isPilotMouthBusy — awaiting_swarm stays Send, not Steer.
      const busy = composerBusy;
      // While a turn is running, plain Enter STEERS (redirects the current turn);
      // Cmd/Ctrl+Enter QUEUES (runs after the current turn finishes); Alt+Enter
      // INTERRUPTS (stop this turn, then run the typed prompt next). When idle,
      // Enter always sends a normal turn.
      const enterAction = composerEnterAction({
        busy,
        metaOrCtrl: e.metaKey || e.ctrlKey,
        altKey: e.altKey,
        hasText: Boolean(input.trim()),
      });
      if (enterAction === "noop") {
        return;
      }
      if (enterAction === "queue") {
        handleQueueAdd();
        return;
      }
      if (enterAction === "interrupt") {
        send("interrupt");
        return;
      }
      send();
    }
  };

  const handleSwarmResult = (d: any) => {
    const job_id = d.job_id;
    if (!job_id) return;

    // Ref is a fast path only — applySwarmResultToItems is the real idempotency
    // gate so poll/SSE/rehydrate stay safe after session-switch clears the ref.
    if (!processedSwarmJobIdsRef.current.includes(job_id)) {
      processedSwarmJobIdsRef.current.push(job_id);
    }

    setPendingJobIds((p) => p.filter(id => id !== job_id));

    setItems((prevItems) => applySwarmResultToItems(prevItems, d));
  };

  const swarmResultsPending = pendingJobIds.length > 0 || backendPendingSwarms;
  // Guarded via usePolling: each tick fires two sequential backend calls
  // (results + session state), so during a swarm this was the single heaviest
  // always-on poller. The in-flight guard keeps at most one round-trip pair
  // outstanding instead of stacking them onto an already-busy backend.
  usePolling(
    () => {
      const pollSid = activeSessionId;
      const pollGen = transcriptLoadGenRef.current;
      let pollResumeFired = false;
      return api.getSwarmResults()
        .then((res) => {
          // Same session+gen fence as swarmLive: do not apply pilot_resume /
          // swarm_result / wiki / memory into a session we already left.
          if (!shouldApplySwarmLiveMerge({
            pollGen,
            currentGen: transcriptLoadGenRef.current,
            pollSessionId: pollSid,
            cachedSessionId: cachedSessionIdRef.current,
            activeSessionId: cachedSessionIdRef.current,
          })) {
            return null;
          }
          if (res && res.results && res.results.length > 0) {
            // At most one triggerResume per poll tick (first pilot_resume wins;
            // extras only set resumeQueuedRef). Mid-stream path already coalesces.
            res.results.forEach((evt) => {
              if (!shouldApplySwarmLiveMerge({
                pollGen,
                currentGen: transcriptLoadGenRef.current,
                pollSessionId: pollSid,
                cachedSessionId: cachedSessionIdRef.current,
                activeSessionId: cachedSessionIdRef.current,
              })) {
                return;
              }
              const action = classifySwarmPollEvent(evt);
              if (action.kind === "swarm_result") {
                handleSwarmResult(action.data);
              } else if (action.kind === "pending_review") {
                setItems((p) => appendPendingReview(p, action.data));
                focusReviewTabAndRefresh();
              } else if (action.kind === "pilot_resume") {
                // Background job finished while the session was idle / awaiting.
                // Backend already extended history; kick keep-alive so the pilot
                // continues ("looking…") without a user prompt. Stop must not
                // leave Looking… painted when the kick is suppressed.
                const resumeAct = pilotResumePollAction({
                  userStopped: userStoppedRef.current,
                  alreadyFired: pollResumeFired,
                });
                if (resumeAct === "suppress_clear_hint") {
                  setWaitHint((prev) => clearSwarmAwaitWaitHint(prev));
                } else if (resumeAct === "fire_looking") {
                  pollResumeFired = true;
                  setWaitHint(PILOT_LOOKING_HINT);
                  resumeTriggerRef.current();
                } else {
                  resumeQueuedRef.current = true;
                }
              } else if (action.kind === "distilled" || action.kind === "wiki_auto") {
                const notice = action.notice;
                setDistillNotice(notice);
                setSafeTimeout(() => setDistillNotice((cur) => (cur === notice ? null : cur)), 8000);
              } else if (action.kind === "wiki_prepare") {
                setWikiPrepared({ pages: action.pages, autoIngested: false });
              } else if (action.kind === "memory_propose") {
                setMemoryProposals((prev) =>
                  appendMemoryProposal(prev, {
                    id: action.id,
                    text: action.text,
                    category: action.category,
                    refine: action.refine,
                  }),
                );
              }
            });
          }
          // Progressive nested worker actions land on local jobs via
          // /api/swarm/live; fold them under run_implement / run_parallel cards.
          // Fence with the same generation + active-session guards as
          // useSessionSwitch so a late poll from a prior session cannot mutate
          // the current transcript.
          return api.swarmLive().then((live) => {
            if (!shouldApplySwarmLiveMerge({
              pollGen,
              currentGen: transcriptLoadGenRef.current,
              pollSessionId: pollSid,
              cachedSessionId: cachedSessionIdRef.current,
              activeSessionId: cachedSessionIdRef.current,
            })) {
              return null;
            }
            const jobs = Array.isArray(live?.jobs) ? live.jobs : [];
            setSwarmLiveJobs(jobs);
            const hasActions = jobs.some(
              (j) => Array.isArray(j.actions) && j.actions.length > 0,
            );
            const terminalIds = terminalJobIdsFromSwarmLive(jobs);
            const hasTerminal = terminalIds.length > 0;
            const recoveryIds = terminalJobIdsNeedingResultRecovery(
              pendingJobIdsRef.current,
              terminalIds,
              itemsRef.current,
            );
            const pruneTerminalTrackers = () => {
              if (!hasTerminal) return;
              setPendingJobIds((prev) => {
                const next = pruneTerminalJobIds(prev, terminalIds);
                // Sync ref so same-tick getSessionState chrome clear sees the
                // pruned count (useEffect would lag one paint).
                pendingJobIdsRef.current = next;
                return next;
              });
            };
            // Do not drop the last pending id until the one-shot recovery
            // drain has had a chance to surface swarm_result. Otherwise
            // Investigating clears before the failed continuation exists.
            if (hasTerminal && recoveryIds.length === 0) {
              pruneTerminalTrackers();
            }
            if (hasActions || hasTerminal) {
              setItems((prev) => {
                // Re-fence inside the updater: a session switch between the
                // await and React applying this update must not mutate items.
                if (!shouldApplySwarmLiveMerge({
                  pollGen,
                  currentGen: transcriptLoadGenRef.current,
                  pollSessionId: pollSid,
                  cachedSessionId: cachedSessionIdRef.current,
                  activeSessionId: cachedSessionIdRef.current,
                })) {
                  return prev;
                }
                return mergeJobActionsIntoItems(prev, jobs);
              });
            }
            if (recoveryIds.length > 0) {
              const recoverySet = new Set(recoveryIds);
              return api.getSwarmResults()
                .then((recovered) => {
                  if (!shouldApplySwarmLiveMerge({
                    pollGen,
                    currentGen: transcriptLoadGenRef.current,
                    pollSessionId: pollSid,
                    cachedSessionId: cachedSessionIdRef.current,
                    activeSessionId: cachedSessionIdRef.current,
                  })) {
                    return;
                  }
                  for (const evt of recovered?.results || []) {
                    const action = classifySwarmPollEvent(evt);
                    if (
                      action.kind === "swarm_result"
                      && recoverySet.has(String(action.data?.job_id || ""))
                    ) {
                      handleSwarmResult(action.data);
                    } else if (action.kind === "pilot_resume") {
                      const resumeAct = pilotResumePollAction({
                        userStopped: userStoppedRef.current,
                        alreadyFired: pollResumeFired,
                      });
                      if (resumeAct === "suppress_clear_hint") {
                        setWaitHint((prev) => clearSwarmAwaitWaitHint(prev));
                      } else if (resumeAct === "fire_looking") {
                        pollResumeFired = true;
                        setWaitHint(PILOT_LOOKING_HINT);
                        resumeTriggerRef.current();
                      } else {
                        resumeQueuedRef.current = true;
                      }
                    }
                  }
                })
                .catch(() => {})
                .then(() => {
                  if (!shouldApplySwarmLiveMerge({
                    pollGen,
                    currentGen: transcriptLoadGenRef.current,
                    pollSessionId: pollSid,
                    cachedSessionId: cachedSessionIdRef.current,
                    activeSessionId: cachedSessionIdRef.current,
                  })) {
                    return null;
                  }
                  pruneTerminalTrackers();
                  return api.getSessionState();
                });
            }
            return api.getSessionState();
          });
        })
        .then((stateRes) => {
          // Same fence as result merges: a late session-A state must not mutate
          // session-B busy chrome (pending_swarms / awaiting_swarm / Looking…).
          if (
            !stateRes
            || !shouldApplySwarmLiveMerge({
              pollGen,
              currentGen: transcriptLoadGenRef.current,
              pollSessionId: pollSid,
              cachedSessionId: cachedSessionIdRef.current,
              activeSessionId: cachedSessionIdRef.current,
            })
          ) {
            return;
          }
          setBackendPendingSwarms(!!stateRes.pending_swarms);
          const chrome = swarmResultsAwaitChromeClear({
            pendingSwarms: !!stateRes.pending_swarms,
            localPendingJobCount: pendingJobIdsRef.current.length,
            userStopped: userStoppedRef.current,
            cancelArmed: !!cancelRef.current,
          });
          if (chrome.clearAwaitStatus) {
            setStatus((prev) => (prev === "awaiting_swarm" ? "done" : prev));
          }
          if (chrome.clearWaitHint) {
            setWaitHint((prev) => clearSwarmAwaitWaitHint(prev));
          }
        })
        .catch((err) => {
          console.error("Failed to poll swarm results:", err);
        });
    },
    2500,
    { enabled: swarmResultsPending },
  );

  // Append decoded text to the streaming assistant bubble (one state update).
  // findStreamingBubbleIdx skips thinking/codegraph decoration for mid-drain
  // typewriter, but tool/prep cards are a hard fence — post-tool deltas open
  // a new bubble below the investigation group.
  const appendStreamingText = (chunk: string) => {
    if (!chunk) return;
    setItems((p) => {
      const next = appendStreamingTextToItems(p, chunk, { isPlan: planTurnRef.current });
      // Keep itemsRef aligned inside the updater so synchronous replay /
      // flush-then-seal never reads an effect-lagged sealed turn.
      itemsRef.current = next;
      return next;
    });
  };

  // Drain the whole arrived chunk on a 33ms Hermes timer (Codex: no drip).
  const startTypewriter = () => {
    startTypewriterLoop(
      { typeBufRef, typeRafRef, typeDoneRef },
      appendStreamingText,
      scheduleStreamPaint,
    );
  };

  const flushTypewriter = () => {
    flushTypewriterBuffer(
      { typeBufRef, typeRafRef, typeDoneRef },
      appendStreamingText,
      cancelStreamPaint,
    );
  };
  flushTypewriterRef.current = flushTypewriter;

  const liveNonLocalSwarmJobIds = () =>
    pendingJobIdsRef.current.filter((id) => !id.startsWith("local-swarm-"));

  const sealTurnItems = (items: Item[], liveIds: string[]) =>
    reconcileOrphanInvestigationCards(
      finalizeOrphanSwarmPills(
        hoistCardsBeforeTrailingFinals(sealOpenStreamSurfaces(items)),
        liveIds,
      ),
      liveIds,
    );

  const applyLocalTurnSettle = (settle: TurnSettle, items: Item[], liveIds: string[]) => {
    const sealed = sealTurnItems(items, liveIds);
    if (!settle.explanation) return sealed;
    return appendTurnTerminal(sealed, {
      id: `turn-term-${streamGenRef.current}`,
      cause: settle.cause,
      state: settle.lifecycle,
      text: settle.explanation,
    });
  };

  const recordTurnSettle = (settle: TurnSettle) => {
    lastSettleRef.current = settle;
    setTurnLifecycle(settle.lifecycle);
    setTerminalCause(settle.cause === "natural" ? null : settle.cause);
    syncConversationTurnFailureDiagnostic(
      settle,
      cachedSessionIdRef.current || activeSessionId || undefined,
    );
    recoveryDispatchingRef.current = false;
    if (recoveryControlsAvailable(settle.lifecycle)) {
      const sessionId = cachedSessionIdRef.current || activeSessionId || "";
      recoveryContextRef.current = sessionId
        ? { sessionId, generation: streamGenRef.current }
        : null;
    } else {
      recoveryContextRef.current = null;
    }
  };

  abandonStaleLocalStreamRef.current = () => {
    if (userStoppedRef.current) return;
    const settle = settleFromStaleLocalAbandon({
      turnSettled: turnSettledRef.current,
      userStopped: userStoppedRef.current,
      hasPartialAnswer: hasPartialAssistantAnswer(itemsRef.current),
    });
    if (settle.kind === "already_settled") return;
    turnSettledRef.current = true;
    resumeQueuedRef.current = false;
    detachedBusyRef.current = false;
    clearChatEventsPoll();
    streamGenRef.current += 1;
    cancelRef.current?.();
    cancelRef.current = null;
    localStreamActiveRef.current = false;
    flushTypewriter();
    setTurnOpen(false);
    setWaitHint(null);
    setStatus(settle.status);
    setCompactingStatus(null);
    recordTurnSettle(settle);
    const liveIds = liveNonLocalSwarmJobIds();
    setPendingJobIds(liveIds);
    setItems((p) => applyLocalTurnSettle(settle, p, liveIds));
    const sid = cachedSessionIdRef.current;
    if (!sid) return;
    void api.sessionTranscript(sid).then((tres) => {
      if (cachedSessionIdRef.current !== sid) return;
      const loadedItems = transcriptResponseToItems(tres);
      setItems((prev) => {
        if (cachedSessionIdRef.current !== sid) return prev;
        const next = applyLocalTurnSettle(
          settle,
          mergeTranscriptItems(prev, loadedItems),
          liveIds,
        );
        const fp = transcriptFingerprint(next);
        if (fp === transcriptFpRef.current) return prev;
        transcriptFpRef.current = fp;
        itemsRef.current = next;
        writeTranscriptCache(sid, next);
        return next;
      });
      setTranscriptStale(false);
    }).catch(() => {});
  };



  // Shared path for live SSE and mid-turn chatEvents reattach. Callers must
  // enforce session/generation guards before invoking.
  // Item transforms live in conversation/streamApply.ts (pure); chrome/side
  // effects are wired via createApplyStreamEvent.
  const applyStreamEvent = createApplyStreamEvent({
    setCompactingStatus,
    setItems,
    setDistillNotice,
    setWikiPrepared,
    setMemoryProposals,
    setWaitHint,
    setStatus,
    setTurnOpen,
    setPendingJobIds,
    pendingJobIdsRef,
    setSafeTimeout,
    itemsRef,
    planTurnRef,
    turnSettledRef,
    userStoppedRef,
    resumeQueuedRef,
    typeBufRef,
    flushTypewriter,
    startTypewriter,
    appendStreamingText,
    setCard,
    onArtifacts,
    onJobChange,
    handleSwarmResult,
    refreshQueue,
    fetchContextUsage,
    recordTurnSettle,
  });
  applyStreamEventRef.current = applyStreamEvent;


  const executeSend = (msg: string, useAuto: boolean, usePlan: boolean = false, resume: boolean = false, imagesOverride?: { path: string; name: string; previewUrl: string }[]) => {
    // Stale transcript = prior session still on screen while B hydrates.
    // Never send into the wrong session.
    const gate = executeSendGate({
      transcriptStale,
      resume,
      userStopped: userStoppedRef.current,
    });
    if (gate === "stale") {
      recoveryDispatchingRef.current = false;
      return;
    }
    if (gate === "stopped_resume") {
      // Keep-alive after Stop must not re-arm the turn.
      resumeQueuedRef.current = false;
      recoveryDispatchingRef.current = false;
      return;
    }
    if (!resume) {
      // Real user/autopilot send clears the Stop hold so thinking can run again.
      userStoppedRef.current = false;
    }
    planTurnRef.current = usePlan;
    turnSettledRef.current = false;
    lastSettleRef.current = null;
    recoveryContextRef.current = null;
    setTurnLifecycle("running");
    setTerminalCause(null);
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
    // Leaving swarm-await chrome: next model turn owns busy (TTFT / tools).
    setWaitHint(null);
    const streamer = resume
      ? (cb: any, done: any, err: any) => api.resume(cb, done, err)
      : useAuto
      ? (cb: any, done: any, err: any) => api.auto(msg, cb, done, err, imgPaths)
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
         const doneDec = streamOnDoneDecision({
           turnSettled: turnSettledRef.current,
           userStopped: userStoppedRef.current,
         });
         if (doneDec.kind === "abort_error") {
           const settle = settleFromTransportEof({
             turnSettled: false,
             userStopped: false,
             hasPartialAnswer: hasPartialAssistantAnswer(itemsRef.current),
           });
           if (settle.kind === "already_settled") return;
           turnSettledRef.current = true;
           setTurnOpen(false);
           setWaitHint(null);
           setStatus(settle.status);
           recordTurnSettle(settle);
           const liveIds = liveNonLocalSwarmJobIds();
           setPendingJobIds(liveIds);
           setItems((p) => applyLocalTurnSettle(settle, p, liveIds));
         } else {
           // Authoritative settle already won — preserve error/interrupted
           // chrome and the terminal chip. Never reseal as success.
           turnSettledRef.current = true;
           setTurnOpen(false);
           const liveIds = liveNonLocalSwarmJobIds();
           const liveJobs = liveIds.some((id) => Boolean(id));
           setPendingJobIds(liveIds);
           const lastSettle = lastSettleRef.current;
           const preserveInterrupt = lastSettle?.lifecycle === "error"
             || lastSettle?.lifecycle === "interrupted"
             || lastSettle?.lifecycle === "aborted";
           setStatus((prev) => alreadySettledOnDoneStatus({
             prev,
             liveJobs,
             userStopped: userStoppedRef.current,
           }));
           if (liveJobs && !userStoppedRef.current && !preserveInterrupt) {
             setWaitHint((prev) => prev || SWARM_AWAIT_HINT);
           } else {
             setWaitHint(null);
           }
           setItems((p) => {
             const sealed = sealTurnItems(p, liveIds);
             if (!lastSettle?.explanation) return sealed;
             return appendTurnTerminal(sealed, {
               id: `turn-term-${streamGenRef.current}`,
               cause: lastSettle.cause,
               state: lastSettle.lifecycle,
               text: lastSettle.explanation,
             });
           });
         }
         cancelRef.current = null;
         localStreamActiveRef.current = false;
         setCompactingStatus(null);
         maybeRunApprovedCommandRetryRef.current();
         maybeRunQueuedResume();
         maybeDrainQueue();
       },
       (streamErr: any) => {
         if (!streamLive()) return;
         flushTypewriter();
         const errDec = streamOnErrorDecision({
           turnSettled: turnSettledRef.current,
           userStopped: userStoppedRef.current,
         });
         if (errDec.kind === "abort_error") {
           const namedCause = (streamErr as { terminal_cause?: unknown } | null)?.terminal_cause;
           const settle = settleFromStreamError(
             streamErrorText(streamErr, namedCause),
             namedCause,
           );
           turnSettledRef.current = true;
           setTurnOpen(false);
           setWaitHint(null);
           recordTurnSettle(settle);
           const liveIds = liveNonLocalSwarmJobIds();
           setPendingJobIds(liveIds);
           setItems((p) => applyLocalTurnSettle(settle, p, liveIds));
           setStatus("error");
         } else if (errDec.kind === "preserve_error_or_done") {
           // EventSource often fires onerror when the stream closes after a
           // normal assistant_done -- do not paint a false error over success.
           setTurnOpen(false);
           const liveJobs = pendingJobIdsRef.current.some(
             (id) => id && !id.startsWith("local-swarm-"),
           );
          if (liveJobs && !userStoppedRef.current) {
            setStatus((prev) => alreadySettledOnDoneStatus({
              prev,
              liveJobs: true,
              userStopped: false,
            }));
            setWaitHint((prev) => prev || SWARM_AWAIT_HINT);
          } else {
            setWaitHint(null);
            setStatus((prev) => alreadySettledOnDoneStatus({
              prev,
              liveJobs: false,
              userStopped: userStoppedRef.current,
            }));
          }
         }
         cancelRef.current = null;
         localStreamActiveRef.current = false;
         setCompactingStatus(null);
         maybeRunApprovedCommandRetryRef.current();
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
    const kickSid = activeSessionIdRef.current;
    setSafeTimeout(() => {
      if (activeSessionIdRef.current !== kickSid) return;
      if (cancelRef.current || resumeQueuedRef.current) return;
      setQueueItems((prev) => prev.filter((it) => it.id !== next.id));
      queueItemsRef.current = queueItemsRef.current.filter((it) => it.id !== next.id);
      const sid = activeSessionIdRef.current;
      api.queueRemove(next.id).catch(() => {}).finally(() => {
        if (activeSessionIdRef.current !== sid) return;
        refreshQueue(sid);
      });
      const nextImgs = (next.images || []).map((p: string) => ({
        path: p,
        name: (p.split(/[\\/]/).pop() || p),
        previewUrl: p,
      }));
      // Per-item model stamp (Hermes-style): apply before kicking the turn so a
      // playlist queued under deepseek does not run under a later kimi pick.
      const kick = async () => {
        if (activeSessionIdRef.current !== kickSid) return;
        const stamped = next.model;
        if (stamped) {
          try {
            await api.swapPilot(stamped);
            window.dispatchEvent(new Event("harness-config-changed"));
          } catch {
            /* best-effort; stream start also reconciles _cfg vs live pilot */
          }
        }
        if (activeSessionIdRef.current !== kickSid) return;
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
    const kickSid = activeSessionIdRef.current;
    setSafeTimeout(() => {
      if (activeSessionIdRef.current !== kickSid) return;
      if (userStoppedRef.current || cancelRef.current) {
        if (!userStoppedRef.current) resumeQueuedRef.current = true;
        return;
      }
      executeSendRef.current("", false, false, true);
    }, 60);
  };
  maybeRunQueuedResumeRef.current = maybeRunQueuedResume;

  const maybeRunApprovedCommandRetry = () => {
    const command = approvedCommandRetryRef.current;
    if (!command || cancelRef.current || userStoppedRef.current) return;
    const kickSid = activeSessionIdRef.current;
    approvedCommandRetryRef.current = null;
    setSafeTimeout(() => {
      if (activeSessionIdRef.current !== kickSid) return;
      if (cancelRef.current || userStoppedRef.current) return;
      executeSendRef.current(
        "The operator approved one execution of this exact command. Retry it "
          + "without changing any character, then continue the objective:\n\n"
          + command,
        true,
        false,
      );
    }, 60);
  };
  maybeRunApprovedCommandRetryRef.current = maybeRunApprovedCommandRetry;

  // A pilot_resume can also arrive via the swarm-results poll while the session is
  // idle (the common background-job case). Trigger a continuation immediately.
  const triggerResume = () => {
    const gate = triggerResumeGate({
      userStopped: userStoppedRef.current,
      cancelArmed: !!cancelRef.current,
    });
    if (gate === "suppress_clear_hint") {
      resumeQueuedRef.current = false;
      // Stop suppressed keep-alive — drop Looking… / Still working… if painted.
      setWaitHint((prev) => clearSwarmAwaitWaitHint(prev));
      return;
    }
    if (gate === "queue_clear_hint") {
      // Stream already armed: queue keep-alive only. Clear Looking… /
      // Still working… so poll-path pilot_resume cannot leave a stuck hint
      // (match Stop-branch honesty; SSE mid-turn queue never paints Looking…).
      resumeQueuedRef.current = true;
      setWaitHint((prev) => clearSwarmAwaitWaitHint(prev));
      return;
    }
    executeSendRef.current("", false, false, true);
  };
  resumeTriggerRef.current = triggerResume;

  const send = (mode?: "interrupt") => {
    if (editBusy) return;
    const raw = input.trim();
    const sid = activeSessionIdRef.current || "_draft";
    const msg = applyTerminalSelectionsToMessage(raw, peekTerminalSelections(sid));
    // Allow a send/steer that is only attached image(s) with no text -- the
    // backend accepts text OR images.
    if (shouldBlockEmptySend({
      transcriptStale,
      text: msg,
      imageCount: attachedImages.length,
    })) return;

    // Intercept slash commands locally
    const slash = classifyLocalSlashCommand({
      message: msg,
      isBuiltIn: isBuiltInSlashCommand,
      customNames: customCommands.map((c) => c.name),
    });
    const chrome = localSlashChromeAction(slash);
    if (chrome === "clear_visible") {
      // /clear: reset the visible transcript for the current session — do not
      // abandon to createSession() (that is /new).
      setInput("");
      setEditingIndex(null);
      setItems([]);
      itemsRef.current = [];
      transcriptFpRef.current = "";
      if (activeSessionId) writeTranscriptCache(activeSessionId, []);
      setTurnOpen(false);
      setWaitHint(null);
      setStatus("idle");
      setCompactingStatus(null);
      return;
    }
    if (chrome === "new_session") {
      setInput("");
      setEditingIndex(null);
      window.dispatchEvent(new Event("harness-new-session"));
      return;
    }
    if (slash.kind === "refine") {
      const refineText = slash.text || "";
      setInput("");
      setEditingIndex(null);
      if (!refineText) {
        setItems((p) => [
          ...p,
          { kind: "msg", msg: { role: "user", text: msg } },
          { kind: "msg", msg: { role: "assistant", text: "Usage: /refine <memory|rule|skill|role text>. Accept/dismiss/rollback stay on the existing refine cards." } },
        ]);
        return;
      }
      const command = msg.startsWith("/") ? msg : `/refine ${refineText}`;
      void api.refinePropose({ command, text: refineText })
        .then((res) => {
          const prop = res?.proposed;
          if (res?.ok && prop?.id && (prop.text || "").trim()) {
            setMemoryProposals((prev) => (
              prev.some((p) => p.id === prop.id)
                ? prev
                : [...prev, {
                  id: prop.id,
                  text: String(prop.text || "").trim(),
                  category: prop.category || "general",
                  refine: {
                    kind: String(prop.kind || "memory"),
                    scope: String(prop.scope || "global"),
                  },
                }]
            ));
          }
        })
        .catch(() => undefined);
      setItems((p) => [
        ...p,
        { kind: "msg", msg: { role: "user", text: msg } },
        { kind: "msg", msg: { role: "assistant", text: "Queued refine proposal on the existing controller." } },
      ]);
      return;
    }
    if (slash.kind === "compact") {
      // Local slash path: echo the command into the transcript so Send feels
      // like a normal prompt (compaction itself still bypasses the pilot loop).
      const compactSid = activeSessionIdRef.current;
      const thinkingId = newThinkingId();
      setInput("");
      setEditingIndex(null);
      setStatus("thinking");
      setItems((p) => [
        ...p,
        { kind: "msg", msg: { role: "user", text: msg } },
        {
          kind: "thinking",
          text: "Compacting session context on backend...",
          id: thinkingId,
        },
      ]);
      api.compactSession()
        .then((res) => {
          if (!shouldApplyCompactSettle({
            requestSessionId: compactSid,
            activeSessionId: activeSessionIdRef.current,
          })) return;
          setStatus("done");
          setItems((p) => [
            ...p.filter((it) => !(it.kind === "thinking" && it.id === thinkingId)),
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
          if (!shouldApplyCompactSettle({
            requestSessionId: compactSid,
            activeSessionId: activeSessionIdRef.current,
          })) return;
          setStatus("error");
          setItems((p) => [
            ...p.filter((it) => !(it.kind === "thinking" && it.id === thinkingId)),
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
    if (slash.kind === "model") {
      setInput("");
      setEditingIndex(null);
      window.dispatchEvent(new Event("harness-open-model-picker"));
      return;
    }
    if (slash.kind === "help") {
      setInput("");
      setEditingIndex(null);
      // Protocol stays in the palette. Do not dump /help as a fake assistant memo.
      window.dispatchEvent(new Event("harness-open-command-palette"));
      return;
    }
    const paletteId = localSlashPaletteAction(slash);
    if (paletteId) {
      // Same event path as Cmd-K (open-swarm / open-memory / open-mcp / …).
      setInput("");
      setEditingIndex(null);
      runCommandPaletteAction(paletteId, {
        toggleLeft: () => {},
        toggleRight: () => {},
        focusSettingsPage: (page) => focusSettingsPage(page as "advanced"),
      });
      return;
    }
    if (slash.kind === "custom") {
      setStatus("thinking");
      api.renderCommand(slash.name, slash.args)
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

    // After a rewind-edit, clear ALL edit chrome and start a fresh turn.
    // Lingering Revert? after send looked like a stuck dead turn.
    const resubmitEdit = editingIndex !== null || canRevertEdit;
    setEditingIndex(null);
    setCanRevertEdit(false);
    setEditNotice(editNoticeAfterSend(false));

    if (composerBusy && !resubmitEdit) {
      // Images-only is a new-turn send. Mid-turn steer/interrupt needs words
      // or Stop invents a phantom steer and then drops it as an error.
      if (!shouldSteerWhileBusy({ text: msg })) return;
      // Snapshot the attached image paths BEFORE the async call so we never
      // read a stale/cleared closure value and images are never silently
      // dropped from the steer/interrupt request. Clear the draft only on
      // success (Cursor parity — keep operator text when 4xx/network fails).
      const steerImages = attachedImages.map((img) => img.path).filter(Boolean);
      const deliveryMode = mode === "interrupt" ? "interrupt" as const : undefined;
      api.steerSession(msg, steerImages, deliveryMode)
        .then((res) => {
          if (shouldClearSteerDraftOnResult(true)) {
            setInput("");
            dropTerminalLabels(sid, terminalLabelsFromDraft(raw));
            setAttachedImages([]);
          }
          const chrome = steerResultChrome({
            action: res?.action,
            composerMode: mode,
          });
          if (chrome === "queue") {
            refreshQueue();
            return;
          }
          const row = steerTranscriptItem({ text: msg, chrome });
          if (!row) return;
          setItems((prev) => [...prev, row]);
        })
        .catch((err) => {
          console.error(
            mode === "interrupt"
              ? "Failed to interrupt session:"
              : "Failed to steer session:",
            err,
          );
          setItems((prev) => [
            ...prev,
            {
              kind: "msg",
              msg: {
                role: "assistant",
                text:
                  mode === "interrupt"
                    ? formatInterruptErrorMessage(err)
                    : formatSteerErrorMessage(err),
              }
            }
          ]);
        });
      return;
    }

    const kickSend = () => {
      setInput("");
      dropTerminalLabels(sid, terminalLabelsFromDraft(raw));
      executeSend(msg, auto, plan);
    };

    // Edit-resubmit must start a new loop, not silently steer into a dead turn.
    if (composerBusy && resubmitEdit) {
      stop();
      kickSend();
      return;
    }

    kickSend();
  };

  const stopLocal = () => {
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
    const settle = settleFromUserStop();
    setTurnOpen(false);
    setWaitHint(null);
    setStatus("idle");
    setCompactingStatus(null);
    recordTurnSettle(settle);
    const liveIds = liveNonLocalSwarmJobIds();
    setPendingJobIds(liveIds);
    // Same seal → orphan settle order as assistant_done: close any open
    // pilot/reasoning surface before folding orphan swarm/investigation cards.
    setItems((p) => applyLocalTurnSettle(settle, p, liveIds));
  };

  const stop = () => {
    const sid = activeSessionId;
    void runStopFlow({
      stopLocal,
      interruptSession: () => api.interruptSession(),
      refreshTranscript: sid
        ? async () => {
            const tres = await api.sessionTranscript(sid);
            const loadedItems = transcriptResponseToItems(tres);
            const settle = lastSettleRef.current;
            const liveIds = liveNonLocalSwarmJobIds();
            setItems((prev) => {
              const merged = mergeTranscriptItems(prev, loadedItems);
              const next = settle
                ? applyLocalTurnSettle(settle, merged, liveIds)
                : merged;
              const fp = transcriptFingerprint(next);
              if (fp === transcriptFpRef.current) return prev;
              transcriptFpRef.current = fp;
              itemsRef.current = next;
              writeTranscriptCache(sid, next);
              return next;
            });
          }
        : undefined,
    }).then((result) => {
      if (result.kind === "interrupt_failed") {
        setEditNotice(result.notice);
        return;
      }
      // Belt-and-suspenders: interrupt body notices if refresh missed them.
      for (const notice of result.notices) {
        if (!noticeIsStopHonesty(notice.reason)) continue;
        setItems((prev) => appendStopHonestyNotice(prev, notice.message));
      }
    });
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
  const handleCommandApproval = useCallback(
    (item: CommandApprovalItem, decision: boolean | "amendment") => {
      const approveOriginal = decision === true;
      const approveAmendment = decision === "amendment";
      const approve = approveOriginal || approveAmendment;
      setItems((current) => updateCommandApproval(
        current,
        item.commandHash,
        { status: "approving", error: undefined },
      ));
      const request = approveAmendment
        ? api.approveCommandAmendment(item.sessionId, item.workspaceRoot, item.commandHash)
        : approveOriginal
          ? api.approveCommand(item.sessionId, item.workspaceRoot, item.commandHash)
          : api.rejectCommand(item.sessionId, item.workspaceRoot, item.commandHash);
      void request.then((response) => {
        setItems((current) => updateCommandApproval(
          current,
          item.commandHash,
          { status: approve ? "approved" : "rejected", error: undefined },
        ));
        if (approve && "retry_command" in response && response.retry_command) {
          approvedCommandRetryRef.current = response.retry_command;
          maybeRunApprovedCommandRetryRef.current();
        }
      }).catch((error) => {
        setItems((current) => updateCommandApproval(
          current,
          item.commandHash,
          {
            status: "error",
            error: error instanceof Error ? error.message : String(error),
          },
        ));
      });
    },
    [],
  );
  const handleSecretRequest = useCallback(
    (item: SecretRequestItem, decision: { action: "save"; value: string } | { action: "dismiss" }) => {
      setItems((current) => updateSecretRequest(
        current,
        item.connector,
        item.field,
        { status: decision.action === "save" ? "saving" : "declined", error: undefined },
      ));
      const sessionId = item.sessionId || activeSessionId || "";
      const request = decision.action === "save"
        ? api.submitSecret({
            session_id: sessionId,
            connector: item.connector,
            field: item.field,
            value: decision.value,
          })
        : api.dismissSecret({
            session_id: sessionId,
            connector: item.connector,
            field: item.field,
          });
      void request.then((response) => {
        setItems((current) => updateSecretRequest(
          current,
          item.connector,
          item.field,
          { status: decision.action === "save" ? "saved" : "declined" },
        ));
        if (decision.action === "save" && response && (response as { resume?: boolean }).resume) {
          executeSendRef.current("", false, false, true);
        }
      }).catch((error) => {
        setItems((current) => updateSecretRequest(
          current,
          item.connector,
          item.field,
          {
            status: "error",
            error: error instanceof Error ? error.message : String(error),
          },
        ));
      });
    },
    [activeSessionId],
  );
  const handleTranscriptImageClick = useCallback((url: string) => setLightboxUrl(url), []);
  const recoveryBound = recoveryContextRef.current;
  const handleRecoveryContinue = () => {
    if (!recoveryBound) return;
    if (!recoveryDispatchAllowed({
      composerBusy,
      dispatching: recoveryDispatchingRef.current,
      lifecycle: turnLifecycle,
      boundSessionId: recoveryBound.sessionId,
      activeSessionId,
      boundGeneration: recoveryBound.generation,
      activeGeneration: streamGenRef.current,
    })) return;
    recoveryDispatchingRef.current = true;
    executeSendRef.current(CONTINUE_PROMPT, auto, plan);
  };
  const handleRecoveryRetry = () => {
    if (!recoveryBound) return;
    if (!recoveryDispatchAllowed({
      composerBusy,
      dispatching: recoveryDispatchingRef.current,
      lifecycle: turnLifecycle,
      boundSessionId: recoveryBound.sessionId,
      activeSessionId,
      boundGeneration: recoveryBound.generation,
      activeGeneration: streamGenRef.current,
    })) return;
    const ask = latestUserAsk(itemsRef.current);
    if (!ask) return;
    recoveryDispatchingRef.current = true;
    executeSendRef.current(ask, auto, plan);
  };

  const handleTranscriptExecutePlan = useCallback((planText: string) => {
    setAuto(true);
    setPlan(false);
    executeSendRef.current(
      "Execute the following approved plan. Implement it fully, using run_implement/run_parallel as needed:\n\n" + planText,
      true,
      false
    );
  }, []);

  const handleOperationalRecovery = useCallback(async () => {
    if (!operationalDiagnostic) return;
    await executeDiagnosticRecovery(operationalDiagnostic, async () => {
      if (operationalDiagnostic.code === AUTH_FAILURE) {
        focusSettingsPage("providers");
        window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "settings" }));
        const ask = latestUserAsk(itemsRef.current);
        if (ask) {
          recoveryDispatchingRef.current = true;
          executeSendRef.current(ask, auto, plan);
        }
        return;
      }
      try {
        const res = await api.diagnostics();
        const diag = fromBackendDiagnostic(res.diagnostic as Record<string, unknown> | null);
        if (!diag) clearDiagnosticAfterSuccess(operationalDiagnostic);
        else publishDiagnostic(diag);
        window.dispatchEvent(new Event("harness-config-changed"));
      } catch {
        /* keep diagnostic visible */
      }
    });
  }, [operationalDiagnostic, auto, plan]);

  const handleAuthFailureRetry = useCallback(() => {
    const ask = latestUserAsk(itemsRef.current);
    if (!ask) return;
    recoveryDispatchingRef.current = true;
    executeSendRef.current(ask, auto, plan);
  }, [auto, plan]);

  const recoveryAction = (
    operationalDiagnostic
    && operationalDiagnostic.recovery.kind !== "none"
      ? {
          label: operationalDiagnostic.recovery.label,
          onClick: () => { void handleOperationalRecovery(); },
        }
      : undefined
  );

  return (
    <main className="flex flex-col h-full min-w-0 bg-transparent" data-active-editor-tab={activeTab}>
      {/* Brand + idle share equal inset so they line up with the floating dock. */}
      <ConversationHeader
        pillStatus={pillStatus}
        correlationId={
          pillStatus === "error" && operationalDiagnostic?.correlationId
            ? operationalDiagnostic.correlationId
            : undefined
        }
        detail={
          pillStatus === "error" && operationalDiagnostic
            ? operationalDiagnostic.summary
            : (
              !transcriptStale && pillStatus !== "idle" && composerBusy
                ? (
                  busyProgress.label
                    ? busyProgress.pill
                    : derivePillBusyDetail({
                      liveInvestigation,
                      pillStatus,
                      agentLoopOpen,
                    })
                )
                : undefined
            )
        }
        recoveryAction={recoveryAction}
        onBusyDetailClick={() => {
          // Worker / shell busy chrome → terminal, never the file editor.
          try {
            window.dispatchEvent(
              new CustomEvent("harness-focus-tab", { detail: "terminal" }),
            );
          } catch {
            /* ignore */
          }
        }}
      />

      <EditorTabStrip
        openTabs={openTabs}
        activeTab={activeTab}
        tabContextMenu={tabContextMenu}
        repoRoot={repoRoot}
        onSelectTab={setActiveTab}
        onCloseTab={handleCloseTab}
        onCloseOtherTabs={handleCloseOtherTabs}
        onCloseAllTabs={handleCloseAllTabs}
        onOpenContextMenu={setTabContextMenu}
        onCloseContextMenu={() => setTabContextMenu(null)}
      />

      <div className="relative flex flex-col flex-1 min-h-0 min-w-0">
      <div
        className={chatColumnMountClass(activeTab)}
        aria-hidden={!isChatColumnActive(activeTab)}
      >
        <ConversationChatColumn
          feedRef={feedRef}
          feedContentRef={feedContentRef}
          transcriptStale={transcriptStale}
          items={items}
          status={status}
          compactingStatus={compactingStatus}
          editingIndex={editingIndex}
          auto={auto}
          plan={plan}
          busyElapsedMs={busyElapsedMs}
          turnOpen={turnOpen}
          holdSwarmAwait={holdSwarmAwait}
          feedSettled={feedSettled}
          scrollToEndRef={scrollFeedToEndRef}
          onEditMessage={stableEditMessage}
          onExecuteSend={stableExecuteSend}
          onImageClick={handleTranscriptImageClick}
          onSetCard={stableSetCard}
          onExecutePlan={handleTranscriptExecutePlan}
          onCommandApproval={handleCommandApproval}
          onSecretRequest={handleSecretRequest}
          onAuthFailureRetry={handleAuthFailureRetry}
          showJumpToBottom={showJumpToBottom}
          onJumpToBottom={jumpToLatest}
          composerDock={(
      <ComposerDock
        config={config}
        taRef={taRef}
        input={input}
        auto={auto}
        plan={plan}
        composerBusy={composerBusy}
        transcriptStale={transcriptStale}
        wikiPrepared={wikiPrepared}
        memoryProposals={memoryProposals}
        distillNotice={distillNotice}
        msgQueue={msgQueue}
        dragIndex={dragIndex}
        dragOverIndex={dragOverIndex}
        queueItems={queueItems}
        swarmLiveJobs={swarmLiveJobs}
        sessionId={activeSessionId || cachedSessionIdRef.current || ""}
        queueLoadError={queueLoadError}
        queueDragIndex={queueDragIndex}
        queueDragOverIndex={queueDragOverIndex}
        editingIndex={editingIndex}
        canRevertEdit={canRevertEdit}
        editNotice={editNotice}
        editBusy={editBusy}
        showContextPanel={showContextPanel}
        contextUsage={contextUsage}
        mentionSearch={mentionSearch}
        filteredFiles={filteredFiles}
        filteredFolders={filteredFolders}
        symbolResults={symbolResults}
        mentionListingCap={mentionListingCap}
        selectedFileIndex={selectedFileIndex}
        codegraphStatus={codegraphStatus}
        slashSearch={slashSearch}
        selectedSlashIndex={selectedSlashIndex}
        allSlashCommands={allSlashCommands}
        attachedImages={attachedImages}
        isDragOver={isDragOver}
        uploadError={uploadError}
        onSetWikiPrepared={setWikiPrepared}
        onSetMemoryProposals={setMemoryProposals}
        onSetDistillNotice={setDistillNotice}
        onSetMsgQueue={setMsgQueue}
        onSetInput={setInput}
        onSetAuto={setAuto}
        onSetPlan={setPlan}
        onSetCanRevertEdit={setCanRevertEdit}
        onSetEditNotice={setEditNotice}
        onSetShowContextPanel={setShowContextPanel}
        onSetSelectedFileIndex={setSelectedFileIndex}
        onSetSelectedSlashIndex={setSelectedSlashIndex}
        onSetAttachedImages={setAttachedImages}
        onSetUploadError={setUploadError}
        onSetLightboxUrl={setLightboxUrl}
        setSafeTimeout={setSafeTimeout}
        fetchContextUsage={fetchContextUsage}
        handleDragStart={handleDragStart}
        handleDragOver={handleDragOver}
        handleDragLeave={handleDragLeave}
        handleDrop={handleDrop}
        handleDragEnd={handleDragEnd}
        moveQueueItem={moveQueueItem}
        handleQueueClearAll={handleQueueClearAll}
        handleQueueDragStart={handleQueueDragStart}
        handleQueueDragOver={handleQueueDragOver}
        handleQueueDragLeave={handleQueueDragLeave}
        handleQueueDrop={handleQueueDrop}
        handleQueueDragEnd={handleQueueDragEnd}
        handleQueueEdit={handleQueueEdit}
        handleQueueRemove={handleQueueRemove}
        handleComposerDragOver={handleComposerDragOver}
        handleComposerDragLeave={handleComposerDragLeave}
        handleComposerDrop={handleComposerDrop}
        handleRevertEdit={handleRevertEdit}
        handleCancelEdit={handleCancelEdit}
        handleInputChange={handleInputChange}
        handleKeyDown={handleKeyDown}
        handlePaste={handlePaste}
        insertMention={insertMention}
        insertFolder={insertFolder}
        insertSymbol={insertSymbol}
        insertCodebase={insertCodebase}
        showCodebaseMention={showCodebaseMention}
        insertSlashCommand={insertSlashCommand}
        handleQueueAdd={handleQueueAdd}
        stop={stop}
        send={send}
        recoveryAvailable={Boolean(recoveryBound) && recoveryDispatchAllowed({
          composerBusy,
          dispatching: recoveryDispatchingRef.current,
          lifecycle: turnLifecycle,
          boundSessionId: recoveryBound?.sessionId,
          activeSessionId,
          boundGeneration: recoveryBound?.generation,
          activeGeneration: streamGenRef.current,
        })}
        recoveryRetryAvailable={Boolean(latestUserAsk(items))}
        recoveryCause={terminalCause}
        onContinue={handleRecoveryContinue}
        onRetry={handleRecoveryRetry}
      />
      )}
        />
      </div>
      {!isChatColumnActive(activeTab) ? (
    <div data-close-surface="editor" className="flex-1 min-h-0 min-w-0 flex flex-col">
    <FileEditorPane
      path={activeTab}
      line={openTabs.find((t) => t.path === activeTab)?.line}
      col={openTabs.find((t) => t.path === activeTab)?.col}
      onClose={() => handleCloseTab(activeTab)}
      onDirtyChange={(dirty) => handleTabDirtyChange(activeTab, dirty)}
    />
    </div>
      ) : null}
      </div>

      <ImageLightbox url={lightboxUrl} onClose={() => setLightboxUrl(null)} />

      <SpillPreviewModal
        preview={spillPreview}
        onClose={() => setSpillPreview(null)}
      />

    </main>
  );
}
