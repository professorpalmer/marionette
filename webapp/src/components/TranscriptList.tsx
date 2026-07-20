import { useLayoutEffect, useRef, useState, useCallback, useEffect, memo } from "react";
import { ChevronRight, Loader2, ChevronDown, ChevronUp, Play, Copy, Check, Pencil, RefreshCw, History, Share2, CheckCircle2, XCircle } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import "highlight.js/styles/github-dark.css";
import {
  openAgentLink,
  openAgentFile,
  openAgentUrl,
  openAgentCommand,
  isExternalUrl,
  looksLikePathInlineCode,
  classifyActionGoal,
  autolinkAgentText,
} from "../lib/agentLinks";
import {
  aggregateExplorationSummary,
  cardEffectivelyRunning,
  deriveBusyProgress,
  investigatingHeadline,
  resolveCardCliInput,
  shortenGoal,
  quietWorkingCueVisible,
  shouldShowBusyFooter,
  toolFocusPhrase,
  toolInputFieldKey,
  toolRowLabel,
  turnHasLiveInvestigation,
} from "../lib/turnProgress";
import {
  autoHaltPresentation,
  autoStatusPresentation,
  commandApprovalStatusCopy,
  commandBlockedPresentation,
  type AutoBudgetSnapshot,
} from "../lib/autoReceipts";
import { TranscriptImage } from "./conversation/TranscriptImage";

export type Msg = {
  role: "user" | "assistant";
  text: string;
  isPlan?: boolean;
  images?: { path: string; name: string; previewUrl: string }[];
  streaming?: boolean;
  // Ephemeral live preview of a swarm worker's token stream. Rendered in a
  // height-capped, auto-scrolling window and DROPPED when the action finalizes
  // (the worker's real output is carried by the swarm artifacts/summary), so a
  // multi-worker swarm can't concatenate into one unbounded permanent bubble.
  workerStream?: boolean;
};
/** Bounded nested worker tool row (from local job actions[] / display hydrate). */
export type NestedAction = {
  action_id: string;
  kind: string;
  goal?: string;
  status: "running" | "complete" | "failed";
  duration_ms?: number | null;
  error?: string;
  worker_id?: string;
};

export type Card = {
  id: string; goal: string; cwd?: string | null;
  running: boolean; open: boolean;
  kind?: string;
  /** Stable provider tool call id (tool_prep promotion / reload). */
  call_id?: string;
  /** run_parallel parent goals (hydrated from display / action_start). */
  goals?: string[];
  /** Nested sanitized worker actions (never stdout/args). */
  actions?: NestedAction[];
  /** Owning local job id when actions were mirrored from a worker. */
  worker_id?: string;
  // Fields are optional because a card's result can be a full tool outcome
  // (num/types/artifacts) OR a lightweight dispatch ack (status/message) for a
  // backgrounded run_implement/run_parallel job. Rendering must not assume the
  // rich shape -- expanding a dispatch-only card used to crash on types.join.
  result?: { job_id?: string; num?: number; types?: string[]; adapter?: string;
             artifacts?: { type: string; headline: string }[]; error?: string;
             status?: string; message?: string; duration_ms?: number };
};
/** Inline swarm status pill lifecycle (running spinner vs terminal chips). */
export type SwarmPendingStatus = "running" | "done" | "failed" | "ended";

export type SwarmPendingItem = {
  kind: "swarm_pending";
  job_ids: string[];
  objective: string;
  /** @deprecated prefer status; true means terminal "done". */
  resolved?: boolean;
  status?: SwarmPendingStatus;
  /** Job ids that already received a swarm_result (for run_parallel). */
  terminal_job_ids?: string[];
};

export type CommandApprovalItem = {
  kind: "command_approval";
  id: string;
  command: string;
  commandHash: string;
  sessionId: string;
  workspaceRoot: string;
  category: string;
  reason: string;
  matched: string;
  status: "pending" | "approving" | "approved" | "rejected" | "error";
  error?: string;
};

export type Item =
  | { kind: "msg"; msg: Msg }
  | { kind: "card"; card: Card }
  | { kind: "thinking"; text: string; streaming?: boolean; id?: string }
  | { kind: "tool_prep"; name: string }
  | SwarmPendingItem
  | { kind: "swarm_result"; job_id: string; applied: boolean; files: string[]; summary: string; error: string | null; objective?: string }
  | { kind: "checkpoint"; id: string; label: string; trigger: string }
  | { kind: "compaction"; before_tokens: number; after_tokens: number }
  | { kind: "codegraph_context"; symbols: number; query: string }
  | { kind: "command_blocked"; command: string; category: string; reason: string; matched: string }
  | CommandApprovalItem
  | { kind: "auto_status"; cycle: number; snapshot: AutoBudgetSnapshot }
  | { kind: "auto_halt"; reason: string; snapshot: AutoBudgetSnapshot }
  | { kind: "auth_failure"; message: string; id?: string }
  | { kind: "steer"; text: string };

export type GroupedItem =
  | { kind: "msg"; msg: Msg }
  | { kind: "thinking"; text: string; streaming?: boolean; id?: string }
  | SwarmPendingItem
  | { kind: "swarm_result"; job_id: string; applied: boolean; files: string[]; summary: string; error: string | null; objective?: string }
  | { kind: "checkpoint"; id: string; label: string; trigger: string }
  | { kind: "compaction"; before_tokens: number; after_tokens: number }
  | { kind: "codegraph_context"; symbols: number; query: string }
  | { kind: "command_blocked"; command: string; category: string; reason: string; matched: string }
  | CommandApprovalItem
  | { kind: "auto_status"; cycle: number; snapshot: AutoBudgetSnapshot }
  | { kind: "auto_halt"; reason: string; snapshot: AutoBudgetSnapshot }
  | { kind: "auth_failure"; message: string; id?: string }
  | { kind: "steer"; text: string }
  | { kind: "activity_group"; items: ActivityItem[] };

type ActivityItem =
  | { kind: "card"; card: Card }
  | { kind: "thinking"; text: string; streaming?: boolean; id?: string }
  | { kind: "codegraph_context"; symbols: number; query: string }
  | { kind: "checkpoint"; id: string; label: string; trigger: string }
  | { kind: "swarm_result"; job_id: string; applied: boolean; files: string[]; summary: string; error: string | null; objective?: string }
  | { kind: "msg"; msg: Msg };


/**
 * Assistants that belong inside the investigation fold for this turn.
 *
 * Open-loop absorption (fold mid-turn narration once the turn has tools /
 * thinking) applies ONLY to the current turn — the span after the last user
 * message. Prior turns always use the sealed rule: fold only assistants that
 * still have a later tool card. Without that scope, a live turn would re-fold
 * every historical finale into its Explored group, shrink the render-window
 * group count, then peel those finales back out on seal and shove older
 * Explored / swarm-done rows behind "Show earlier messages" (the disappear).
 *
 * Current turn while open: fold streaming + sealed narration once there is
 * investigation activity (or while streaming before the first tool).
 * Current turn when closed / any prior turn: trailing final stands alone.
 */
export function collectIntermediateAssistantItems(
  items: Item[],
  agentLoopOpen: boolean,
): Set<Item> {
  const intermediateItems = new Set<Item>();
  let lastUserIdx = -1;
  for (let i = 0; i < items.length; i++) {
    const row = items[i];
    if (row.kind === "msg" && row.msg.role === "user") lastUserIdx = i;
  }
  const currentTurnStart = lastUserIdx >= 0 ? lastUserIdx + 1 : 0;

  let turnStart = 0;
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    if (item.kind === "msg" && item.msg.role === "user") {
      turnStart = i + 1;
      continue;
    }
    if (item.kind !== "msg" || item.msg.role !== "assistant") continue;

    const seenCardBefore = items
      .slice(turnStart, i)
      .some((row) => row.kind === "card");
    const turnHasFoldActivity = items
      .slice(turnStart)
      .some((row) => row.kind === "card" || row.kind === "thinking");

    // Open-loop absorption is current-turn only (see docstring).
    const openAbsorb = agentLoopOpen && i >= currentTurnStart;
    if (openAbsorb) {
      // Mid-turn: keep streaming + sealed narration inside the fold from the
      // first token once the turn is investigative, and while streaming even
      // before the first tool (avoids outside→absorb blink). Pure chat still
      // peels to a standalone finale when the loop closes (no card after).
      if (turnHasFoldActivity || item.msg.streaming === true) {
        intermediateItems.add(item);
      }
      continue;
    }

    if (!seenCardBefore) continue; // sealed pre-tool sticky outside when done
    // Sealed / prior turns: fold only if another tool card still follows.
    let cardAfter = false;
    for (let j = i + 1; j < items.length; j++) {
      const later = items[j];
      if (later.kind === "msg" && later.msg.role === "user") break;
      if (later.kind === "card") {
        cardAfter = true;
        break;
      }
    }
    if (cardAfter) intermediateItems.add(item);
  }
  return intermediateItems;
}

function groupAgentActivity(items: Item[], intermediateItems: Set<Item>): GroupedItem[] {
  // Mental model: a turn is [user msg] + sticky pre-tool bubbles +
  // [investigation fold: thinking / tools / post-tool micro-narration] +
  // [final answer]. Surfaces do not reclassify after first paint.
  const grouped: GroupedItem[] = [];
  let currentGroup: ActivityItem[] = [];

  const flush = () => {
    if (currentGroup.length > 0) {
      grouped.push({ kind: "activity_group", items: currentGroup });
      currentGroup = [];
    }
  };

  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    if (item.kind === "thinking" && (!item.text || !item.text.trim())) continue;
    // tool_prep is busy-footer only -- never a transcript row.
    if (item.kind === "tool_prep") continue;

    if (item.kind === "msg") {
      // Post-tool micro-narration folds into the investigation box. Pre-tool
      // assistant bubbles stay standalone permanently (no look-ahead reparent).
      if (item.msg.role === "assistant" && intermediateItems.has(item)) {
        currentGroup.push(item);
      } else {
        flush();
        grouped.push(item);
      }
    } else if (item.kind === "swarm_result" || item.kind === "checkpoint") {
      // These are emitted by tool execution, so they belong inside the same
      // collapsed investigation as the action card that produced them. Rendering
      // them as standalone chips made the transcript vertically noisy.
      currentGroup.push(item);
    } else if (
      item.kind === "swarm_pending"
      || item.kind === "compaction"
      || item.kind === "command_blocked"
      || item.kind === "command_approval"
      || item.kind === "auto_status"
      || item.kind === "auto_halt"
      || item.kind === "auth_failure"
      || item.kind === "steer"
    ) {
      flush();
      grouped.push(item);
    } else if (item.kind === "card" || item.kind === "thinking" || item.kind === "codegraph_context") {
      // Cards, reasoning, codegraph chips: all collect into the one box.
      currentGroup.push(item);
    }
  }

  flush();
  return grouped;
}

// PERF: Stable per-item keys for the transcript map. Array-index keys forced
// React to reconcile every sibling whenever the list changed (streaming,
// grouping); a stable identity lets React skip untouched rows. We derive the
// key from the item's underlying object identity where possible (msg objects
// keep stable references across renders because setItems only appends), and
// fall back to content + index only when no object identity is available.
const __transcriptKeys = new WeakMap<object, string>();
let __transcriptKeySeq = 0;
function objKey(obj: object): string {
  let k = __transcriptKeys.get(obj);
  if (!k) {
    k = `k${__transcriptKeySeq++}`;
    __transcriptKeys.set(obj, k);
  }
  return k;
}
// Persist Investigated-toggle open state across remounts. Card patches used to
// replace the lead item's object identity, which changed the React key, remounted
// ActivityGroup, and reset useState(false) -- the "blinks itself closed" bug.
const __activityOpen = new Map<string, boolean>();
// Reasoning expand preference (user click) survives remounts / live→idle flips.
const __thinkingExpanded = new Map<string, boolean>();
// Alias every durable member of an investigation onto one canon key so a
// thinking-only group does not remount when the first tool card arrives (and
// the reverse). Streaming used to key off objKey(thinking) which changed every
// token and remounted the fold -- expand clicked shut, inner scroll stuck at top.
const __activityGroupCanon = new Map<string, string>();

/** Stable React key for one investigation fold. Exported for unit tests. */
export function activityGroupStableId(items: ActivityItem[], fallbackIndex: number): string {
  // Collect durable members (thinking ids first so a live reasoning stream that
  // later grows tool cards keeps the same canon). ALWAYS suffix the group index:
  // duplicate card ids in a corrupted/replayed transcript must not share one
  // React key (that remounts one group across every sibling).
  const members: string[] = [];
  for (const it of items) {
    if (it.kind === "thinking" && it.id) members.push(`t:${it.id}`);
  }
  for (const it of items) {
    if (it.kind === "card" && it.card?.id) members.push(`c:${it.card.id}`);
  }
  for (const it of items) {
    if (it.kind === "checkpoint") members.push(`k:${it.id}`);
    if (it.kind === "swarm_result") members.push(`s:${it.job_id}`);
  }

  let canon: string | undefined;
  for (const m of members) {
    const hit = __activityGroupCanon.get(m);
    if (hit) {
      canon = hit;
      break;
    }
  }
  if (!canon) {
    canon = members[0]
      ? `grp-${members[0]}`
      : items[0]
        ? `grp-${objKey(items[0])}`
        : `grp-${fallbackIndex}`;
  }
  for (const m of members) __activityGroupCanon.set(m, canon);
  // Canon alone is the open-state id. Do NOT suffix fallbackIndex here —
  // when a turn finishes (hoist/regroup) group indices shift, and an
  // index-suffixed id remounted every prior fold as "new" → default-open.
  return canon;
}

function stableItemKey(it: GroupedItem, i: number): string {
  switch (it.kind) {
    case "msg":
      return `msg-${objKey(it.msg)}`;
    case "activity_group":
      // React key keeps the index so duplicate-card corruption cannot collide;
      // ActivityGroup's groupId (open map) stays on the canon alone.
      return `${activityGroupStableId(it.items, i)}#${i}`;
    case "swarm_result":
      return `swres-${it.job_id}`;
    case "swarm_pending":
      return `swpen-${(it.job_ids || []).join("_") || i}`;
    case "checkpoint":
      return `ckpt-${it.id}`;
    case "compaction":
      return `cmp-${it.before_tokens}-${it.after_tokens}-${i}`;
    case "codegraph_context":
      return `cg-${i}-${it.symbols}`;
    case "command_blocked":
      return `blk-${i}-${it.category}`;
    case "command_approval":
      return `cmd-approval-${it.commandHash}`;
    case "auto_status":
      return `auto-status-${it.cycle}`;
    case "auto_halt":
      return `auto-halt-${i}-${(it.reason || "").slice(0, 24)}`;
    case "auth_failure":
      return `auth-${it.id || i}`;
    case "steer":
      return `steer-${i}`;
    case "thinking":
      return it.id ? `think-${it.id}` : `think-${i}`;
    default:
      return `item-${i}`;
  }
}

// PERF: Long sessions grow the transcript without bound, and every displayed
// group is an expensive subtree (markdown + syntax highlight + tool cards). Cap
// the DOM at the newest RENDER_WINDOW groups; a "Show earlier messages" affordance
// prepends another window on demand. This is pure rendering -- older groups are
// simply not mounted -- so it never touches scrollTop and can't fight the
// parent's stick-to-bottom autoscroll. Adapted from the Hermes desktop thread's
// render-budget windowing (which counts message parts; Marionette counts the
// coarser display groups, one rendered subtree each). Short sessions render in
// full and never show the button.
//
// Sized so a LONG session actually windows: at 200 the cap effectively never
// fired (a session needs 200+ grouped turns before anything collapses), so long
// sessions never cut off and never showed "Show earlier messages" -- the exact
// symptom users hit. 40 groups keeps a comfortably long recent window mounted
// (each group is a full user-turn subtree) while genuinely long sessions cap and
// surface the button; "Show earlier" prepends another 40 on demand.
const RENDER_WINDOW = 40;

// PERF: Memoized transcript renderer. Its props are intentionally free of the
// composer `input` (or any per-keystroke state), so React.memo lets typing skip
// re-rendering the whole transcript. Only transcript-affecting state (items,
// status, compactingStatus, editingIndex, auto, plan) plus stable callbacks are
// passed in; all callbacks are useCallback-stabilized in the parent so the memo
// comparison holds.
export type TranscriptListProps = {
  items: Item[];
  status: "idle" | "thinking" | "executing" | "done" | "error" | "streaming";
  compactingStatus: string | null;
  editingIndex: number | null;
  auto: boolean;
  plan: boolean;
  /** Wall-clock ms since the current busy turn began (for elapsed on the footer). */
  busyElapsedMs?: number | null;
  /**
   * Sticky open-turn latch from Conversation (true until assistant_done / Stop).
   * Keeps mid-turn narration folded into Investigating between tool batches.
   */
  turnOpen?: boolean;
  scrollContainerRef: React.RefObject<HTMLDivElement | null>;
  onEditMessage: (idx: number, originalText: string) => void;
  onExecuteSend: (msg: string, useAuto: boolean, usePlan?: boolean) => void;
  onImageClick: (url: string) => void;
  onSetCard: (id: string, patch: Partial<Card>) => void;
  onExecutePlan: (planText: string) => void;
  onCommandApproval: (item: CommandApprovalItem, approve: boolean) => void;
};

export const TranscriptList = memo(function TranscriptList({
  items,
  status,
  compactingStatus,
  editingIndex,
  auto,
  plan,
  busyElapsedMs = null,
  turnOpen = false,
  scrollContainerRef,
  onEditMessage,
  onExecuteSend,
  onImageClick,
  onSetCard,
  onExecutePlan,
  onCommandApproval,
}: TranscriptListProps) {
  const agentLoopOpen =
    turnOpen
    || status === "thinking"
    || status === "executing"
    || status === "streaming";

  const intermediateItems = collectIntermediateAssistantItems(items, agentLoopOpen);
  const grouped = groupAgentActivity(items, intermediateItems);

  // PERF: window to the newest RENDER_WINDOW display groups. Walk newest-first,
  // counting groups until the window is filled; everything before that is hidden
  // behind the "Show earlier messages" button. Short sessions never fill the
  // window, so hiddenCount stays 0 and nothing changes for them.
  const [renderWindow, setRenderWindow] = useState(RENDER_WINDOW);
  let firstVisible = grouped.length;
  for (let i = grouped.length - 1, shown = 0; i >= 0; i--) {
    shown += 1;
    firstVisible = i;
    if (shown >= renderWindow) break;
  }
  const hiddenCount = firstVisible;

  // Prepend an older window while preserving the reading position: capture the
  // distance from the bottom before the content grows, restore it once the taller
  // content has laid out. The user is scrolled up here, so the parent's
  // stick-to-bottom autoscroll is already released and won't fight this.
  const restoreFromBottomRef = useRef<number | null>(null);
  const showEarlier = useCallback(() => {
    const el = scrollContainerRef.current;
    restoreFromBottomRef.current = el ? el.scrollHeight - el.scrollTop : null;
    setRenderWindow((w) => w + RENDER_WINDOW);
  }, [scrollContainerRef]);
  useLayoutEffect(() => {
    const el = scrollContainerRef.current;
    if (el && restoreFromBottomRef.current != null) {
      el.scrollTop = el.scrollHeight - restoreFromBottomRef.current;
      restoreFromBottomRef.current = null;
    }
  }, [renderWindow, scrollContainerRef]);

  // Find the last assistant message inside the original items array
  let lastAssistantRawIdx = -1;
  for (let idx = items.length - 1; idx >= 0; idx--) {
    const itm = items[idx];
    if (itm.kind === "msg") {
      const msgItm = itm as { kind: "msg"; msg: Msg };
      if (msgItm.msg.role === "assistant") {
        lastAssistantRawIdx = idx;
        break;
      }
    }
  }

  // Find the last user message text
  let lastUserText = "";
  for (let idx = items.length - 1; idx >= 0; idx--) {
    const itm = items[idx];
    if (itm.kind === "msg") {
      const msgItm = itm as { kind: "msg"; msg: Msg };
      if (msgItm.msg.role === "user") {
        lastUserText = msgItm.msg.text;
        break;
      }
    }
  }

  let lastActivityGroupIdx = -1;
  for (let gi = grouped.length - 1; gi >= 0; gi--) {
    if (grouped[gi].kind === "activity_group") {
      lastActivityGroupIdx = gi;
      break;
    }
  }

  const list = grouped.map((it, i) => {
    if (i < hiddenCount) return null;
    const key = stableItemKey(it, i);
    if (it.kind === "msg") {
      const rawIdx = items.findIndex(raw => raw.kind === "msg" && (raw as { kind: "msg"; msg: Msg }).msg === it.msg);

      let prevMsg: Msg | null = null;
      for (let j = i - 1; j >= 0; j--) {
        const prevItem = grouped[j];
        if (prevItem.kind === "msg") {
          prevMsg = prevItem.msg;
          break;
        }
      }
      const isFirstInRun = !prevMsg || prevMsg.role !== "assistant";
      const isIntermediate = intermediateItems.has(it as Item);

      const onEdit = it.msg.role === "user" ? () => onEditMessage(rawIdx, it.msg.text) : undefined;
      const isEditing = editingIndex === rawIdx;

      const isLastAssistant = rawIdx === lastAssistantRawIdx;
      const isNotBusy = !agentLoopOpen && (status === "idle" || status === "done" || status === "error");
      const onRegenerate = (isLastAssistant && isNotBusy && lastUserText)
        ? () => { onExecuteSend(lastUserText, auto, plan); }
        : undefined;

      return (
        <Bubble
          key={key}
          msg={it.msg}
          showLabel={it.msg.role === "assistant" ? isFirstInRun : false}
          isIntermediate={isIntermediate}
          onExecutePlan={(planText) => onExecutePlan(planText)}
          onEdit={onEdit}
          isEditing={isEditing}
          onRegenerate={onRegenerate}
          onImageClick={(url) => onImageClick(url)}
        />
      );
    } else if (it.kind === "swarm_pending") {
      const objText = it.objective || "";
      const truncatedObj = objText.length > 60 ? objText.slice(0, 60) + "..." : objText;
      const jobIdsStr = (it.job_ids || []).join(", ");
      const pillStatus: SwarmPendingStatus =
        it.status || (it.resolved ? "done" : "running");
      if (pillStatus === "done") {
        return (
          <div key={key} className="flex items-center gap-1.5 py-1 px-3 rounded-full bg-panel2/20 border border-edge/30 text-[11px] text-faint w-fit my-1 select-none">
            <span className="w-1.5 h-1.5 rounded-full bg-good/40" />
            <span>swarm done: {truncatedObj} ({jobIdsStr})</span>
          </div>
        );
      }
      if (pillStatus === "failed") {
        return (
          <div key={key} className="flex items-center gap-1.5 py-1 px-3 rounded-full bg-risk/10 border border-risk/30 text-[11px] text-risk/80 w-fit my-1 select-none">
            <span className="w-1.5 h-1.5 rounded-full bg-risk/50" />
            <span>swarm failed: {truncatedObj} ({jobIdsStr})</span>
          </div>
        );
      }
      if (pillStatus === "ended") {
        return (
          <div key={key} className="flex items-center gap-1.5 py-1 px-3 rounded-full bg-panel2/15 border border-edge/20 text-[11px] text-faint w-fit my-1 select-none">
            <span className="w-1.5 h-1.5 rounded-full bg-faint/40" />
            <span>swarm ended: {truncatedObj} ({jobIdsStr})</span>
          </div>
        );
      }
      return (
        <div key={key} className="flex items-center gap-1.5 py-1 px-3 rounded-full bg-panel2/60 border border-edge/60 text-[11px] text-muted w-fit my-1 select-none">
          <Loader2 size={11} className="animate-spin text-accent" />
          <span>swarm running: {truncatedObj} ({jobIdsStr})</span>
        </div>
      );
    } else if (it.kind === "swarm_result") {
      return (
        <SwarmResultCard
          key={key}
          applied={it.applied}
          files={it.files}
          summary={it.summary}
          error={it.error}
          objective={it.objective}
        />
      );
    } else if (it.kind === "checkpoint") {
      return (
        <div key={key} className="flex items-center gap-1.5 py-1 px-3 rounded-full bg-panel2/15 border border-edge/20 text-[10px] text-faint w-fit my-1 select-none">
          <History size={11} className="text-accent" />
          <span>restore point created: {it.label} ({it.id.slice(0, 8)})</span>
        </div>
      );
    } else if (it.kind === "codegraph_context") {
      return (
        <div key={key} className="flex items-center gap-1.5 py-0.5 text-[10px] text-accent/70 w-fit my-0.5 select-none" title={it.query ? `CodeGraph consulted for: ${it.query}` : "CodeGraph consulted"}>
          <Share2 size={9} className="text-accent/70" />
          <span>CodeGraph consulted{it.symbols > 0 ? ` -- ${it.symbols} symbols` : ""}</span>
        </div>
      );
    } else if (it.kind === "command_blocked") {
      const blocked = commandBlockedPresentation(it);
      return (
        <div
          key={key}
          className="flex items-start gap-1.5 py-1 px-3 rounded-full bg-panel2/10 border border-risk/25 text-[10.5px] text-muted w-fit max-w-full my-1 select-none"
          title={it.matched ? `matched: ${it.matched}` : "Full-auto did not execute this command"}
        >
          <span className="font-medium shrink-0 text-risk/80">{blocked.label}</span>
          <span className="min-w-0">
            <span className="text-faint">{blocked.detail}</span>
            {it.command ? <code className="block mt-0.5 text-[10px] text-faint/80 font-mono truncate">{it.command}</code> : null}
          </span>
        </div>
      );
    } else if (it.kind === "command_approval") {
      const decisionPending = it.status === "pending" || it.status === "error";
      const statusCopy = commandApprovalStatusCopy(it.status);
      return (
        <div
          key={key}
          role="alert"
          className="w-full max-w-2xl rounded-md border border-edge bg-panel2/40 px-3.5 py-3 text-[11px] text-txt my-1.5"
        >
          <div className="flex items-start gap-2">
            <XCircle size={15} className="mt-0.5 shrink-0 text-risk/80" />
            <div className="min-w-0 flex-1">
              <div className="font-medium text-txt">Command needs approval</div>
              <div className="mt-0.5 text-muted">
                Full-auto did not run this command.{" "}
                {it.reason || it.category || "Safety policy requires an explicit decision."}
              </div>
              <code className="mt-2 block max-h-28 overflow-auto rounded border border-edge bg-panel/60 p-2 font-mono text-[10.5px] text-muted whitespace-pre-wrap break-all select-text">
                {it.command}
              </code>
              {it.error ? <div className="mt-1.5 text-risk/90">{it.error}</div> : null}
              <div className="mt-2 flex items-center gap-2">
                {decisionPending ? (
                  <>
                    <button
                      type="button"
                      onClick={() => onCommandApproval(it, true)}
                      className="rounded-md border border-edge bg-panel px-2.5 py-1 font-medium text-txt hover:border-accent/40"
                    >
                      Approve once and retry
                    </button>
                    <button
                      type="button"
                      onClick={() => onCommandApproval(it, false)}
                      className="rounded-md border border-edge bg-panel2/60 px-2.5 py-1 text-muted hover:text-txt"
                    >
                      Reject
                    </button>
                  </>
                ) : (
                  <span className="text-faint">{statusCopy}</span>
                )}
              </div>
            </div>
          </div>
        </div>
      );
    } else if (it.kind === "auto_status") {
      const status = autoStatusPresentation(it.cycle, it.snapshot);
      return (
        <div
          key={key}
          className="flex items-center gap-1.5 py-1 px-3 rounded-full bg-panel2/10 border border-edge/10 text-[10.5px] text-faint w-fit my-1 select-none font-mono"
          title="AutoBudget progress — not a completion or compaction receipt"
        >
          <span>{status.label}</span>
          {status.detail ? <span className="text-muted/80">· {status.detail}</span> : null}
        </div>
      );
    } else if (it.kind === "auto_halt") {
      const halt = autoHaltPresentation(it.reason, it.snapshot);
      return (
        <div
          key={key}
          className={`flex items-center gap-1.5 py-1 px-3 rounded-full border text-[10.5px] w-fit my-1 select-none font-mono ${
            halt.metObjective
              ? "bg-panel2/15 border-edge/20 text-muted"
              : "bg-panel2/10 border-edge/15 text-faint"
          }`}
          title={it.reason || "Full-auto ended"}
        >
          <span className={halt.metObjective ? "text-good/80" : "text-muted"}>{halt.label}</span>
          <span className="text-faint">· {halt.detail}</span>
        </div>
      );
    } else if (it.kind === "auth_failure") {
      return (
        <div key={key} role="alert" className="flex items-start gap-2 py-2.5 px-3.5 rounded-lg bg-red-500/12 border border-red-500/50 text-[12px] text-red-200 w-full max-w-full my-1.5 shadow-sm animate-in fade-in duration-200">
          <XCircle size={15} className="text-red-400 shrink-0 mt-0.5" />
          <span className="min-w-0">
            <span className="font-semibold text-red-300">Provider auth failure.</span>{" "}
            <span className="text-red-200/90">The API key was rejected -- this is a dead, revoked, or wrong key, not a weak model or bad prompt. Fix the named credential (e.g. OPENAI_API_KEY), then re-run.</span>
            {it.message ? <code className="block mt-1 text-[10.5px] text-red-200/80 font-mono break-all whitespace-pre-wrap">{it.message}</code> : null}
          </span>
        </div>
      );
    } else if (it.kind === "compaction") {
      return (
        <div key={key} className="flex items-center gap-1.5 py-1 px-3 rounded-full bg-panel2/10 border border-edge/10 text-[10.5px] text-faint w-fit my-1 select-none font-mono">
          <span>Context summarized: {it.before_tokens} → {it.after_tokens} tokens</span>
        </div>
      );
    } else if (it.kind === "steer") {
      return (
        <div key={key} className="flex items-center gap-1.5 py-1 px-3 rounded-full bg-panel2/15 border border-edge/20 text-[10.5px] text-faint w-fit my-1 select-none font-mono animate-in fade-in duration-200">
          <span className="text-muted">steer:</span>
          <span>{it.text}</span>
        </div>
      );
    } else if (it.kind === "thinking") {
      return (
        <ThinkingBlock
          key={key}
          blockId={it.id || key}
          text={it.text}
          live={Boolean(it.streaming)}
        />
      );
    } else if (it.kind === "activity_group") {
      const openId = activityGroupStableId(it.items, i);
      return (
        <ActivityGroup
          key={key}
          groupId={openId}
          items={it.items}
          loopOpen={agentLoopOpen && i === lastActivityGroupIdx}
          onToggleCard={(card) => onSetCard(card.id, { open: !card.open })}
        />
      );
    }
    return null;
  });

  const busyProgress = deriveBusyProgress(items, status, busyElapsedMs);
  // Hide flat busy footer while investigation rows own the status surface (T1),
  // or when the assistant answer already looks complete despite SSE lag (T5).
  const hideBusyFooter = turnHasLiveInvestigation(items, agentLoopOpen);
  const showBusyFooter = shouldShowBusyFooter(items, status) && !hideBusyFooter;
  // Quiet "Still working…" cue: whenever the turn is busy but no other chrome
  // visibly signals work (no running card, no streaming thinking/answer, busy
  // footer hidden by the investigation fold), show it IMMEDIATELY. The old 2s
  // arming timer left a dead gap at every tool boundary that read as an
  // idle→working flicker.
  const showStall = quietWorkingCueVisible(
    items,
    status,
    Boolean(compactingStatus),
    showBusyFooter,
  );

  return (
    <>
      {hiddenCount > 0 && (
        <button
          type="button"
          onClick={showEarlier}
          className="mx-auto mb-1 rounded-full border border-edge/60 bg-panel2/40 px-3 py-1 text-[11px] text-muted hover:text-txt hover:bg-panel2/70 transition-colors select-none"
        >
          Show earlier messages ({hiddenCount})
        </button>
      )}
      {list}
      {compactingStatus && (
        <div className="flex items-center gap-1.5 py-1 px-3 rounded-full bg-panel2/15 border border-edge/20 text-[11px] text-faint w-fit my-1 select-none animate-pulse">
          <Loader2 size={11} className="animate-spin text-accent" />
          <span>{compactingStatus}</span>
        </div>
      )}
      {showBusyFooter && !compactingStatus && (
        <div
          className="flex items-center gap-1.5 py-1 text-[12px] text-muted select-none mt-1 pl-0.5 min-w-0"
          title={busyProgress.runningGoal || busyProgress.label}
        >
          <Loader2 size={12} className="animate-spin text-muted shrink-0" />
          <span className="truncate font-mono text-[11.5px] tracking-tight">
            {busyProgress.label || (status === "thinking" ? "Waiting on provider…" : status === "streaming" ? "streaming..." : "running...")}
          </span>
        </div>
      )}
      {showStall && (
        <div
          className="flex items-center gap-1.5 py-1 text-[12px] text-muted/90 select-none mt-1 pl-0.5 min-w-0"
          data-testid="stream-stall"
        >
          <Loader2 size={12} className="animate-spin text-faint shrink-0" />
          <span className="truncate font-mono text-[11.5px] tracking-tight">
            Still working…
          </span>
        </div>
      )}
    </>
  );
});

function cleanAssistantText(text: string): string {
  const lines = text.split("\n");
  const cleaned: string[] = [];
  let inTraceback = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const stripped = line.trim();

    if (stripped.startsWith("USER: (") || stripped.includes("completed with exit code")) {
      continue;
    }
    if (stripped.match(/^\s*Traceback\s*\(most\s+recent\s+call\s+last\):/i)) {
      inTraceback = true;
      continue;
    }
    if (inTraceback) {
      if (stripped === "") {
        continue;
      }
      if (line.startsWith(" ") || line.startsWith("\t")) {
        continue;
      }
      inTraceback = false;
      continue;
    }
    if (stripped.includes("During handling of the above exception") || stripped.includes("The above exception was the direct cause")) {
      continue;
    }
    cleaned.push(line);
  }

  let result = cleaned.join("\n").trim();
  result = result.replace(/\n{3,}/g, "\n\n");
  return result || "Working...";
}

function isGateSuppressed(card: Card): boolean {
  const err = card.result?.error;
  return typeof err === "string" && err.startsWith("(SUPPRESSED");
}

function getCardMeta(card: Card): string | null {
  if (card.running) return null;
  const parts: string[] = [];

  const duration = card.result?.duration_ms;
  if (typeof duration === "number") {
    parts.push(`${duration}ms`);
  }

  if (isGateSuppressed(card)) {
    // Swarm/delegate gate blocked this call -- not a tool failure. Label it
    // honestly so a broad-ask turn doesn't look like a wall of red errors.
    parts.push("blocked");
  } else if (card.result?.error) {
    parts.push("error");
  } else if (card.result?.artifacts && card.result.artifacts.length > 0) {
    const headline = card.result.artifacts[0].headline || "";
    
    const readMatch = headline.match(/Read (\d+) chars/i);
    if (readMatch) {
      parts.push(`${readMatch[1]} chars`);
    } else {
      const writeMatch = headline.match(/Wrote (\d+) bytes/i);
      if (writeMatch) {
        parts.push(`${writeMatch[1]} B`);
      } else {
        const exitMatch = headline.match(/Command exited with (-?\d+)/i);
        if (exitMatch) {
          parts.push(`exit ${exitMatch[1]}`);
        }
      }
    }
  }

  return parts.length > 0 ? parts.join(" · ") : null;
}

function ActivityGroup({
  items,
  onToggleCard,
  groupId,
  loopOpen = false,
}: {
  items: ActivityItem[];
  onToggleCard: (card: Card) => void;
  groupId: string;
  /** True while this is the current turn's fold and the agent loop is still open. */
  loopOpen?: boolean;
}) {
  // Tool-bearing investigations start OPEN (Cursor/Hermes: see every write /
  // run as it happens). Pure Thought/reasoning stays collapsed. Seed from the
  // module map so a remount does not yank an explicit toggle mid-stream.
  const [open, setOpen] = useState(() => {
    if (__activityOpen.has(groupId)) return Boolean(__activityOpen.get(groupId));
    // Live tool-bearing folds start open; sealed history stays collapsed so a
    // remount / regroup at turn-end cannot re-expand every prior Investigating.
    if (!loopOpen) return false;
    return items.some((it) => it.kind === "card");
  });
  const toggleOpen = () => {
    setOpen((v) => {
      const next = !v;
      __activityOpen.set(groupId, next);
      return next;
    });
  };

  const cards = items.filter((it) => it.kind === "card") as { kind: "card"; card: Card }[];
  const cgItems = items.filter((it) => it.kind === "codegraph_context") as { kind: "codegraph_context"; symbols: number; query: string }[];
  const checkpointItems = items.filter((it) => it.kind === "checkpoint") as { kind: "checkpoint"; id: string; label: string; trigger: string }[];
  const swarmResults = items.filter((it) => it.kind === "swarm_result") as { kind: "swarm_result"; job_id: string; applied: boolean; files: string[]; summary: string; error: string | null; objective?: string }[];
  // Recount incrementally from visible top-level cards AND nested worker rows
  // so Explored / Investigating tracks the investigation timeline the user sees.
  const nestedRows = cards.flatMap((c) => c.card.actions || []);
  const actionCount = cards.length + nestedRows.length;
  // Ignore stale ``running`` when a terminal result body is already present —
  // otherwise every Explored group force-opens on seal and spinners never die.
  const anyRunning = cards.some((c) => cardEffectivelyRunning(c.card));
  const runningCard = [...cards].reverse().find((c) => cardEffectivelyRunning(c.card))?.card;
  const runningNested = runningCard
    ? undefined
    : [...nestedRows].reverse().find((a) => a.status === "running");
  const runningKind = toolFocusPhrase(
    runningCard?.kind || runningNested?.kind || "",
  );
  const runningGoal = shortenGoal(
    resolveCardCliInput(runningCard || {}) || runningNested?.goal || "",
  );
  const narrationMsgs = items.filter(
    (it) => it.kind === "msg" && (it as { kind: "msg"; msg: Msg }).msg.text.trim()
  ) as { kind: "msg"; msg: Msg }[];
  const thinkingItems = items.filter(
    (it) => it.kind === "thinking" && (it as { kind: "thinking"; text: string }).text.trim()
  ) as { kind: "thinking"; text: string; streaming?: boolean; id?: string }[];
  const liveThinking = thinkingItems.some((t) => t.streaming);
  // Keep Investigating across gaps between tool steps (loop still open).
  // Cursor CLI often streams reasoning before any tool_call event — treat
  // live thinking + open loop as Investigating so the fold is not blank until
  // tools flush at the end of the agent subprocess.
  const investigating =
    anyRunning
    || liveThinking
    || (loopOpen && (actionCount > 0 || thinkingItems.length > 0));

  // Auto-open while tools/reasoning are live ONLY when the user has never
  // toggled this group. A prior remount reset autoOpenedRef and re-forced open
  // on every tool call -- expand clicked shut, then snapped open again.
  useEffect(() => {
    if (!(anyRunning || liveThinking || investigating)) return;
    if (__activityOpen.has(groupId)) return;
    setOpen(true);
    __activityOpen.set(groupId, true);
  }, [anyRunning, liveThinking, investigating, groupId]);

  // A group with NO tool actions, no narration AND no reasoning (just a lone
  // CodeGraph chip from the per-step auto-injection) would render a misleading
  // "0 steps" box -- suppress it. But folded intermediate narration OR a reasoning
  // trace must still show (collapsed), so reasoning never silently vanishes from
  // the step list the way it used to.
  if (actionCount === 0 && narrationMsgs.length === 0 && thinkingItems.length === 0 && checkpointItems.length === 0 && swarmResults.length === 0) {
    return null;
  }

  const narrationPreview = narrationMsgs.length
    ? narrationMsgs[narrationMsgs.length - 1].msg.text.trim().split("\n", 1)[0]
    : (thinkingItems.length
        ? thinkingItems[thinkingItems.length - 1].text.trim().split("\n", 1)[0]
        : "");

  // Cursor-style kind buckets ("3 files, 1 search") for Explored / Investigating.
  const kindSummary = aggregateExplorationSummary([
    ...cards.map((c) => c.card.kind || "action"),
    ...nestedRows.map((a) => a.kind || "action"),
  ]);
  const stepHeadline = investigatingHeadline(
    actionCount,
    investigating,
    runningKind,
    runningGoal,
    kindSummary,
  );

  const renderInner = (it: typeof items[number], idx: number) => {
    if (it.kind === "card") {
      return <ActionCard key={it.card.id || `card-${idx}`} card={it.card} onToggle={() => onToggleCard(it.card)} />;
    }
    if (it.kind === "thinking") {
      const blockId = it.id || `${groupId}-think-${idx}`;
      return (
        <ThinkingBlock
          key={blockId}
          blockId={blockId}
          text={it.text}
          live={Boolean(it.streaming)}
        />
      );
    }
    if (it.kind === "msg") {
      // Per-step micro-narration inside the collapsible tool-call breakdown.
      // Render through <Markdown> (not raw whitespace-pre-wrap) so code blocks,
      // bold, lists, etc. survive here exactly like they do in the main
      // transcript -- previously these folded messages lost all formatting.
      if (!it.msg.text || !it.msg.text.trim()) return null;
      return (
        <div key={objKey(it.msg)} className="text-[12px] text-muted/90 py-0.5 leading-relaxed">
          <Markdown text={it.msg.text} />
        </div>
      );
    }
    if (it.kind === "codegraph_context") {
      return (
        <div key={`cg-${idx}-${it.symbols}`} className="flex items-center gap-1.5 py-0.5 text-[10px] text-faint/70 select-none" title={it.query ? `CodeGraph consulted for: ${it.query}` : "CodeGraph consulted"}>
          <Share2 size={9} className="text-faint/60" />
          <span>CodeGraph consulted{it.symbols > 0 ? ` -- ${it.symbols} symbols` : ""}</span>
        </div>
      );
    }
    if (it.kind === "checkpoint") {
      return (
        <div key={`ckpt-${it.id}`} className="flex items-center gap-1.5 py-0.5 text-[10px] text-faint/80 select-none">
          <History size={10} className="text-faint/70" />
          <span>restore point created: {it.label} ({it.id.slice(0, 8)})</span>
        </div>
      );
    }
    if (it.kind === "swarm_result") {
      return (
        <SwarmResultCard
          key={`swres-${it.job_id}`}
          applied={it.applied}
          files={it.files}
          summary={it.summary}
          error={it.error}
          objective={it.objective}
        />
      );
    }
    return null;
  };

  // Tiny groups (1-2 actions, no codegraph noise, no narration) render inline --
  // collapsing them would add a click for no benefit.
  const hasMsg = items.some((it) => it.kind === "msg" && (it as { kind: "msg"; msg: Msg }).msg.text.trim());
  if (actionCount <= 2 && cgItems.length === 0 && !hasMsg && checkpointItems.length === 0 && swarmResults.length === 0) {
    return (
      <div className="flex flex-col gap-0.5 pl-3 border-l-2 border-edge/40 my-1 w-full">
        {items.map(renderInner)}
      </div>
    );
  }

  return (
    <div className="my-1 w-full">
      <button
        onClick={toggleOpen}
        className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-panel2/20 border border-edge/30 hover:bg-panel2/40 transition text-[11px] text-muted w-fit select-none"
      >
        {open ? <ChevronDown size={11} className="text-faint/70" /> : <ChevronRight size={11} className="text-faint/70" />}
        {investigating ? <Loader2 size={11} className="animate-spin text-faint" /> : <Share2 size={10} className="text-faint/70" />}
        {actionCount > 0 ? (
          <span
            className="text-txt/70 font-medium tracking-tight truncate max-w-[52ch] normal-case"
            title={investigating ? (runningCard?.goal || stepHeadline) : stepHeadline}
          >
            {stepHeadline}
          </span>
        ) : (
          <>
            <span className="text-txt/70 font-medium tracking-tight">
              {swarmResults.length > 0 ? "Swarm" : "Thought"}
            </span>
            <span className="text-faint truncate max-w-[46ch] normal-case">
              {swarmResults.length > 0
                ? `${swarmResults.length} result${swarmResults.length === 1 ? "" : "s"}`
                : narrationPreview}
            </span>
          </>
        )}
        {cgItems.length > 0 && (
          <span className="ml-0.5 text-faint/70">+ CodeGraph</span>
        )}
        {checkpointItems.length > 0 && (
          <span className="ml-0.5 text-faint/70">+ {checkpointItems.length} restore point{checkpointItems.length === 1 ? "" : "s"}</span>
        )}
        {swarmResults.length > 0 && (
          <span className="ml-0.5 text-good/75">+ swarm done</span>
        )}
      </button>
      {open && (
        <div className="flex flex-col gap-0.5 pl-3 mt-1 border-l-2 border-edge/40 w-full">
          {items.map(renderInner)}
        </div>
      )}
    </div>
  );
}


function ThinkingBlock({
  text,
  live = false,
  blockId,
}: {
  text: string;
  live?: boolean;
  blockId: string;
}) {
  // Cursor/Hermes-style compression: reasoning collapses to a single header line
  // by default (a faint preview of the first line hints at the content), and
  // expands into a height-capped, scrollable window rather than dumping its full
  // height inline. Unbounded inline reasoning is what used to blow up the window
  // and bury the actual answer, so the compact default is the legibility win.
  // While live-streaming, render plain text -- full markdown + syntax highlight
  // on every delta was a major CPU sink.
  //
  // Expand preference is sticky: never re-force open on live/tool updates once
  // the user has toggled. Inner scroll stick-to-bottom follows new tokens only
  // while the user stays pinned near the bottom of this box.
  const [expanded, setExpanded] = useState(
    () => __thinkingExpanded.get(blockId) ?? live,
  );
  const bodyRef = useRef<HTMLDivElement>(null);
  const pinnedInnerRef = useRef(true);

  useEffect(() => {
    if (!live) return;
    if (__thinkingExpanded.has(blockId)) return;
    setExpanded(true);
  }, [live, blockId]);

  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (!el || !expanded || !live) return;
    if (pinnedInnerRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [text, expanded, live]);

  if (!text || !text.trim()) {
    return null;
  }

  const preview = text.trim().split("\n", 1)[0].slice(0, 160);

  return (
    <div className="flex flex-col w-full py-0.5 min-w-0">
      <button
        onClick={() => {
          setExpanded((v) => {
            const next = !v;
            __thinkingExpanded.set(blockId, next);
            return next;
          });
        }}
        className="flex items-center gap-1 text-faint/70 hover:text-muted transition font-mono text-[10px] text-left w-full min-w-0 select-none uppercase tracking-wide"
        aria-expanded={expanded}
        title={expanded ? "Collapse reasoning" : "Expand reasoning"}
      >
        {expanded ? <ChevronDown size={9} className="text-faint/70 shrink-0" /> : <ChevronRight size={9} className="text-faint/70 shrink-0" />}
        <span className="shrink-0">{live ? "thinking" : "reasoning"}</span>
        {!expanded && (
          <span className="ml-1 truncate normal-case tracking-normal font-sans text-faint/50">{preview}</span>
        )}
      </button>
      {expanded && (
        <div
          ref={bodyRef}
          onScroll={() => {
            const el = bodyRef.current;
            if (!el) return;
            pinnedInnerRef.current =
              el.scrollHeight - el.scrollTop - el.clientHeight < 48;
          }}
          onWheel={(e) => {
            // Keep wheel deltas inside this capped pane so the outer transcript
            // does not steal scroll while the user reads a long live thought.
            const el = bodyRef.current;
            if (!el) return;
            const atTop = el.scrollTop <= 0;
            const atBottom =
              el.scrollHeight - el.scrollTop - el.clientHeight <= 1;
            if ((e.deltaY < 0 && !atTop) || (e.deltaY > 0 && !atBottom)) {
              e.stopPropagation();
            }
            if (e.deltaY < 0) pinnedInnerRef.current = false;
          }}
          className="mt-0.5 pl-2.5 ml-1 border-l-2 border-edge/40 overflow-y-auto overscroll-contain text-faint/85 text-[11px] leading-[1.65] max-w-[92%] max-h-[34dvh]"
        >
          {live ? (
            <pre className="whitespace-pre-wrap font-sans text-[11px] leading-[1.65] text-faint/85 m-0">
              {text}
            </pre>
          ) : (
            <Markdown text={text} />
          )}
        </div>
      )}
    </div>
  );
}

// Recursively pull the raw text out of a React node tree. react-markdown hands
// a fenced block's `children` as an ARRAY of nodes (one per line/segment) once
// it spans multiple lines, so String(children) stringifies the array and emits
// ",[object Object]," garbage. Walk the tree and concatenate real text instead
// so multi-line copies (e.g. shell command blocks) come out verbatim.
function nodeToText(node: any): string {
  if (node == null || node === false) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeToText).join("");
  if (typeof node === "object" && node.props) return nodeToText(node.props.children);
  return "";
}

/** Pull a file-ish path from a tree / listing line (`├── poll_loop.py:12  # note`). */
function pathTokenInCodeLine(line: string): { before: string; path: string; after: string } | null {
  const m = line.match(
    /^(.*?)((?:[\w.-]+[\\/])*[\w.-]+\.\w{1,8}(?::\d+){0,2})(\s*(?:#.*)?)$/,
  );
  if (!m) return null;
  const path = m[2];
  const bare = path.replace(/(?::\d+){1,2}$/, "");
  if (!looksLikePathInlineCode(bare) && !looksLikePathInlineCode(bare.split(/[\\/]/).pop() || "")) {
    return null;
  }
  return { before: m[1], path, after: m[3] || "" };
}

function FencedCodeBlock({ className, children, ...props }: any) {
  const [copied, setCopied] = useState(false);
  const codeText = nodeToText(children).replace(/\n$/, "");
  const lines = codeText.split("\n");
  const pathLines = lines.filter((ln) => pathTokenInCodeLine(ln)).length;
  // Directory trees / file lists: make paths open in the editor on click.
  const clickableTree = pathLines >= 2 || (lines.length <= 4 && pathLines >= 1);

  const handleCopy = () => {
    navigator.clipboard.writeText(codeText);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };

  return (
    <div className="relative group/code my-2">
      {clickableTree ? (
        <pre
          className={`${className || ""} block bg-panel/80 border border-accent/20 rounded-md p-3 pr-10 overflow-x-auto font-mono text-[0.719rem] leading-[1.55] text-txt/90 m-0 whitespace-pre`}
          {...props}
        >
          {lines.map((line, i) => {
            const tok = pathTokenInCodeLine(line);
            if (!tok) {
              return <span key={i}>{line}{i < lines.length - 1 ? "\n" : ""}</span>;
            }
            return (
              <span key={i}>
                {tok.before}
                <button
                  type="button"
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    openAgentFile(tok.path);
                  }}
                  title={`Open ${tok.path}`}
                  className="text-accent/90 hover:underline underline-offset-2 cursor-pointer bg-transparent border-0 p-0 font-inherit"
                >
                  {tok.path}
                </button>
                {tok.after}
                {i < lines.length - 1 ? "\n" : ""}
              </span>
            );
          })}
        </pre>
      ) : (
        <code className={`${className || ""} block bg-panel/80 border border-accent/20 rounded-md p-3 pr-10 overflow-x-auto font-mono text-[0.719rem] leading-[1.55] text-txt/90`} {...props}>
          {children}
        </code>
      )}
      <button
        onClick={handleCopy}
        className="absolute right-2 top-2 p-1 rounded bg-panel2/80 hover:bg-panel2 text-faint hover:text-muted border border-edge opacity-0 group-hover/code:opacity-100 transition-opacity"
        title="Copy code"
      >
        {copied ? <Check size={12} className="text-good" /> : <Copy size={12} />}
      </button>
    </div>
  );
}

// Route a clicked markdown link to the right surface instead of a raw
// new-window navigation: http(s) opens an in-app Browser tab, a file-ish path
// opens in the editor, and everything else is blocked (no javascript: in Electron).
function openMarkdownHref(href: string, e: React.MouseEvent): void {
  openAgentLink(href, e);
}

// Memoized so a streaming bubble only re-parses when the text actually changes.
// The typewriter re-renders the parent every animation frame; without this the
// full remark/rehype pipeline would run each frame even when no character was
// added. Restores formatted-while-streaming without the old ~40% CPU cost.
const Markdown = memo(function Markdown({ text }: { text: string }) {
  const linked = autolinkAgentText(text || "");
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeHighlight]}
      components={{
        h1: ({ children }: any) => <h1 className="text-sm font-semibold text-txt mt-3 mb-1.5 border-b border-edge pb-0.5">{children}</h1>,
        h2: ({ children }: any) => <h2 className="text-[0.8125rem] font-semibold text-txt mt-3 mb-1.5">{children}</h2>,
        h3: ({ children }: any) => <h3 className="text-[0.75rem] font-semibold text-muted mt-2 mb-1">{children}</h3>,
        p: ({ children }: any) => <p className="text-[0.8125rem] leading-[1.7] my-2 first:mt-0 last:mb-0">{children}</p>,
        strong: ({ children }: any) => <strong className="font-semibold text-txt">{children}</strong>,
        em: ({ children }: any) => <em className="italic text-txt/90">{children}</em>,
        ul: ({ children }: any) => <ul className="list-disc pl-4 my-2 space-y-1 text-txt/90">{children}</ul>,
        ol: ({ children }: any) => <ol className="list-decimal pl-4 my-2 space-y-1 text-txt/90">{children}</ol>,
        li: ({ children }: any) => <li className="text-[0.8125rem] leading-[1.65]">{children}</li>,
        blockquote: ({ children }: any) => (
          <blockquote className="border-l-2 border-edge pl-2.5 my-2 text-muted italic bg-panel2/30 rounded-r-sm py-1">
            {children}
          </blockquote>
        ),
        a: ({ href, children }: any) => (
          <a
            href={href}
            onClick={(e) => openMarkdownHref(href, e)}
            className="text-accent/90 no-underline hover:underline underline-offset-2 decoration-accent/40 cursor-pointer break-words"
          >
            {children}
          </a>
        ),
        img: ({ src, alt }: any) => (
          <img
            src={src}
            alt={alt || ""}
            loading="lazy"
            onClick={() => { if (src && isExternalUrl(src)) openAgentUrl(src); }}
            className="max-w-full h-auto rounded-md border border-edge/40 my-2 cursor-zoom-in"
          />
        ),
        table: ({ children }: any) => (
          <div className="overflow-x-auto my-1.5 border border-edge rounded bg-panel/40">
            <table className="min-w-full text-left text-[0.719rem] border-collapse">{children}</table>
          </div>
        ),
        thead: ({ children }: any) => (
          <thead className="bg-panel2/80 border-b border-edge font-semibold text-muted">{children}</thead>
        ),
        tbody: ({ children }: any) => (
          <tbody className="divide-y divide-edge/40">{children}</tbody>
        ),
        tr: ({ children }: any) => (
          <tr className="hover:bg-panel2/20 odd:bg-transparent even:bg-panel2/10">{children}</tr>
        ),
        th: ({ children }: any) => (
          <th className="px-2 py-1 border-r border-edge/30 last:border-r-0 font-semibold">{children}</th>
        ),
        td: ({ children }: any) => (
          <td className="px-2 py-1 border-r border-edge/30 last:border-r-0 text-txt/90">{children}</td>
        ),
        hr: () => <hr className="border-edge/60 my-2" />,
        code: ({ className, children, ...props }: any) => {
          const isInline = !className;
          if (isInline) {
            const raw = nodeToText(children).trim();
            if (looksLikePathInlineCode(raw)) {
              return (
                <button
                  type="button"
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    openAgentFile(raw);
                  }}
                  title={`Open ${raw}`}
                  className="bg-accent/[0.08] px-1 py-[1px] rounded text-[0.9em] font-mono text-accent/90 hover:underline underline-offset-2 cursor-pointer"
                >
                  {children}
                </button>
              );
            }
            return (
              <code className="bg-accent/[0.08] px-1 py-[1px] rounded text-[0.9em] font-mono text-accent/90" {...props}>
                {children}
              </code>
            );
          }
          return (
            <FencedCodeBlock className={className} {...props}>
              {children}
            </FencedCodeBlock>
          );
        },
        pre: ({ children }: any) => <div className="my-1">{children}</div>
      }}
    >
      {linked}
    </ReactMarkdown>
  );
});

function Bubble({
  msg,
  showLabel,
  isIntermediate,
  onExecutePlan,
  onEdit,
  isEditing,
  onRegenerate,
  onImageClick
}: {
  msg: Msg;
  showLabel?: boolean;
  isIntermediate?: boolean;
  onExecutePlan?: (text: string) => void;
  onEdit?: () => void;
  isEditing?: boolean;
  onRegenerate?: () => void;
  onImageClick?: (url: string) => void;
}) {
  const [executed, setExecuted] = useState(false);
  const [copied, setCopied] = useState(false);
  const isUser = msg.role === "user";
  const displayedText = isUser ? msg.text : cleanAssistantText(msg.text);

  // Cursor-style clamp: long SENT user messages collapse to a few lines with a
  // fade + "Show more", so a pasted wall of text doesn't dominate the transcript.
  const USER_CLAMP_PX = 160;
  const [userExpanded, setUserExpanded] = useState(false);
  const [userOverflowing, setUserOverflowing] = useState(false);
  const userClampRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (!isUser) return;
    const el = userClampRef.current;
    if (el) setUserOverflowing(el.scrollHeight > USER_CLAMP_PX + 4);
  }, [displayedText, isUser]);
  const userCollapsed = isUser && userOverflowing && !userExpanded;

  // Keep the ephemeral worker-stream window pinned to its latest tokens so it
  // reads as a live ticker rather than scrolling the whole page.
  const workerScrollRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    if (msg.workerStream && workerScrollRef.current) {
      workerScrollRef.current.scrollTop = workerScrollRef.current.scrollHeight;
    }
  }, [displayedText, msg.workerStream]);

  const handleCopy = () => {
    navigator.clipboard.writeText(displayedText);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };

  // A swarm worker's live token stream: a compact, height-capped, auto-scrolling
  // preview (fades older lines at the top) instead of an unbounded bubble. It is
  // ephemeral -- the finalizers drop it once the swarm's artifacts land.
  if (!isUser && msg.workerStream) {
    if (!displayedText.trim()) return null;
    return (
      <div className="flex flex-col items-start gap-0.5 my-1 w-full">
        <span className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-faint px-0.5 select-none font-mono">
          <Loader2 size={10} className="animate-spin text-faint/70" /> worker streaming
        </span>
        <div
          ref={workerScrollRef}
          className="w-full max-w-[95%] max-h-[7.5rem] overflow-y-auto overscroll-contain pl-2.5 border-l-2 border-edge/40 text-[10.5px] leading-[1.7] text-faint/70 whitespace-pre-wrap font-mono"
          style={{
            maskImage: "linear-gradient(to bottom, transparent 0%, black 24%, black 100%)",
            WebkitMaskImage: "linear-gradient(to bottom, transparent 0%, black 24%, black 100%)",
          }}
        >
          {displayedText}
        </div>
      </div>
    );
  }

  if (isUser) {
    return (
      <div className="flex flex-col items-end gap-0.5 my-1 w-full group relative">
        {showLabel && (
          <span className="text-[10px] uppercase tracking-wider text-faint px-1 select-none font-semibold mt-1">you</span>
        )}
        <div className="flex items-center gap-1.5 max-w-[85%] relative pr-1">
          {onEdit && (
            <button
              onClick={onEdit}
              className="p-1 rounded hover:bg-panel2 text-faint hover:text-muted opacity-0 group-hover:opacity-100 transition-opacity border border-transparent hover:border-edge absolute left-[-26px] top-1/2 -translate-y-1/2"
              title="Edit message"
            >
              <Pencil size={12} />
            </button>
          )}
          <div className={`rounded-xl px-3 py-1 text-[13px] leading-relaxed whitespace-pre-wrap break-words border transition-all ${
            isEditing
              ? "bg-accent/10 text-txt border-accent"
              : "bg-accent2 text-txt border-edge/30"
          }`}>
            <div className="relative">
              <div
                ref={userClampRef}
                className="overflow-hidden"
                style={userCollapsed ? { maxHeight: USER_CLAMP_PX } : undefined}
              >
                {displayedText}
              </div>
              {userCollapsed && (
                <div className={`pointer-events-none absolute inset-x-0 bottom-0 h-8 bg-gradient-to-t to-transparent ${isEditing ? "from-accent/10" : "from-accent2"}`} />
              )}
            </div>
            {isUser && userOverflowing && (
              <button
                type="button"
                onClick={() => setUserExpanded((v) => !v)}
                className="mt-1 flex items-center gap-0.5 text-[11px] text-muted/90 hover:text-txt transition-colors select-none"
              >
                {userExpanded
                  ? (<><ChevronUp size={12} /> Show less</>)
                  : (<><ChevronDown size={12} /> Show more</>)}
              </button>
            )}
            {msg.images && msg.images.length > 0 && (
              <div className="flex flex-wrap gap-2 mt-2">
                {msg.images.map((img, idx) => (
                  <TranscriptImage
                    key={`${img.path || img.name}-${idx}`}
                    path={img.path}
                    name={img.name}
                    previewUrl={img.previewUrl}
                    onImageClick={onImageClick}
                  />
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  // NOTE: intermediate narration (assistant prose followed by a tool card or
  // more prose in the same turn) MUST stay visible -- hiding it (the old
  // `return null`) is exactly what made streamed text vanish the moment a tool
  // fired. We keep the full text -> tool -> text -> tool thought chain on screen;
  // `isIntermediate` now only tones styling down slightly, never hides.
  const showExecuteButton = msg.isPlan && !executed && onExecutePlan;

  return (
    <div className={`flex flex-col items-start gap-0.5 my-1 w-full group relative${isIntermediate ? " pl-2 border-l border-edge/40" : ""}`}>
      {showLabel && (
        <span className="text-[10px] uppercase tracking-wider text-faint px-0.5 select-none font-semibold mt-1">pilot</span>
      )}
      <div className={`text-[0.8125rem] leading-[1.7] break-words max-w-[95%] py-0.5 w-full relative pr-14 ${isIntermediate ? "text-txt/75" : "text-txt/95"}`}>
        {/* Render Markdown even WHILE streaming so text types out formatted (code
            stays fenced, bold/lists render) instead of showing raw markdown that
            then reflows -- the "types out broken, then snaps" look. The <Markdown>
            component is memoized on its text, so a typewriter frame that adds no
            new characters does not re-parse; the earlier plain-text-while-streaming
            optimization traded polish for CPU, which read as unprofessional. */}
        <Markdown text={displayedText} />
        
        {/* Assistant copy & regenerate buttons */}
        <div className="absolute right-0 top-0.5 opacity-0 group-hover:opacity-100 transition-opacity flex items-center gap-1 select-none">
          {onRegenerate && (
            <button
              onClick={onRegenerate}
              className="p-1 rounded hover:bg-panel2 text-faint hover:text-muted transition border border-transparent hover:border-edge"
              title="Regenerate response"
            >
              <RefreshCw size={13} />
            </button>
          )}
          <button
            onClick={handleCopy}
            className="p-1 rounded hover:bg-panel2 text-faint hover:text-muted transition border border-transparent hover:border-edge"
            title="Copy message"
          >
            {copied ? <Check size={13} className="text-good" /> : <Copy size={13} />}
          </button>
        </div>

        {showExecuteButton && (
          <div className="mt-2 flex items-center gap-2">
            <button
              onClick={() => {
                setExecuted(true);
                onExecutePlan(msg.text);
              }}
              className="bg-accent text-black/90 rounded-md px-3 h-[26px] text-[12px] font-semibold hover:brightness-110 flex items-center gap-1.5 transition shadow-sm"
            >
              <Play size={11} fill="currentColor" />
              <span>Execute this plan</span>
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function ActionCard({ card, onToggle }: { card: Card; onToggle: () => void }) {
  const toolName = toolRowLabel(card.kind || "");
  // Prefer the real CLI input (path/command/query), recovering from nested
  // goals / artifact headlines when the stream left ``goal`` empty.
  const cliInput = resolveCardCliInput(card);
  const multiGoals = Array.isArray(card.goals) && card.goals.length > 1
    ? card.goals.map((g) => shortenGoal(g, 40)).join(" · ")
    : "";
  const rawGoal = multiGoals || cliInput;
  const goalPreview = shortenGoal(rawGoal, 56);
  const inputKey = toolInputFieldKey(card.kind || "");
  const meta = getCardMeta(card);
  const nested = Array.isArray(card.actions) ? card.actions : [];
  const effectivelyRunning = cardEffectivelyRunning(card);
  const nestedRunning =
    effectivelyRunning && nested.some((a) => a.status === "running");
  // Nested worker tools stay visible while running (open fold); terminal stays
  // collapsible with the parent card chrome.
  const showNested = nested.length > 0 && (card.open || nestedRunning || effectivelyRunning);

  // Hermes tool-row spec: monochrome. Success is SILENT (no glyph -- the row
  // reads as done without a checkmark); only running (spinner) and hard error
  // (destructive) carry a leading glyph. Gate suppressions are muted "blocked",
  // not red -- they are intentional harness redirects, not tool failures.
  const suppressed = isGateSuppressed(card);
  const isErr = !!card.result?.error && !suppressed;
  const { linkKind, value: goalValue } = classifyActionGoal(card.kind || "", rawGoal);

  const onGoalClick = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (linkKind === "file") openAgentFile(goalValue);
    else if (linkKind === "url") openAgentUrl(goalValue);
    else if (linkKind === "command") openAgentCommand(goalValue, { run: false });
  };

  const onRunCommand = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    openAgentCommand(goalValue, { run: true });
  };

  return (
    <div className="flex flex-col w-full select-none">
      <button
        onClick={onToggle}
        className="flex items-center justify-between w-full py-1 px-2 rounded-md hover:bg-panel2/40 text-left text-[11px] font-mono group transition-colors"
      >
        <div className="flex items-center gap-2 min-w-0 flex-1">
          <div className="flex items-center justify-center w-3.5 h-3.5 shrink-0">
            {effectivelyRunning ? (
              <Loader2 size={11} className="animate-spin text-faint/70" />
            ) : isErr ? (
              <span className="w-1.5 h-1.5 rounded-full bg-risk/70" />
            ) : suppressed ? (
              <span className="w-1.5 h-1.5 rounded-full bg-faint/50" />
            ) : null}
          </div>
          <span className={`font-medium shrink-0 ${isErr ? "text-risk/85" : suppressed ? "text-faint/80" : "text-txt/70"}`}>
            {toolName}
          </span>
          {goalPreview ? (
            linkKind !== "none" && goalValue ? (
              <span
                role="link"
                tabIndex={0}
                onClick={onGoalClick}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") onGoalClick(e as any);
                }}
                className="text-accent/80 hover:underline underline-offset-2 truncate max-w-[70%] font-normal cursor-pointer"
                title={
                  linkKind === "file"
                    ? `Open ${goalValue}`
                    : linkKind === "url"
                    ? "Open in browser"
                    : "Focus terminal"
                }
              >
                {goalPreview}
              </span>
            ) : (
              <span className="text-faint/85 truncate max-w-[70%] font-normal" title={rawGoal}>
                {goalPreview}
              </span>
            )
          ) : null}
        </div>

        <div className="flex items-center gap-2 shrink-0 text-[10px] text-faint/60 select-none tabular-nums">
          {linkKind === "command" && goalValue && (
            <span
              role="button"
              tabIndex={0}
              onClick={onRunCommand}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") onRunCommand(e as any);
              }}
              className="inline-flex items-center gap-0.5 px-1 py-0.5 rounded border border-edge/50 hover:bg-panel2/60 hover:text-txt cursor-pointer"
              title="Run in terminal"
            >
              <Play size={9} />
              Run
            </span>
          )}
          {meta && <span>{meta}</span>}
          <ChevronRight
            size={11}
            className={`text-faint/40 group-hover:text-faint/70 transition shrink-0 ${
              card.open ? "rotate-90" : ""
            }`}
          />
        </div>
      </button>

      {showNested && (
        <div className="mt-0.5 ml-5 pl-2 border-l border-edge/70 space-y-0.5">
          {nested.map((action) => {
            const nestedLabel = toolRowLabel(action.kind || "");
            const nestedGoal = shortenGoal(action.goal || "", 52);
            const nestedErr = Boolean(action.error) || action.status === "failed";
            return (
              <div
                key={action.action_id}
                className="flex items-center gap-2 py-0.5 px-1.5 text-[10px] font-mono text-faint/90"
                data-testid="nested-worker-action"
                data-action-id={action.action_id}
                data-status={action.status}
              >
                <div className="flex items-center justify-center w-3 h-3 shrink-0">
                  {action.status === "running" && effectivelyRunning ? (
                    <Loader2 size={10} className="animate-spin text-faint/70" />
                  ) : nestedErr ? (
                    <span className="w-1 h-1 rounded-full bg-risk/70" />
                  ) : null}
                </div>
                <span className={`shrink-0 ${nestedErr ? "text-risk/80" : "text-txt/65"}`}>
                  {nestedLabel}
                </span>
                {nestedGoal ? (
                  <span className="truncate text-faint/85" title={action.goal}>
                    {nestedGoal}
                  </span>
                ) : null}
                {typeof action.duration_ms === "number" && action.status !== "running" ? (
                  <span className="ml-auto tabular-nums text-faint/50 shrink-0">
                    {action.duration_ms < 1000
                      ? `${action.duration_ms}ms`
                      : `${(action.duration_ms / 1000).toFixed(1)}s`}
                  </span>
                ) : null}
              </div>
            );
          })}
        </div>
      )}

      {card.open && (
        <div className="mt-1 ml-5 pl-3 border-l border-edge py-1.5 pr-3 bg-panel2/40 rounded-r-md text-[11px] max-w-full text-txt/90 space-y-1">
          {/* Never render an empty key row — that was the "goal" with no value. */}
          {cliInput ? (
            <KV
              k={inputKey}
              v={cliInput}
              linkKind={classifyActionGoal(card.kind || "", cliInput).linkKind}
            />
          ) : null}
          {Array.isArray(card.goals) && card.goals.length > 1 && (
            <div className="space-y-0.5">
              {card.goals.map((g, i) => (
                <KV key={`goal-${i}`} k={`g${i + 1}`} v={g} />
              ))}
            </div>
          )}
          {card.cwd && <KV k="cwd" v={card.cwd} linkKind="file" />}
          {card.result?.error && (
            <div className={`mt-1 font-sans ${suppressed ? "text-faint/80" : "text-risk"}`}>
              {suppressed ? card.result.error : `error: ${card.result.error}`}
            </div>
          )}
          {card.result && !card.result.error && (
            <>
              {card.result.job_id && <KV k="job" v={card.result.job_id || ""} />}
              {/* Dispatch-only ack (backgrounded run_implement/run_parallel): show
                  its status/message; the rich artifact fields aren't present yet. */}
              {Array.isArray(card.result.types) ? (
                <KV k="found" v={`${card.result.num ?? 0} artifacts · ${card.result.types.join(", ")}`} />
              ) : (card.result.message || card.result.status) ? (
                <KV k="status" v={card.result.message || card.result.status || ""} />
              ) : null}
              {card.result.adapter === "demo" && <div className="text-warn text-[10px] mt-1 font-sans">demo substrate -- not real codebase analysis</div>}
              {(card.result.artifacts || []).map((a, i) => (
                <div key={i} className="flex gap-2 py-0.5 border-t border-edge/30 mt-1 items-center font-sans">
                  <span className="text-[9px] uppercase px-1.5 rounded bg-panel2 text-faint h-fit leading-none py-0.5 border border-edge/50">{a.type}</span>
                  <span className="text-txt/80 truncate">{a.headline}</span>
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}
const KV = ({ k, v, linkKind }: { k: string; v: string; linkKind?: "file" | "url" | "command" | "none" }) => {
  const clickable = linkKind === "file" || linkKind === "url" || linkKind === "command";
  return (
    <div className="flex gap-2 mb-0.5">
      <span className="text-muted w-14 shrink-0">{k}</span>
      {clickable && v ? (
        <button
          type="button"
          className="break-all text-left text-accent/85 hover:underline underline-offset-2"
          onClick={(e) => {
            e.stopPropagation();
            if (linkKind === "file") openAgentFile(v);
            else if (linkKind === "url") openAgentUrl(v);
            else openAgentCommand(v, { run: false });
          }}
        >
          {v}
        </button>
      ) : (
        <span className="break-all">{v}</span>
      )}
    </div>
  );
};

// A swarm outcome in the transcript. Previously this dumped the entire worker
// summary as full-width green/red monospace text -- a "wall" that read as noise
// on a finished run. Now it's a compact status line (icon + verb + objective +
// file count) that stays collapsed by default; the full summary, file chips,
// and any error live behind a click. Status color is confined to the icon,
// label, and border so the body text stays readable instead of tinted.
function SwarmResultCard({ applied, files, summary, error, objective }: {
  applied: boolean;
  files: string[];
  summary: string;
  error: string | null;
  objective?: string;
}) {
  const [open, setOpen] = useState(false);
  const obj = objective ? (objective.length > 70 ? objective.slice(0, 70) + "..." : objective) : "swarm";
  const hasBody = !!(summary || (!applied && error) || (applied && files.length > 0));

  return (
    <div className={`rounded-md border w-fit max-w-full my-1 overflow-hidden select-none bg-panel/40 ${applied ? "border-good/30" : "border-risk/30"}`}>
      <button
        onClick={() => hasBody && setOpen((v) => !v)}
        className={`flex items-center gap-2 px-2.5 py-1.5 text-[11px] w-full text-left transition-colors ${hasBody ? "hover:bg-panel2/40 cursor-pointer" : "cursor-default"}`}
        title={objective || undefined}
      >
        {applied
          ? <CheckCircle2 size={13} className="text-good shrink-0" />
          : <XCircle size={13} className="text-risk shrink-0" />}
        <span className={`font-medium shrink-0 ${applied ? "text-good" : "text-risk"}`}>
          {applied ? "swarm done" : "swarm failed"}
        </span>
        <span className="text-muted truncate">{obj}</span>
        <span className="flex-1 min-w-[8px]" />
        {applied
          ? (files.length > 0
            ? <span className="text-faint shrink-0 tabular-nums">{files.length} file{files.length === 1 ? "" : "s"}</span>
            : <span className="text-faint shrink-0 truncate max-w-[45%]">{summary}</span>)
          : <span className="text-risk/70 shrink-0 truncate max-w-[45%]">{error || "error"}</span>}
        {hasBody && (open
          ? <ChevronDown size={12} className="text-faint shrink-0" />
          : <ChevronRight size={12} className="text-faint shrink-0" />)}
      </button>

      {open && hasBody && (
        <div className="px-2.5 pb-2 pt-1.5 border-t border-edge/30 flex flex-col gap-1.5">
          {applied && files.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {files.map((f) => (
                <button
                  key={f}
                  type="button"
                  onClick={() => openAgentFile(f)}
                  className="text-[9px] font-mono text-accent/85 bg-panel2/60 border border-edge/50 rounded px-1 py-0.5 hover:underline underline-offset-2 cursor-pointer"
                  title={`Open ${f}`}
                >
                  {f}
                </button>
              ))}
            </div>
          )}
          {!applied && error && (
            <div className="text-[10px] text-risk/90 font-mono whitespace-pre-wrap leading-relaxed break-words">{error}</div>
          )}
          {summary && (
            <div className="text-[10.5px] text-muted whitespace-pre-wrap leading-relaxed break-words">{summary}</div>
          )}
        </div>
      )}
    </div>
  );
}
