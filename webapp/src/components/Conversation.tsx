import { useEffect, useLayoutEffect, useRef, useState, useCallback } from "react";
import { api, type Config } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import FileEditorPane from "./FileEditorPane";
import { type Item, type Card } from "./TranscriptList";
import {
  deriveBusyProgress,
  turnHasInvestigationActivity,
  turnHasLiveInvestigation,
  turnLooksAnswerComplete,
} from "../lib/turnProgress";
import { renameDefaultSessionIfNeeded } from "../lib/sessionTitle";

import { writeTranscriptCache } from "./conversation/transcriptCache";
import { transcriptResponseToItems } from "./conversation/transcriptItems";
import { newThinkingId } from "./conversation/thinkingToolPrep";
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
import { derivePillStatus } from "./conversation/pillStatus";
import {
  applySwarmResultToItems,
  finalizeOrphanSwarmPills,
  patchCardInItems,
} from "./conversation/streamApply";
import {
  classifyLocalSlashCommand,
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
  import {
  FEED_PIN_THRESHOLD_PX,
  pinStateFromScrollGeometry,
  settleFrameResult,
  shouldUnpinOnTouchMove,
  shouldUnpinOnWheel,
} from "./conversation/feedScroll";
import {
  STREAM_ABORT_MESSAGE,
  streamOnDoneDecision,
  streamOnErrorDecision,
} from "./conversation/streamTerminal";
import ConversationChatColumn from "./conversation/ConversationChatColumn";
import {
  appendMentionsToInput,
  buildMentionInsert,
  buildSymbolInsert,
  clampSelectIndex,
  cycleSelectIndex,
  detectComposerTrigger,
  filterSlashCommands,
  mentionTokenForDroppedPath,
} from "./conversation/composerInput";
import { moveItem, reorderByDrag } from "./conversation/queueOps";
import {
  notifyPrefEnabled,
  queueMessagesPrefEnabled,
  shouldShowCompletionNotification,
  soundPrefEnabled,
} from "./conversation/completionNotify";
import { createApplyStreamEvent } from "./conversation/streamEventHandler";
import EditorTabStrip from "./conversation/EditorTabStrip";
import ComposerDock from "./conversation/ComposerDock";
import ConversationHeader from "./conversation/ConversationHeader";
import ImageLightbox from "./conversation/ImageLightbox";
import { useSessionSwitch } from "./conversation/useSessionSwitch";
import { useRunnersBusyPoll } from "./conversation/useRunnersBusyPoll";
import {
  appendMemoryProposal,
  classifySwarmPollEvent,
} from "./conversation/swarmPoll";
import {
  flushTypewriterBuffer,
  startTypewriterLoop,
} from "./conversation/streamTypewriter";
import {
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
    const handleOpenFile = (e: CustomEvent<{ path: string; line?: number; col?: number }>) => {
      const filePath = e.detail.path;
      if (!filePath) return;
      const line = e.detail.line;
      const col = e.detail.col;
      setOpenTabs((prev) => upsertOpenTab(prev, filePath, line, col));
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
  const pendingJobIdsRef = useRef<string[]>([]);
  useEffect(() => { pendingJobIdsRef.current = pendingJobIds; }, [pendingJobIds]);
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
    setQueueItems((prev) => {
      const next = reorderByDrag(prev, fromIdx, targetIdx);
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
  // Hermes session-switch settle: while true, the [items] effect keeps scrolling
  // to bottom until height stabilizes (or wall-clock timeout). onScroll still
  // tracks real geometry so keyboard/scrollbar unpin is not swallowed.
  const scrollSettlingRef = useRef(false);
  useEffect(() => {
    const el = feedRef.current;
    if (!el) return;
    const onScroll = () => {
      pinnedToBottomRef.current = pinStateFromScrollGeometry(
        el.scrollHeight,
        el.scrollTop,
        el.clientHeight,
        scrollSettlingRef.current,
        FEED_PIN_THRESHOLD_PX,
      );
    };
    // Fast-path unpin on upward wheel/touch before the next thinking token
    // re-runs stick-to-bottom -- otherwise long reasoning streams keep yanking
    // the feed back to the end and the user cannot scroll the Thought block.
    const onWheel = (e: WheelEvent) => {
      if (shouldUnpinOnWheel(e.deltaY, scrollSettlingRef.current)) {
        pinnedToBottomRef.current = false;
      }
    };
    let touchY: number | null = null;
    const onTouchStart = (e: TouchEvent) => {
      touchY = e.touches[0]?.clientY ?? null;
    };
    const onTouchMove = (e: TouchEvent) => {
      const y = e.touches[0]?.clientY;
      if (shouldUnpinOnTouchMove(touchY, y ?? null, scrollSettlingRef.current)) {
        pinnedToBottomRef.current = false;
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
  // stable for ~5 frames (or ~1s wall-clock), then re-lock stick-to-bottom.
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
    const startedAtMs = performance.now();
    const settle = () => {
      const node = feedRef.current;
      if (!node) {
        scrollSettlingRef.current = false;
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
      node.scrollTop = height;
      pinnedToBottomRef.current = true;
      if (step.done) {
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
    applyStreamEventRef,
    flushTypewriterRef,
    maybeRunQueuedResumeRef,
    maybeDrainQueueRef,
    ensureChatEventsReattachRef,
    cancelRef,
    localStreamActiveRef,
    detachedBusyRef,
    userStoppedRef,
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
    ensureChatEventsReattachRef,
    setItems,
    setTranscriptStale,
    setTurnOpen,
    setStatus,
    setCompactingStatus,
  });


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
      setSelectedFileIndex(clampSelectIndex(selectedFileIndex, total));
    }
  }, [filteredFiles, symbolResults, selectedFileIndex]);

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
      const insideToken = mentionTokenForDroppedPath({ osPath, repo });
      if (insideToken) {
        mentions.push(insideToken);
        continue;
      }
      // Outside repo, or inside-repo path with spaces: upload then @-mention.
      try {
        const uploaded = await api.uploadImage(file); // generic file upload endpoint
        const token = mentionTokenForDroppedPath({
          osPath: "",
          repo,
          uploadedPath: uploaded.path,
        });
        if (token) mentions.push(token);
        else flashUploadError("Dropped file path has spaces -- rename and retry");
      } catch (err) {
        console.error("Failed to upload dropped file:", err);
        flashUploadError("File upload failed");
      }
    }

    if (mentions.length > 0) {
      setInput((prev) => appendMentionsToInput(prev, mentions));
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
              const action = classifySwarmPollEvent(evt);
              if (action.kind === "swarm_result") {
                handleSwarmResult(action.data);
              } else if (action.kind === "pilot_resume") {
                // Background job finished while the session was idle. The backend
                // already extended history with the result + continuation; kick
                // off a keep-alive turn so the pilot continues without a prompt.
                if (!pollResumeFired) {
                  pollResumeFired = true;
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
                  }),
                );
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
  const startTypewriter = () => {
    startTypewriterLoop(
      { typeBufRef, typeRafRef, typeDoneRef },
      appendStreamingText,
      requestAnimationFrame,
    );
  };

  const flushTypewriter = () => {
    flushTypewriterBuffer(
      { typeBufRef, typeRafRef, typeDoneRef },
      appendStreamingText,
      cancelAnimationFrame,
    );
  };
  flushTypewriterRef.current = flushTypewriter;


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
         const doneDec = streamOnDoneDecision({
           turnSettled: turnSettledRef.current,
           userStopped: userStoppedRef.current,
         });
         if (doneDec.kind === "abort_error") {
           turnSettledRef.current = true;
           setTurnOpen(false);
           setStatus("error");
           setItems((p) => [...p, {
             kind: "msg",
             msg: {
               role: "assistant",
               text: STREAM_ABORT_MESSAGE,
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
         const errDec = streamOnErrorDecision({
           turnSettled: turnSettledRef.current,
           userStopped: userStoppedRef.current,
         });
         if (errDec.kind === "abort_error") {
           turnSettledRef.current = true;
           setTurnOpen(false);
           setItems((p) => [...p, {
             kind: "msg",
             msg: {
               role: "assistant",
               text: STREAM_ABORT_MESSAGE,
             },
           }]);
           setStatus("error");
         } else if (errDec.kind === "preserve_error_or_done") {
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
    const slash = classifyLocalSlashCommand({
      message: msg,
      isBuiltIn: isBuiltInSlashCommand,
      customNames: customCommands.map((c) => c.name),
    });
    if (slash.kind === "clear_or_new") {
      setInput("");
      setEditingIndex(null);
      window.dispatchEvent(new Event("harness-new-session"));
      return;
    }
    if (slash.kind === "compact") {
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
    if (slash.kind === "model") {
      setInput("");
      setEditingIndex(null);
      window.dispatchEvent(new Event("harness-open-model-picker"));
      return;
    }
    if (slash.kind === "help") {
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
    const liveIds = pendingJobIdsRef.current.filter(
      (id) => !id.startsWith("local-swarm-"),
    );
    setPendingJobIds(liveIds);
    setItems((p) => finalizeOrphanSwarmPills(p, liveIds));
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
      <ConversationHeader
        pillStatus={pillStatus}
        detail={
          !transcriptStale && !answerChromeIdle && pillStatus !== "idle" && busyProgress.label
            ? busyProgress.pill
            : undefined
        }
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

      {activeTab === "chat" ? (
        <ConversationChatColumn
          feedRef={feedRef}
          transcriptStale={transcriptStale}
          items={items}
          status={status}
          compactingStatus={compactingStatus}
          editingIndex={editingIndex}
          auto={auto}
          plan={plan}
          busyElapsedMs={busyElapsedMs}
          turnOpen={turnOpen}
          onEditMessage={stableEditMessage}
          onExecuteSend={stableExecuteSend}
          onImageClick={handleTranscriptImageClick}
          onSetCard={stableSetCard}
          onExecutePlan={handleTranscriptExecutePlan}
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
        insertSymbol={insertSymbol}
        insertSlashCommand={insertSlashCommand}
        handleQueueAdd={handleQueueAdd}
        stop={stop}
        send={send}
      />
      )}
        />
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
        <ImageLightbox url={lightboxUrl} onClose={() => setLightboxUrl(null)} />
      )}

    </main>
  );
}

