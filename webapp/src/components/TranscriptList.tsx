import { useEffect, useLayoutEffect, useRef, useState, useCallback, useDeferredValue, useSyncExternalStore, memo } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ChevronRight, Loader2, ChevronDown, ChevronUp, Play, Copy, Check, Pencil, RefreshCw, History, Share2, CheckCircle2, XCircle, Eye } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import "highlight.js/styles/github-dark.css";
import {
  openAgentLink,
  openAgentFile,
  openAgentUrl,
  openAgentCommand,
  openAgentImage,
  openAgentWorkspace,
  openAgentSwarmJob,
  openAgentSpill,
  syncAgentCommandOutput,
  isExternalUrl,
  looksLikeJobId,
  looksLikeSpillUri,
  looksLikePathInlineCode,
  classifyActionGoal,
  classifyTranscriptHref,
  autolinkAgentText,
  type AgentLinkKind,
} from "../lib/agentLinks";
import {
  getAgentCommandIndexVersion,
  lookupAgentCommandSession,
  registerAgentCommandSession,
  subscribeAgentCommandIndex,
} from "../lib/agentCommandIndex";
import {
  pathTokenInCodeLine,
  tokenizeClickableOutput,
} from "../lib/clickableOutput";
import { splitStreamingMarkdown } from "../lib/streamMarkdown";
import {
  aggregateExplorationSummary,
  cardEffectivelyRunning,
  cardHasDurableJob,
  deriveBusyProgress,
  investigatingHeadline,
  resolveCardCliInput,
  shortenGoal,
  quietWorkingCueVisible,
  shouldShowBusyFooter,
  toolFocusPhrase,
  toolInputFieldKey,
  toolRowLabel,
  turnHasVisibleBusySurface,
} from "../lib/turnProgress";
import { isAgentLoopOpen } from "./conversation/runnersBusy";
import {
  autoHaltPresentation,
  autoStatusPresentation,
  commandApprovalStatusCopy,
  commandBlockedPresentation,
  qualityGatePresentation,
  verificationReceiptPresentation,
  type AutoBudgetSnapshot,
} from "../lib/autoReceipts";
import { TranscriptImage } from "./conversation/TranscriptImage";
import {
  FEED_UNPIN_BUBBLE_EVENT,
  nextFeedPinState,
  shouldStopNestedWheelBubble,
  shouldUnpinInnerOnWheel,
  THINKING_INNER_PIN_THRESHOLD_PX,
} from "./conversation/feedScroll";
import {
  isOccludedScrollParentSize,
  shouldUseVirtualTranscriptWindow,
} from "./conversation/transcriptVirtualWindow";
import {
  compactionKeptDroppedLine,
  compactionSuccessLabel,
  focusReviewTabAndRefresh,
  vaultCiteChipLabel,
} from "./conversation/streamApply";

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
  /** Owning swarm/local worker id — keys parallel workerStream previews apart. */
  worker_id?: string;
  /** Provider output-item identity (Codex/Sol dual-channel streams). */
  stream_id?: string;
  /** Visible channel: progress (commentary) vs answer (final_answer). */
  channel?: "progress" | "answer" | string;
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
             status?: string; message?: string; duration_ms?: number;
             /** Truncated shell stdout/stderr excerpt for run_command cards. */
             output?: string;
             exit_code?: number | string;
             command?: string;
             /** spill:// when stdout exceeded the inline capture budget. */
             spill_uri?: string;
             /** True when full output was spilled off the wire. */
             output_spilled?: boolean;
             /** Full spilled output length in characters (when known). */
             output_chars?: number;
             /** Bounded preview when spilled or truncated. */
             output_preview?: string };
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
  | { kind: "thinking"; text: string; streaming?: boolean; id?: string; stream_id?: string }
  | { kind: "tool_prep"; name: string }
  | SwarmPendingItem
  | { kind: "swarm_result"; job_id: string; applied: boolean; files: string[]; summary: string; error: string | null; objective?: string; held_for_review?: boolean; analysis_ok?: boolean; reuse_status?: string; source_job_id?: string; reuse_reason?: string; invalidated_paths?: string[]; validation_fingerprint?: string; environment_fingerprint?: string; acceptance_criteria?: string[] }
  | { kind: "checkpoint"; id: string; label: string; trigger: string }
  | { kind: "pending_review"; id: string; summary: string }
  | {
      kind: "compaction";
      before_tokens: number;
      after_tokens: number;
      aborted?: boolean;
      reason?: string;
      message?: string;
      mode?: "extractive" | "llm";
      kept?: string[];
      dropped?: string[];
      handles?: string[];
      story?: string[];
    }
  | { kind: "codegraph_context"; symbols: number; query: string }
  | { kind: "vault_cite"; route: string; snippets: string[]; query?: string }
  | { kind: "command_blocked"; command: string; category: string; reason: string; matched: string }
  | CommandApprovalItem
  | { kind: "auto_status"; cycle: number; snapshot: AutoBudgetSnapshot }
  | { kind: "auto_halt"; reason: string; snapshot: AutoBudgetSnapshot }
  | { kind: "auth_failure"; message: string; id?: string }
  | { kind: "steer"; text: string; mode?: "steer" | "interrupt" }
  | {
      kind: "quality_gate";
      outcome: string;
      passed: boolean;
      cmd?: string;
      attempts?: number;
      block_finish?: boolean;
      output?: string;
    }
  | { kind: "verifying"; cmd?: string; auto?: boolean }
  | {
      kind: "auto_verify";
      passed: boolean;
      command?: string;
      output_excerpt?: string;
    }
  | { kind: "verification"; passed: boolean; output?: string; cmd?: string };

export type GroupedItem =
  | { kind: "msg"; msg: Msg }
  | { kind: "thinking"; text: string; streaming?: boolean; id?: string; stream_id?: string }
  | SwarmPendingItem
  | { kind: "swarm_result"; job_id: string; applied: boolean; files: string[]; summary: string; error: string | null; objective?: string; held_for_review?: boolean; analysis_ok?: boolean; reuse_status?: string; source_job_id?: string; reuse_reason?: string; invalidated_paths?: string[]; validation_fingerprint?: string; environment_fingerprint?: string; acceptance_criteria?: string[] }
  | { kind: "checkpoint"; id: string; label: string; trigger: string }
  | { kind: "pending_review"; id: string; summary: string }
  | {
      kind: "compaction";
      before_tokens: number;
      after_tokens: number;
      aborted?: boolean;
      reason?: string;
      message?: string;
      mode?: "extractive" | "llm";
      kept?: string[];
      dropped?: string[];
      handles?: string[];
      story?: string[];
    }
  | { kind: "codegraph_context"; symbols: number; query: string }
  | { kind: "vault_cite"; route: string; snippets: string[]; query?: string }
  | { kind: "command_blocked"; command: string; category: string; reason: string; matched: string }
  | CommandApprovalItem
  | { kind: "auto_status"; cycle: number; snapshot: AutoBudgetSnapshot }
  | { kind: "auto_halt"; reason: string; snapshot: AutoBudgetSnapshot }
  | { kind: "auth_failure"; message: string; id?: string }
  | { kind: "steer"; text: string; mode?: "steer" | "interrupt" }
  | {
      kind: "quality_gate";
      outcome: string;
      passed: boolean;
      cmd?: string;
      attempts?: number;
      block_finish?: boolean;
      output?: string;
    }
  | { kind: "verifying"; cmd?: string; auto?: boolean }
  | {
      kind: "auto_verify";
      passed: boolean;
      command?: string;
      output_excerpt?: string;
    }
  | { kind: "verification"; passed: boolean; output?: string; cmd?: string }
  | { kind: "activity_group"; items: ActivityItem[] };

type ActivityItem =
  | { kind: "card"; card: Card }
  | { kind: "thinking"; text: string; streaming?: boolean; id?: string; stream_id?: string }
  | { kind: "codegraph_context"; symbols: number; query: string }
  | { kind: "vault_cite"; route: string; snippets: string[]; query?: string }
  | { kind: "checkpoint"; id: string; label: string; trigger: string }
  | SwarmPendingItem
  | { kind: "swarm_result"; job_id: string; applied: boolean; files: string[]; summary: string; error: string | null; objective?: string; held_for_review?: boolean; analysis_ok?: boolean; reuse_status?: string; source_job_id?: string; reuse_reason?: string; invalidated_paths?: string[]; validation_fingerprint?: string; environment_fingerprint?: string; acceptance_criteria?: string[] }
  | { kind: "msg"; msg: Msg }
  | Extract<
      Item,
      | { kind: "compaction" }
      | { kind: "command_blocked" }
      | { kind: "auto_status" }
      | { kind: "auto_halt" }
      | { kind: "quality_gate" }
      | { kind: "verifying" }
      | { kind: "auto_verify" }
      | { kind: "verification" }
    >;

/** Receipts that belong inside the activity strip, not beside the sentence. */
function isActivityTelemetry(
  item: ActivityItem | Item,
): item is Extract<
  Item,
  | { kind: "compaction" }
  | { kind: "command_blocked" }
  | { kind: "auto_status" }
  | { kind: "auto_halt" }
  | { kind: "quality_gate" }
  | { kind: "verifying" }
  | { kind: "auto_verify" }
  | { kind: "verification" }
> {
  return (
    item.kind === "compaction"
    || item.kind === "command_blocked"
    || item.kind === "auto_status"
    || item.kind === "auto_halt"
    || item.kind === "quality_gate"
    || item.kind === "verifying"
    || item.kind === "auto_verify"
    || item.kind === "verification"
  );
}

function compactionRowChrome(it: Extract<Item, { kind: "compaction" }>): {
  label: string;
  title: string;
} {
  const tokens = `${it.before_tokens} → ${it.after_tokens}${it.mode ? ` · ${it.mode}` : ""}`;
  if (it.aborted) {
    const label = it.message
      || (it.reason ? `Compaction aborted (${it.reason})` : "Compaction aborted");
    return { label, title: it.reason ? `${tokens} · ${it.reason}` : tokens };
  }
  const counts = compactionKeptDroppedLine(it.kept, it.dropped);
  return {
    label: compactionSuccessLabel(),
    title: counts ? `${tokens} · ${counts}` : tokens,
  };
}

function CompactionReceipt({
  it,
  fold,
}: {
  it: Extract<Item, { kind: "compaction" }>;
  fold?: boolean;
}) {
  const [openHandle, setOpenHandle] = useState<string | null>(null);
  const chrome = compactionRowChrome(it);
  const counts = !it.aborted ? compactionKeptDroppedLine(it.kept, it.dropped) : undefined;
  const handles = it.handles || [];
  const story = it.story || [];
  const pillClass = fold
    ? "flex items-center gap-1.5 py-0.5 text-[10px] text-faint/80 select-none font-mono"
    : `flex items-center gap-1.5 py-1 px-3 rounded-full w-fit select-none font-mono text-[10.5px] ${
        it.aborted
          ? "bg-amber-500/10 border border-amber-500/25 text-amber-200/90"
          : "bg-panel2/10 border border-edge/10 text-faint"
      }`;
  return (
    <div className={fold ? undefined : "flex flex-col gap-0.5 my-1"}>
      <div
        role={fold ? undefined : "status"}
        title={chrome.title}
        className={pillClass}
      >
        <span>{chrome.label}</span>
      </div>
      {!fold && counts ? (
        <div className="text-[10.5px] text-faint font-mono px-3">{counts}</div>
      ) : null}
      {handles.length > 0 && !fold ? (
        <div className="flex flex-wrap gap-1 px-3">
          {handles.map((handle) => (
            <button
              type="button"
              key={handle}
              title={handle}
              onClick={() => setOpenHandle((cur) => (cur === handle ? null : handle))}
              className="text-[10.5px] text-faint font-mono bg-transparent border-0 p-0 cursor-pointer hover:underline underline-offset-2"
            >
              {handle}
            </button>
          ))}
        </div>
      ) : null}
      {handles.length > 0 && fold ? (
        <span className="text-[10px] text-faint/70 font-mono" title={handles.join(" · ")}>
          {handles.length} handle{handles.length === 1 ? "" : "s"}
        </span>
      ) : null}
      {openHandle && !fold ? (
        <div className="text-[10.5px] text-faint font-mono px-3 whitespace-pre-wrap break-all">
          <div>{openHandle}</div>
          {story.map((line) => (
            <div key={line}>{line}</div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function VaultCiteChip({
  it,
  fold,
}: {
  it: Extract<Item, { kind: "vault_cite" }>;
  fold?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const first = (it.snippets[0] || "").trim();
  const snippet = first.length > 72 ? `${first.slice(0, 70)}…` : first;
  const title = [it.query, ...it.snippets].filter(Boolean).join("\n");
  return (
    <button
      type="button"
      title={title}
      onClick={() => setOpen((v) => !v)}
      className={
        fold
          ? "flex items-center gap-1.5 py-0.5 text-[10px] text-faint/70 select-none bg-transparent border-0 p-0 cursor-pointer text-left"
          : "flex items-center gap-1.5 py-0.5 text-[10px] text-accent/70 w-fit my-0.5 select-none bg-transparent border-0 p-0 cursor-pointer text-left"
      }
    >
      <History size={9} className={fold ? "text-faint/60" : "text-accent/70"} />
      <span>
        {vaultCiteChipLabel()}
        {snippet ? ` -- ${snippet}` : ""}
      </span>
      {open && it.snippets.length > 1 ? (
        <span className="block text-[10px] text-faint font-mono whitespace-pre-wrap">
          {it.snippets.slice(1).join("\n")}
        </span>
      ) : null}
    </button>
  );
}


/**
 * Assistants that belong inside the investigation fold for this turn.
 *
 * Open-loop absorption (fold mid-turn narration once the turn has tools /
 * thinking) applies ONLY to the current turn — the span after the last user
 * message. Prior turns always use the sealed rule: fold only assistants that
 * still have a later tool card. Without that scope, a live turn would re-fold
 * every historical finale into its Explored group, then peel those finales
 * back out on seal (row remount churn / flicker in the virtualized feed).
 *
 * Current turn while open: fold streaming + sealed narration once there is
 * investigation activity (or while streaming before the first tool).
 * Current turn when closed / any prior turn: trailing final stands alone.
 */
function isPlanOrProgressAssistant(msg: Msg): boolean {
  return Boolean(msg.isPlan) || msg.channel === "progress";
}

function turnHasInvestigationActivity(items: Item[], turnStart: number): boolean {
  return items.slice(turnStart).some(
    (row) =>
      row.kind === "card"
      || row.kind === "thinking"
      || row.kind === "swarm_result"
      || row.kind === "swarm_pending",
  );
}

function laterInvestigationActivity(items: Item[], fromIdx: number): {
  laterCardOrSwarm: boolean;
  laterThinking: boolean;
  laterAssistant: boolean;
} {
  let laterCardOrSwarm = false;
  let laterThinking = false;
  let laterAssistant = false;
  for (let j = fromIdx + 1; j < items.length; j++) {
    const later = items[j];
    if (later.kind === "msg" && later.msg.role === "user") break;
    if (later.kind === "msg" && later.msg.role === "assistant") {
      laterAssistant = true;
    }
    if (
      later.kind === "card"
      || later.kind === "swarm_result"
      || later.kind === "swarm_pending"
    ) {
      laterCardOrSwarm = true;
    }
    if (later.kind === "thinking") {
      laterThinking = true;
    }
  }
  return { laterCardOrSwarm, laterThinking, laterAssistant };
}

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
    // workerStream assistants fold into Investigating with other mid-turn
    // narration. ActivityGroup renders them via Bubble's capped ticker (not
    // muted <pre>) when the fold is open — never force-open for them.

    const seenCardBefore = items
      .slice(turnStart, i)
      .some((row) => row.kind === "card");
    const foldActivity = turnHasInvestigationActivity(items, turnStart);

    // Open-loop absorption is current-turn only (see docstring).
    const openAbsorb = agentLoopOpen && i >= currentTurnStart;
    if (openAbsorb) {
      // Mid-turn: keep streaming + sealed narration inside the fold from the
      // first token once the turn is investigative, and while streaming even
      // before the first tool (avoids outside→absorb blink). Pure chat still
      // peels to a standalone finale when the loop closes (no card after).
      if (foldActivity || item.msg.streaming === true) {
        intermediateItems.add(item);
      }
      continue;
    }

    const later = laterInvestigationActivity(items, i);

    if (!seenCardBefore) {
      // Sealed pre-tool sticky outside — except explicit plan/progress
      // narration, which folds into the investigation when tools/swarm/
      // thinking follow (Cursor-like chrome; final answers stay standalone).
      if (
        isPlanOrProgressAssistant(item.msg)
        && (later.laterCardOrSwarm || later.laterThinking)
      ) {
        intermediateItems.add(item);
      }
      continue;
    }
    // Sealed / prior turns: fold mid-turn narration when investigation still
    // continues after it. A later tool/swarm_result always counts. Later
    // thinking alone is not enough (Cursor late-reasoning after a true finale
    // must not bury the answer inside Explored) — but thinking PLUS a later
    // assistant means planning→Thought→answer, so the planning line folds.
    if (later.laterCardOrSwarm || (later.laterThinking && later.laterAssistant)) {
      intermediateItems.add(item);
    }
  }
  return intermediateItems;
}

export function groupAgentActivity(items: Item[], intermediateItems: Set<Item>): GroupedItem[] {
  // The feed is a conversation, not an event log. Top-level rows are
  // msg / steer, a question (command_approval), a file (pending_review),
  // a loud auth failure, and one activity strip. Compaction, gates, verify,
  // and auto receipts fold into that strip. Surfaces do not reclassify
  // after first paint.
  const grouped: GroupedItem[] = [];
  let currentGroup: ActivityItem[] = [];
  let terminalSwarmItems: ActivityItem[] = [];
  const resultJobIds = new Set(
    items
      .filter((item): item is Extract<Item, { kind: "swarm_result" }> => item.kind === "swarm_result")
      .map((item) => item.job_id),
  );

  const flush = () => {
    const activityItems = [...currentGroup, ...terminalSwarmItems];
    if (activityItems.length > 0) {
      grouped.push({ kind: "activity_group", items: activityItems });
      currentGroup = [];
      terminalSwarmItems = [];
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
    } else if (item.kind === "swarm_result") {
      // These are emitted by tool execution, so they belong inside the same
      // collapsed investigation as the action card that produced them. Rendering
      // them as standalone chips made the transcript vertically noisy.
      // Keep terminal detail after any later tool rows in this investigation.
      terminalSwarmItems.push(item);
    } else if (item.kind === "checkpoint") {
      currentGroup.push(item);
    } else if (item.kind === "pending_review") {
      // Operator receipt for DiffReview hold — keep top-level so it is not
      // buried inside a collapsed investigation fold.
      flush();
      grouped.push(item);
    } else if (item.kind === "swarm_pending") {
      const status = item.status || (item.resolved ? "done" : "running");
      if (status === "running") {
        // Keep the live swarm pill inside the current Investigating fold with
        // surrounding tool cards / reasoning — do not flush it top-level.
        currentGroup.push(item);
        continue;
      }
      const uncoveredJobIds = (item.job_ids || []).filter((jobId) => !resultJobIds.has(jobId));
      if (uncoveredJobIds.length === 0) continue;
      // Terminal lifecycle metadata is part of the investigation, but is
      // deferred so it cannot interrupt the chronological tool chain.
      terminalSwarmItems.push(
        uncoveredJobIds.length === item.job_ids.length
          ? item
          : { ...item, job_ids: uncoveredJobIds },
      );
    } else if (isActivityTelemetry(item)) {
      currentGroup.push(item);
    } else if (
      item.kind === "command_approval"
      || item.kind === "auth_failure"
      || item.kind === "steer"
    ) {
      flush();
      grouped.push(item);
    } else if (item.kind === "card" || item.kind === "thinking" || item.kind === "codegraph_context" || item.kind === "vault_cite") {
      // Cards, reasoning, codegraph/vault chips: all collect into the one box.
      currentGroup.push(item);
    }
  }

  flush();
  return grouped;
}

/** Fingerprint for exact-duplicate failed run_swarm / swarm_result routing chrome.
 *  Shared across paired ActionCard + terminal swarm_result for the same
 *  error/objective so one retry lifecycle collapses to a single chrome row. */
function failedRoutingFingerprint(it: ActivityItem): string | null {
  if (it.kind === "swarm_result") {
    if (it.applied || it.held_for_review || it.analysis_ok) return null;
    const err = String(it.error || "").trim();
    if (!err) return null;
    return `${err}\n${String(it.objective || "").trim()}`;
  }
  if (it.kind === "card") {
    const kind = String(it.card.kind || "").toLowerCase();
    if (kind !== "run_swarm" && kind !== "run_parallel") return null;
    const err = String(it.card.result?.error || "").trim();
    if (!err || it.card.running) return null;
    return `${err}\n${String(it.card.goal || "").trim()}`;
  }
  return null;
}

/**
 * Collapse exact-duplicate failed run_swarm routing cards / swarm_result rows
 * inside one investigation fold. Distinct failures stay visible; successes
 * are never merged.
 */
export function collapseDuplicateFailedRoutingItems(
  items: ActivityItem[],
): { items: ActivityItem[]; duplicateCounts: number[] } {
  const out: ActivityItem[] = [];
  const duplicateCounts: number[] = [];
  const indexByFingerprint = new Map<string, number>();

  for (const it of items) {
    const fp = failedRoutingFingerprint(it);
    if (!fp) {
      out.push(it);
      duplicateCounts.push(1);
      continue;
    }
    const existing = indexByFingerprint.get(fp);
    if (existing === undefined) {
      indexByFingerprint.set(fp, out.length);
      out.push(it);
      duplicateCounts.push(1);
    } else {
      duplicateCounts[existing] += 1;
      // Prefer terminal swarm_result chrome over the paired ActionCard so the
      // shared failure body stays visible without expanding a tool row.
      if (it.kind === "swarm_result" && out[existing].kind === "card") {
        out[existing] = it;
      }
    }
  }
  return { items: out, duplicateCounts };
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
// Session-scoped only — clearActivityFoldPrefs() on session switch so stable
// ids cannot leak open/closed prefs across conversations.
const __activityOpen = new Map<string, boolean>();
// Reasoning expand preference (user click) survives remounts / live→idle flips.
const __thinkingExpanded = new Map<string, boolean>();
// Alias every durable member of an investigation onto one canon key so a
// thinking-only group does not remount when the first tool card arrives (and
// the reverse). Streaming used to key off objKey(thinking) which changed every
// token and remounted the fold -- expand clicked shut, inner scroll stuck at top.
const __activityGroupCanon = new Map<string, string>();

/**
 * Drop module-global fold prefs when the active session changes. Stable card /
 * thinking ids can collide across sessions; leaking prefs made session B open
 * with session A's expand state.
 */
export function clearActivityFoldPrefs(): void {
  __activityOpen.clear();
  __thinkingExpanded.clear();
  __activityGroupCanon.clear();
}

/**
 * Investigation folds default CLOSED (Cursor/Hermes). Only an explicit user
 * toggle (sticky in ``prefs``) opens them — never live tools/reasoning.
 */
export function resolveActivityGroupOpen(
  groupId: string,
  prefs: Map<string, boolean> = __activityOpen,
): boolean {
  if (prefs.has(groupId)) return Boolean(prefs.get(groupId));
  return false;
}

/**
 * REASONING / thinking rows default CLOSED. Live streaming must not auto-expand;
 * the user opens individual blocks when they want the body.
 */
export function resolveThinkingExpanded(
  blockId: string,
  prefs: Map<string, boolean> = __thinkingExpanded,
): boolean {
  if (prefs.has(blockId)) return Boolean(prefs.get(blockId));
  return false;
}

/**
 * Index of the current-turn investigation fold in a grouped transcript, or -1
 * when the latest user message has no activity_group yet.
 *
 * Live/spinning chrome must fence on this — never "last group in the whole
 * transcript" — so submitting a new prompt cannot reopen a prior Explored fold.
 */
export function liveActivityGroupIndex(grouped: GroupedItem[]): number {
  let lastUserIdx = -1;
  for (let i = 0; i < grouped.length; i++) {
    const g = grouped[i];
    if (g.kind === "msg" && g.msg.role === "user") lastUserIdx = i;
  }
  for (let i = grouped.length - 1; i > lastUserIdx; i--) {
    if (grouped[i].kind === "activity_group") return i;
  }
  return -1;
}

/**
 * Investigating chrome is live-fold only. A prior fold may still hold a
 * stale ``running`` card or leftover swarm_pending after a steer flush
 * splits the turn — those must not keep a second Investigating spinner.
 */
export function activityFoldInvestigating(opts: {
  isLiveFold: boolean;
  anyRunning: boolean;
  liveThinking: boolean;
  pausePoint: boolean;
  swarmPendingRunning: boolean;
  loopOpen: boolean;
  hasFoldContent: boolean;
}): boolean {
  if (!opts.isLiveFold) return false;
  return (
    opts.anyRunning
    || opts.liveThinking
    || (
      !opts.pausePoint
      && (
        opts.swarmPendingRunning
        || (opts.loopOpen && opts.hasFoldContent)
      )
    )
  );
}

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
    case "pending_review":
      return `prev-${it.id}`;
    case "compaction":
      return `cmp-${it.aborted ? "abort" : "ok"}-${it.before_tokens}-${it.after_tokens}-${it.reason || it.mode || i}`;
    case "codegraph_context":
      return `cg-${i}-${it.symbols}`;
    case "vault_cite":
      return `vault-${i}-${it.route}`;
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
    case "quality_gate":
      return `qg-${i}-${it.outcome}-${it.passed ? "ok" : "fail"}`;
    case "verifying":
      return `verifying-${i}-${it.auto ? "auto" : "manual"}`;
    case "auto_verify":
      return `auto-verify-${i}-${it.passed ? "ok" : "fail"}`;
    case "verification":
      return `verification-${i}-${it.passed ? "ok" : "fail"}`;
    case "thinking":
      return it.id ? `think-${it.id}` : `think-${i}`;
    default:
      return `item-${i}`;
  }
}

// PERF: Long sessions grow the transcript without bound, and every displayed
// group is an expensive subtree (markdown + syntax highlight + tool cards).
// Virtualize the muted four-surface feed (msg / question / file / activity fold)
// with @tanstack/react-virtual + measureElement so only the viewport (plus
// overscan) stays mounted. Parent stick-to-bottom (nextFeedPinState hysteresis
// in Conversation) still owns pin/unpin — this list only reports real total
// height via the spacer; it never fakes a 40-row render cap.
const FEED_ROW_ESTIMATE_PX = 72;
const FEED_VIRTUAL_OVERSCAN = 8;

/** Bind run_command cards even when Investigating is collapsed (Hermes procId). */
function indexCardCommandSession(card: Card): void {
  const cliInput = resolveCardCliInput(card);
  const resultCommand = String(card.result?.command || "").trim();
  const inputKey = toolInputFieldKey(card.kind || "");
  const commandKv = inputKey === "command" && resultCommand ? resultCommand : cliInput;
  const rawGoal = commandKv || cliInput;
  const { linkKind, value } = classifyActionGoal(card.kind || "", rawGoal);
  const id = String(card.id || card.result?.job_id || "").trim();
  if (linkKind !== "command" || !id || !value) return;
  registerAgentCommandSession({
    id,
    command: value,
    output: String(card.result?.output || ""),
  });
}

function indexTranscriptCommandSessions(items: Item[]): void {
  for (const it of items) {
    if (it.kind === "card") indexCardCommandSession(it.card);
  }
}

// PERF: Memoized transcript renderer. Its props are intentionally free of the
// composer `input` (or any per-keystroke state), so React.memo lets typing skip
// re-rendering the whole transcript. Only transcript-affecting state (items,
// status, compactingStatus, editingIndex, auto, plan) plus stable callbacks are
// passed in; all callbacks are useCallback-stabilized in the parent so the memo
// comparison holds.
export type TranscriptListProps = {
  items: Item[];
  status: "idle" | "thinking" | "executing" | "done" | "error" | "streaming" | "awaiting_swarm";
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
  /**
   * Conversation's pending-job hold — OR'd into agentLoopOpen so fold /
   * absorption / footer match StatusPill through idle flaps / switch rearm.
   */
  holdSwarmAwait?: boolean;
  scrollContainerRef: React.RefObject<HTMLDivElement | null>;
  /** Conversation jump-to-latest / stick-to-bottom: virtualizer-aware end scroll. */
  scrollToEndRef?: React.MutableRefObject<(() => void) | null>;
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
  holdSwarmAwait = false,
  scrollContainerRef,
  scrollToEndRef,
  onEditMessage,
  onExecuteSend,
  onImageClick,
  onSetCard,
  onExecutePlan,
  onCommandApproval,
}: TranscriptListProps) {
  // Match Conversation's latch — awaiting_swarm plus holdSwarmAwait so
  // Investigating / mid-turn absorption / footer stay armed through idle flaps.
  useEffect(() => {
    indexTranscriptCommandSessions(items);
  }, [items]);

  const agentLoopOpen = isAgentLoopOpen(turnOpen, status) || holdSwarmAwait;
  // Pause-point: StatusPill prefers Still working… — seal sticky Investigating
  // and keep the busy footer visible instead of hiding under fold chrome.
  // holdSwarmAwait alone must not seal mid-turn: only when the pilot is idle.
  const pilotBusy =
    turnOpen
    || status === "thinking"
    || status === "executing"
    || status === "streaming";
  const pausePoint =
    status === "awaiting_swarm" || (holdSwarmAwait && !pilotBusy);

  const intermediateItems = collectIntermediateAssistantItems(items, agentLoopOpen);
  const grouped = groupAgentActivity(items, intermediateItems);

  // Virtualizer scroll parent is the feed column (composer is a sibling). The
  // list sits below empty-state / padding, so scrollMargin tracks that offset.
  // scrollEpoch re-renders once feedRef attaches / resizes so getScrollElement
  // is observed (refs alone do not trigger React updates).
  const listAnchorRef = useRef<HTMLDivElement>(null);
  const virtualizedOnceRef = useRef(false);
  const [scrollMargin, setScrollMargin] = useState(0);
  const [scrollEpoch, setScrollEpoch] = useState(0);
  useLayoutEffect(() => {
    const scrollEl = scrollContainerRef.current;
    if (!scrollEl) return;
    setScrollEpoch((n) => n + 1);
    const onResize = () => {
      // Alt-tab / occlusion often reports 0x0. Bumping epoch then would
      // remount the unvirtualized list and snap the feed to the top.
      if (isOccludedScrollParentSize(scrollEl.clientHeight, scrollEl.offsetHeight)) {
        return;
      }
      setScrollEpoch((n) => n + 1);
    };
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(onResize) : null;
    ro?.observe(scrollEl);
    return () => ro?.disconnect();
  }, [scrollContainerRef]);
  useLayoutEffect(() => {
    const scrollEl = scrollContainerRef.current;
    const anchor = listAnchorRef.current;
    if (!scrollEl || !anchor) return;
    const syncMargin = () => {
      const next =
        anchor.getBoundingClientRect().top -
        scrollEl.getBoundingClientRect().top +
        scrollEl.scrollTop;
      setScrollMargin((prev) => (Math.abs(prev - next) > 0.5 ? next : prev));
    };
    syncMargin();
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(syncMargin) : null;
    ro?.observe(scrollEl);
    ro?.observe(anchor);
    return () => ro?.disconnect();
  }, [scrollContainerRef, grouped.length, scrollEpoch]);

  const rowVirtualizer = useVirtualizer({
    count: grouped.length,
    getScrollElement: () => scrollContainerRef.current,
    estimateSize: () => FEED_ROW_ESTIMATE_PX,
    overscan: FEED_VIRTUAL_OVERSCAN,
    scrollMargin,
    getItemKey: (index) => stableItemKey(grouped[index]!, index),
    measureElement:
      typeof window !== "undefined" &&
      !/firefox/i.test(window.navigator.userAgent)
        ? (element) => element.getBoundingClientRect().height
        : undefined,
  });
  const scrollToEnd = useCallback(() => {
    const last = grouped.length - 1;
    if (last >= 0) {
      rowVirtualizer.scrollToIndex(last, { align: "end" });
    }
    const el = scrollContainerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [grouped.length, rowVirtualizer, scrollContainerRef]);
  useLayoutEffect(() => {
    if (!scrollToEndRef) return;
    scrollToEndRef.current = scrollToEnd;
    return () => {
      if (scrollToEndRef.current === scrollToEnd) {
        scrollToEndRef.current = null;
      }
    };
  }, [scrollToEnd, scrollToEndRef]);
  void scrollEpoch;

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

  // Only the newest fold AFTER the latest user message may be live. Prior
  // turns stay sealed even while turnOpen/busy — otherwise a new prompt
  // reactivates the previous Investigating pill until turn-2 tools land.
  const lastActivityGroupIdx = liveActivityGroupIndex(grouped);

  const renderGroupedItem = (i: number) => {
    const it = grouped[i];
    if (!it) return null;
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
      return (
        <SwarmPendingPill
          key={key}
          jobIds={it.job_ids || []}
          objective={it.objective || ""}
          status={it.status || (it.resolved ? "done" : "running")}
        />
      );
    } else if (it.kind === "swarm_result") {
      return (
        <SwarmResultCard
          key={key}
          jobId={it.job_id}
          applied={it.applied}
          files={it.files}
          summary={it.summary}
          error={it.error}
          objective={it.objective}
          heldForReview={it.held_for_review}
          analysisOk={it.analysis_ok}
          reuseStatus={it.reuse_status}
          sourceJobId={it.source_job_id}
          reuseReason={it.reuse_reason}
          invalidatedPaths={it.invalidated_paths}
        />
      );
    } else if (it.kind === "checkpoint") {
      return (
        <div key={key} className="flex items-center gap-1.5 py-1 px-3 rounded-full bg-panel2/15 border border-edge/20 text-[10px] text-faint w-fit my-1 select-none">
          <History size={11} className="text-accent" />
          <span>restore point created: {it.label} ({it.id.slice(0, 8)})</span>
        </div>
      );
    } else if (it.kind === "pending_review") {
      return (
        <button
          type="button"
          key={key}
          data-testid="pending-review-receipt"
          onClick={() => focusReviewTabAndRefresh()}
          title="Open Review tab"
          className="flex items-center gap-1.5 py-1 px-3 rounded-full bg-accent/10 border border-accent/25 text-[10px] text-accent w-fit my-1 select-none cursor-pointer hover:bg-accent/15 transition-colors"
        >
          <Eye size={11} className="text-accent shrink-0" />
          <span>review ready: {it.summary} ({it.id.slice(0, 12)})</span>
        </button>
      );
    } else if (it.kind === "codegraph_context") {
      return (
        <div key={key} className="flex items-center gap-1.5 py-0.5 text-[10px] text-accent/70 w-fit my-0.5 select-none" title={it.query ? `CodeGraph consulted for: ${it.query}` : "CodeGraph consulted"}>
          <Share2 size={9} className="text-accent/70" />
          <span>CodeGraph consulted{it.symbols > 0 ? ` -- ${it.symbols} symbols` : ""}</span>
        </div>
      );
    } else if (it.kind === "vault_cite") {
      return <VaultCiteChip key={key} it={it} />;
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
            {it.command ? (
              <button
                type="button"
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  openAgentCommand(it.command, { id: it.command, run: false });
                }}
                title="Reveal command"
                className="block mt-0.5 max-w-full text-left text-[10px] text-accent/80 hover:underline underline-offset-2 font-mono truncate bg-transparent border-0 p-0 cursor-pointer"
              >
                {it.command}
              </button>
            ) : null}
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
              <button
                type="button"
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  openAgentCommand(it.command, { id: it.command, run: false });
                }}
                title="Reveal command"
                className="mt-2 block w-full max-h-28 overflow-auto rounded border border-edge bg-panel/60 p-2 font-mono text-[10.5px] text-accent/85 hover:underline underline-offset-2 whitespace-pre-wrap break-all select-text text-left cursor-pointer"
              >
                {it.command}
              </button>
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
      return <CompactionReceipt key={key} it={it} />;
    } else if (it.kind === "steer") {
      return (
        <div key={key} className="flex items-center gap-1.5 py-1 px-3 rounded-full bg-panel2/15 border border-edge/20 text-[10.5px] text-faint w-fit my-1 select-none font-mono animate-in fade-in duration-200">
          <span className="text-muted">{it.mode === "interrupt" ? "interrupt:" : "steer:"}</span>
          <span>{it.text}</span>
        </div>
      );
    } else if (it.kind === "quality_gate") {
      const gate = qualityGatePresentation(it);
      const toneClass =
        gate.tone === "good"
          ? "bg-panel2/15 border-edge/20 text-muted"
          : gate.tone === "risk"
            ? "bg-risk/10 border-risk/30 text-risk/90"
            : gate.tone === "warn"
              ? "bg-amber-500/10 border-amber-500/25 text-amber-200/90"
              : "bg-panel2/10 border-edge/15 text-faint";
      const labelClass =
        gate.tone === "good"
          ? "text-good/80"
          : gate.tone === "risk"
            ? "text-risk/90"
            : gate.tone === "warn"
              ? "text-amber-200/90"
              : "text-muted";
      return (
        <div
          key={key}
          role="status"
          title={it.output ? it.output.slice(0, 400) : gate.label}
          className={`flex items-center gap-1.5 py-1 px-3 rounded-full border text-[10.5px] w-fit my-1 select-none font-mono ${toneClass}`}
        >
          <span className={labelClass}>{gate.label}</span>
          {gate.detail ? <span className="text-faint">· {gate.detail}</span> : null}
        </div>
      );
    } else if (it.kind === "verifying" || it.kind === "auto_verify" || it.kind === "verification") {
      const receipt = verificationReceiptPresentation(
        it.kind === "verifying"
          ? { kind: "verifying", cmd: it.cmd, auto: it.auto }
          : it.kind === "auto_verify"
            ? { kind: "auto_verify", passed: it.passed, command: it.command }
            : { kind: "verification", passed: it.passed, cmd: it.cmd },
      );
      const toneClass =
        receipt.tone === "good"
          ? "bg-panel2/15 border-edge/20 text-muted"
          : receipt.tone === "risk"
            ? "bg-risk/10 border-risk/30 text-risk/90"
            : receipt.tone === "busy"
              ? "bg-panel2/15 border-edge/20 text-faint animate-pulse"
              : "bg-panel2/10 border-edge/15 text-faint";
      const labelClass =
        receipt.tone === "good"
          ? "text-good/80"
          : receipt.tone === "risk"
            ? "text-risk/90"
            : "text-muted";
      const excerpt =
        it.kind === "auto_verify"
          ? it.output_excerpt
          : it.kind === "verification"
            ? it.output
            : undefined;
      return (
        <div
          key={key}
          role="status"
          title={excerpt ? excerpt.slice(0, 400) : receipt.label}
          className={`flex items-center gap-1.5 py-1 px-3 rounded-full border text-[10.5px] w-fit my-1 select-none font-mono ${toneClass}`}
        >
          <span className={labelClass}>{receipt.label}</span>
          {receipt.detail ? <span className="text-faint">· {receipt.detail}</span> : null}
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
          isLiveFold={i === lastActivityGroupIdx}
          loopOpen={agentLoopOpen && i === lastActivityGroupIdx}
          pausePoint={pausePoint && i === lastActivityGroupIdx}
          onToggleCard={(card) => onSetCard(card.id, { open: !card.open })}
        />
      );
    }
    return null;
  };

  const virtualItems = rowVirtualizer.getVirtualItems();
  // jsdom / pre-layout: scroll parent missing or unsized → mount full flow so
  // presentation tests keep working without faking a 40-row window.
  // Once sized, stay virtual even if alt-tab reports a 0-height parent —
  // flipping back remounts every bubble and snaps the feed to the top.
  const scrollParentSized = Boolean(
    scrollContainerRef.current &&
      !isOccludedScrollParentSize(
        scrollContainerRef.current.clientHeight,
        scrollContainerRef.current.offsetHeight,
      ),
  );
  if (scrollParentSized) virtualizedOnceRef.current = true;
  const useVirtualWindow = shouldUseVirtualTranscriptWindow({
    scrollParentSized,
    alreadyVirtualized: virtualizedOnceRef.current,
  });
  const list = useVirtualWindow ? (
    <div
      ref={listAnchorRef}
      data-testid="transcript-virtual-list"
      className="relative w-full"
      style={{ height: rowVirtualizer.getTotalSize() }}
    >
      {virtualItems.map((virtualRow) => (
        <div
          key={virtualRow.key}
          data-index={virtualRow.index}
          ref={rowVirtualizer.measureElement}
          data-testid="transcript-virtual-row"
          className="absolute top-0 left-0 w-full pb-1"
          style={{
            transform: `translateY(${virtualRow.start - scrollMargin}px)`,
          }}
        >
          {renderGroupedItem(virtualRow.index)}
        </div>
      ))}
    </div>
  ) : (
    <div ref={listAnchorRef} data-testid="transcript-virtual-list" className="flex flex-col gap-1 w-full">
      {grouped.map((_, i) => renderGroupedItem(i))}
    </div>
  );

  const busyProgress = deriveBusyProgress(items, status, busyElapsedMs);
  // Hide the under-fold line only while a card / prep / stream already
  // shows work. A sticky Investigating fold of *finished* tools used to
  // own chrome and swallow "Still working…" between Kimi-style batches.
  const hideBusyFooter = turnHasVisibleBusySurface(items);
  const showBusyFooter =
    (shouldShowBusyFooter(items, status) || pausePoint) && !hideBusyFooter;
  const showStall = quietWorkingCueVisible(
    items,
    status,
    Boolean(compactingStatus),
    showBusyFooter,
    agentLoopOpen,
  );

  return (
    <>
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
            {busyProgress.label || "Still working…"}
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
      <div
        data-testid="feed-bottom-clearance"
        aria-hidden
        className="feed-bottom-clearance shrink-0 w-full pointer-events-none"
        style={{ height: "clamp(72px, 12vh, 144px)" }}
      />
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
  } else if (typeof card.result?.exit_code === "number") {
    parts.push(`exit ${card.result.exit_code}`);
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
        const exitMatch = headline.match(
          /(?:^exit\s+(-?\d+)\b|Command exited with (-?\d+))/i,
        );
        if (exitMatch) {
          parts.push(`exit ${exitMatch[1] || exitMatch[2]}`);
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
  pausePoint = false,
  isLiveFold = false,
}: {
  items: ActivityItem[];
  onToggleCard: (card: Card) => void;
  groupId: string;
  /** True while this is the current turn's fold and the agent loop is still open. */
  loopOpen?: boolean;
  /**
   * awaiting_swarm / holdSwarmAwait pause — StatusPill says Still working…;
   * do not keep a sticky Investigating spinner over settled tools.
   */
  pausePoint?: boolean;
  /** True only for the live-index fold. Prior folds never show Investigating. */
  isLiveFold?: boolean;
}) {
  // Investigation chrome stays collapsed by default (Cursor/Hermes). The
  // headline still tracks Investigating / Explored while closed; the user
  // opens the fold when they want the step list. Seed from the module map so
  // a remount does not yank an explicit toggle mid-stream.
  const swarmPendingItems = items.filter((it) => it.kind === "swarm_pending");
  const swarmPendingRunning = swarmPendingItems.some((it) => {
    const status = it.status || (it.resolved ? "done" : "running");
    return status === "running";
  });

  const [open, setOpen] = useState(() => resolveActivityGroupOpen(groupId));
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
  // A running swarm_pending is itself live investigation chrome.
  // Pause-point (awaiting_swarm / hold): seal sticky Investigating so the fold
  // shows Explored while StatusPill / busy footer own Still working….
  // Prior folds (steer-flushed leftover running / swarm cards) stay Explored.
  const durableJobRunning = cards.some(
    (c) => cardHasDurableJob(c.card) && cardEffectivelyRunning(c.card),
  );
  const investigating = activityFoldInvestigating({
    isLiveFold,
    anyRunning,
    liveThinking,
    pausePoint,
    swarmPendingRunning,
    loopOpen,
    hasFoldContent: actionCount > 0 || thinkingItems.length > 0 || swarmPendingItems.length > 0,
  });

  // A group with NO tool actions, no narration AND no reasoning (just a lone
  // CodeGraph chip from the per-step auto-injection) would render a misleading
  // "0 steps" box -- suppress it. But folded intermediate narration OR a reasoning
  // trace must still show (collapsed), so reasoning never silently vanishes from
  // the step list the way it used to.
  const telemetryItems = items.filter(isActivityTelemetry);
  if (actionCount === 0 && narrationMsgs.length === 0 && thinkingItems.length === 0 && checkpointItems.length === 0 && swarmResults.length === 0 && swarmPendingItems.length === 0 && telemetryItems.length === 0) {
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

  const { items: displayItems, duplicateCounts } = collapseDuplicateFailedRoutingItems(items);

  const renderInner = (it: (typeof displayItems)[number], idx: number) => {
    const dupCount = duplicateCounts[idx] || 1;
    if (it.kind === "card") {
      return (
        <ActionCard
          key={it.card.id || `card-${idx}`}
          card={it.card}
          onToggle={() => onToggleCard(it.card)}
          duplicateCount={dupCount}
          activityGroupOpen={open}
        />
      );
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
      // Folded plan/progress / micro-narration stays ordinary regular text —
      // Markdown emphasis (`**Plan:**`) must not reappear as bold after expand.
      // workerStream keeps Bubble's capped live ticker (not muted <pre>).
      if (!it.msg.text || !it.msg.text.trim()) return null;
      if (it.msg.workerStream) {
        return (
          <Bubble
            key={objKey(it.msg)}
            msg={it.msg}
            isIntermediate
          />
        );
      }
      return (
        <div key={objKey(it.msg)} className="text-[12px] text-muted/90 py-0.5 leading-relaxed">
          <pre className="whitespace-pre-wrap font-sans font-normal text-[12px] leading-relaxed text-muted/90 m-0">
            {normalizePlainTextNarration(it.msg.text)}
          </pre>
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
    if (it.kind === "vault_cite") {
      return <VaultCiteChip key={`vault-${idx}-${it.route}`} it={it} fold />;
    }
    if (it.kind === "checkpoint") {
      return (
        <div key={`ckpt-${it.id}`} className="flex items-center gap-1.5 py-0.5 text-[10px] text-faint/80 select-none">
          <History size={10} className="text-faint/70" />
          <span>restore point created: {it.label} ({it.id.slice(0, 8)})</span>
        </div>
      );
    }
    // pending_review stays top-level in groupAgentActivity (not folded here).
    if (it.kind === "swarm_result") {
      return (
        <SwarmResultCard
          key={`swres-${it.job_id}`}
          jobId={it.job_id}
          applied={it.applied}
          files={it.files}
          summary={it.summary}
          error={it.error}
          objective={it.objective}
          heldForReview={it.held_for_review}
          analysisOk={it.analysis_ok}
          reuseStatus={it.reuse_status}
          sourceJobId={it.source_job_id}
          reuseReason={it.reuse_reason}
          invalidatedPaths={it.invalidated_paths}
          duplicateCount={dupCount}
        />
      );
    }
    if (it.kind === "swarm_pending") {
      return (
        <SwarmPendingPill
          key={`swarm-pending-${(it.job_ids || []).join(",")}-${idx}`}
          jobIds={it.job_ids || []}
          objective={it.objective || ""}
          status={it.status || (it.resolved ? "done" : "running")}
        />
      );
    }
    if (it.kind === "compaction") {
      // Tokens are hover, not a peer of the sentence. Hide the row entirely
      // when the strip already has tools — the fold title carries the hover.
      if (actionCount > 0 && !it.aborted) return null;
      return <CompactionReceipt key={`compact-${idx}`} it={it} fold />;
    }
    if (it.kind === "command_blocked") {
      const blocked = commandBlockedPresentation(it);
      return (
        <div key={`blocked-${idx}`} className="flex items-center gap-1.5 py-0.5 text-[10px] text-faint/80 select-none">
          <span>{blocked.label}{blocked.detail ? ` · ${blocked.detail}` : ""}</span>
        </div>
      );
    }
    if (it.kind === "auto_status") {
      const status = autoStatusPresentation(it.cycle, it.snapshot);
      return (
        <div key={`auto-status-${idx}`} className="flex items-center gap-1.5 py-0.5 text-[10px] text-faint/80 select-none font-mono">
          <span>{status.label}{status.detail ? ` · ${status.detail}` : ""}</span>
        </div>
      );
    }
    if (it.kind === "auto_halt") {
      const halt = autoHaltPresentation(it.reason, it.snapshot);
      return (
        <div key={`auto-halt-${idx}`} className="flex items-center gap-1.5 py-0.5 text-[10px] text-faint/80 select-none font-mono">
          <span>{halt.label} · {halt.detail}</span>
        </div>
      );
    }
    if (it.kind === "quality_gate") {
      const gate = qualityGatePresentation(it);
      return (
        <div key={`gate-${idx}`} className="flex items-center gap-1.5 py-0.5 text-[10px] text-faint/80 select-none font-mono" title={it.output ? it.output.slice(0, 400) : gate.label}>
          <span>{gate.label}{gate.detail ? ` · ${gate.detail}` : ""}</span>
        </div>
      );
    }
    if (it.kind === "verifying" || it.kind === "auto_verify" || it.kind === "verification") {
      const receipt = verificationReceiptPresentation(
        it.kind === "verifying"
          ? { kind: "verifying", cmd: it.cmd, auto: it.auto }
          : it.kind === "auto_verify"
            ? { kind: "auto_verify", passed: it.passed, command: it.command }
            : { kind: "verification", passed: it.passed, cmd: it.cmd },
      );
      return (
        <div key={`verify-${idx}`} className="flex items-center gap-1.5 py-0.5 text-[10px] text-faint/80 select-none font-mono">
          <span>{receipt.label}{receipt.detail ? ` · ${receipt.detail}` : ""}</span>
        </div>
      );
    }
    return null;
  };

  // Always use the Investigating / Explored collapsible — even for tiny
  // tool-only groups. Inline always-open rows used to burn scroll space and
  // disagreed with Cursor/Hermes (collapsed until the user opens them).

  const quietSummary = (() => {
    if (!investigating && !isLiveFold && durableJobRunning) return "job still running";
    if (actionCount > 0) return stepHeadline;
    if (swarmResults.length > 0) {
      return `Swarm · ${swarmResults.length} result${swarmResults.length === 1 ? "" : "s"}`;
    }
    if (swarmPendingItems.length > 0) {
      if (!isLiveFold) {
        return `Swarm · ${swarmPendingItems.length} pending`;
      }
      return swarmPendingRunning ? "Swarm · running" : `Swarm · ${swarmPendingItems.length} pending`;
    }
    if (investigating) return stepHeadline || "Investigating…";
    if (telemetryItems.length > 0 && actionCount === 0 && thinkingItems.length === 0) {
      const first = telemetryItems[0];
      if (first.kind === "compaction") {
        return first.aborted ? "Compaction aborted" : compactionSuccessLabel();
      }
      if (first.kind === "quality_gate") return qualityGatePresentation(first).label;
      if (first.kind === "auto_halt") return autoHaltPresentation(first.reason, first.snapshot).label;
      if (first.kind === "verifying" || first.kind === "auto_verify" || first.kind === "verification") {
        return verificationReceiptPresentation(
          first.kind === "verifying"
            ? { kind: "verifying", cmd: first.cmd, auto: first.auto }
            : first.kind === "auto_verify"
              ? { kind: "auto_verify", passed: first.passed, command: first.command }
              : { kind: "verification", passed: first.passed, cmd: first.cmd },
        ).label;
      }
      if (first.kind === "command_blocked") return commandBlockedPresentation(first).label;
      if (first.kind === "auto_status") return autoStatusPresentation(first.cycle, first.snapshot).label;
    }
    const preview = normalizeReasoningPreview(narrationPreview, 72);
    return preview || "Thought";
  })();
  const compactionHover = (() => {
    const compact = telemetryItems.find((row) => row.kind === "compaction");
    return compact && compact.kind === "compaction" ? compactionRowChrome(compact).title : "";
  })();
  const foldTitle = [investigating ? (runningCard?.goal || quietSummary) : quietSummary, compactionHover]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className="my-1 w-full">
      <button
        type="button"
        onClick={toggleOpen}
        aria-expanded={open}
        className="flex items-center gap-1.5 py-0.5 text-[12px] font-sans font-normal text-faint/75 hover:text-muted transition w-fit max-w-full select-none"
      >
        {open ? <ChevronDown size={11} className="text-faint/55 shrink-0" /> : <ChevronRight size={11} className="text-faint/55 shrink-0" />}
        {investigating ? <Loader2 size={11} className="animate-spin text-faint/60 shrink-0" /> : null}
        <span
          className="truncate max-w-[52ch] normal-case"
          title={foldTitle}
        >
          {quietSummary}
        </span>
        {cgItems.length > 0 && (
          <span className="ml-0.5 text-[10px] text-faint/40">+ CodeGraph</span>
        )}
        {checkpointItems.length > 0 && (
          <span className="ml-0.5 text-[10px] text-faint/40">+ {checkpointItems.length} restore point{checkpointItems.length === 1 ? "" : "s"}</span>
        )}
        {swarmResults.length > 0 && actionCount > 0 && (
          <span className="ml-0.5 text-[10px] text-faint/40">+ swarm</span>
        )}
      </button>
      {open && (
        <div className="flex flex-col gap-0.5 pl-3 mt-1 border-l border-edge/30 w-full">
          {displayItems.map(renderInner)}
        </div>
      )}
    </div>
  );
}


/**
 * Quiet collapsed-reasoning preview: first line only, strip markdown emphasis
 * markers so `**Plan**` / `*italic*` never leak into the Cursor-like row.
 */
export function normalizeReasoningPreview(text: string, maxLen = 160): string {
  const first = String(text || "").trim().split("\n", 1)[0] || "";
  // Strip Markdown chrome for collapsed previews — keep ordinary * math/globs
  // (2*3*4, a*b*c) and snake_case identifiers intact.
  const cleaned = first
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/~~([^~]+)~~/g, "$1")
    // Left-flanking single *emphasis* only (space/start before opener).
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, "$1$2")
    .replace(/\*\*|__/g, "")
    .replace(/^#{1,6}\s+/, "")
    .replace(/\s+/g, " ")
    .trim();
  if (cleaned.length <= maxLen) return cleaned;
  return cleaned.slice(0, maxLen).trimEnd();
}

/**
 * Shared plain-text path for expanded Thought bodies and folded plan/progress
 * narration. Removes Markdown presentation markers while preserving line breaks
 * and ordinary text (including math/glob asterisks like 2*3*4).
 */
export function normalizePlainTextNarration(text: string): string {
  const raw = String(text || "").replace(/\r\n/g, "\n");
  return raw
    .split("\n")
    .map((line) => {
      let s = line;
      s = s.replace(/!\[([^\]]*)\]\([^)]+\)/g, "$1");
      s = s.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
      s = s.replace(/`([^`]+)`/g, "$1");
      s = s.replace(/\*\*([^*]+)\*\*/g, "$1");
      s = s.replace(/__([^_]+)__/g, "$1");
      s = s.replace(/~~([^~]+)~~/g, "$1");
      s = s.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, "$1$2");
      s = s.replace(/\*\*|__/g, "");
      s = s.replace(/^#{1,6}\s+/, "");
      return s.replace(/[ \t]+$/g, "");
    })
    .join("\n")
    .replace(/^\n+/, "")
    .replace(/\n+$/, "");
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
  // Cursor/Hermes-style compression: reasoning stays a single header line
  // by default (faint first-line preview). Expand is user-driven and sticky;
  // live streaming must not auto-open the body. Expanded bodies strip Markdown
  // chrome (no strong/headings for **Plan:**) then autolink paths/URLs so
  // clicks open the right surface. Inner scroll stick-to-bottom follows new
  // tokens only while the user stays pinned near the bottom of an expanded box.
  const [expanded, setExpanded] = useState(() => resolveThinkingExpanded(blockId));
  const bodyRef = useRef<HTMLDivElement>(null);
  const pinnedInnerRef = useRef(true);
  const innerReleasedByGestureRef = useRef(false);
  const prevInnerScrollTopRef = useRef<number | null>(null);

  const notifyOuterFeedUnpin = useCallback(() => {
    bodyRef.current?.dispatchEvent(
      new CustomEvent(FEED_UNPIN_BUBBLE_EVENT, { bubbles: true }),
    );
  }, []);

  const syncInnerPinFromScroll = useCallback(() => {
    const el = bodyRef.current;
    if (!el) return;
    const wasPinned = pinnedInnerRef.current;
    const next = nextFeedPinState({
      wasPinned,
      releasedByGesture: innerReleasedByGestureRef.current,
      scrollHeight: el.scrollHeight,
      scrollTop: el.scrollTop,
      clientHeight: el.clientHeight,
      prevScrollTop: prevInnerScrollTopRef.current,
      settling: false,
      repinPx: THINKING_INNER_PIN_THRESHOLD_PX,
    });
    if (wasPinned && !next.pinned) {
      notifyOuterFeedUnpin();
    }
    pinnedInnerRef.current = next.pinned;
    innerReleasedByGestureRef.current = next.releasedByGesture;
    prevInnerScrollTopRef.current = el.scrollTop;
  }, [notifyOuterFeedUnpin]);

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

  const preview = normalizeReasoningPreview(text);

  return (
    <div className="flex flex-col w-full py-0.5 min-w-0">
      <button
        type="button"
        onClick={() => {
          setExpanded((v) => {
            const next = !v;
            __thinkingExpanded.set(blockId, next);
            return next;
          });
        }}
        className="flex items-center gap-1.5 text-faint/65 hover:text-muted/90 transition font-sans font-normal text-[12px] text-left w-full min-w-0 select-none"
        aria-expanded={expanded}
        title={expanded ? "Collapse reasoning" : "Expand reasoning"}
      >
        {expanded ? <ChevronDown size={11} className="text-faint/55 shrink-0" /> : <ChevronRight size={11} className="text-faint/55 shrink-0" />}
        <span className="shrink-0">{live ? "Thinking…" : "Thought"}</span>
        {!expanded && preview ? (
          <span className="ml-0.5 truncate text-faint/50">{preview}</span>
        ) : null}
      </button>
      {expanded && (
        <div
          ref={bodyRef}
          onScroll={syncInnerPinFromScroll}
          onWheel={(e) => {
            // Keep wheel deltas inside this capped pane so the outer transcript
            // does not steal scroll while the user reads a long live thought.
            const el = bodyRef.current;
            if (!el) return;
            const atTop = el.scrollTop <= 0;
            const atBottom =
              el.scrollHeight - el.scrollTop - el.clientHeight <= 1;
            if (shouldStopNestedWheelBubble(e.deltaY, atTop, atBottom)) {
              e.stopPropagation();
            }
            if (shouldUnpinInnerOnWheel(e.deltaY)) {
              innerReleasedByGestureRef.current = true;
              if (pinnedInnerRef.current) {
                pinnedInnerRef.current = false;
                notifyOuterFeedUnpin();
              }
            }
          }}
          className="mt-0.5 pl-2.5 ml-1 border-l-2 border-edge/40 overflow-y-auto overscroll-contain text-faint/85 text-[11px] leading-[1.65] max-w-[92%] max-h-[34dvh] [&_p]:my-1 [&_p]:text-[11px] [&_p]:leading-[1.65] [&_p]:text-faint/85"
        >
          <Markdown text={normalizePlainTextNarration(text)} />
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

function lookupLiveCommand(command: string, indexVersion: number) {
  // indexVersion is the useSyncExternalStore snapshot. Naming it here keeps
  // React Compiler from treating the module-Map lookup as a pure function of
  // `command` alone (first paint is always miss; cards register in useEffect).
  return indexVersion >= 0 ? lookupAgentCommandSession(command) : null;
}

function FencedCodeBlock({ className, children, commandIndexVersion = 0, ...props }: any) {
  const [copied, setCopied] = useState(false);
  const codeText = nodeToText(children).replace(/\n$/, "");
  const lines = codeText.split("\n");
  const pathLines = lines.filter((ln) => pathTokenInCodeLine(ln)).length;
  // Directory trees / file lists: make paths open in the editor on click.
  const clickableTree = pathLines >= 2 || (lines.length <= 4 && pathLines >= 1);
  const liveCommand = !clickableTree && lines.length === 1
    ? lookupLiveCommand(codeText, commandIndexVersion)
    : null;

  const handleCopy = () => {
    navigator.clipboard.writeText(codeText);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };

  const revealLiveCommand = (e: React.MouseEvent) => {
    if (!liveCommand) return;
    e.preventDefault();
    e.stopPropagation();
    openAgentCommand(liveCommand.command, {
      id: liveCommand.id,
      output: liveCommand.output,
      run: false,
    });
  };

  return (
    <div className="relative group/code my-2">
      {liveCommand ? (
        <button
          type="button"
          onClick={revealLiveCommand}
          title="Reveal running command"
          className={`${className || ""} block w-full text-left bg-panel/80 border border-accent/20 rounded-md p-3 pr-10 overflow-x-auto font-mono text-[0.719rem] leading-[1.55] text-accent/90 hover:underline underline-offset-2 cursor-pointer m-0 whitespace-pre`}
        >
          {children}
        </button>
      ) : clickableTree ? (
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

// Pretty tree only. Streaming wrappers pass a deferred `flushed` string so
// highlight.js never remounts on a fence the next token can still extend.
const PrettyMarkdown = memo(function PrettyMarkdown({ text }: { text: string }) {
  const commandIndexVersion = useSyncExternalStore(
    subscribeAgentCommandIndex,
    getAgentCommandIndexVersion,
    getAgentCommandIndexVersion,
  );
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
        a: ({ href, children }: any) => {
          const kind = classifyTranscriptHref(href || "");
          if (kind === "none") {
            return <span>{children}</span>;
          }
          return (
            <a
              href={href}
              onClick={(e) => openMarkdownHref(href, e)}
              className="text-accent/90 no-underline hover:underline underline-offset-2 decoration-accent/40 cursor-pointer break-words"
            >
              {children}
            </a>
          );
        },
        img: ({ src, alt }: any) => (
          <img
            src={src}
            alt={alt || ""}
            loading="lazy"
            onClick={() => { if (src) openAgentImage(src); }}
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
            if (isExternalUrl(raw)) {
              return (
                <button
                  type="button"
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    openAgentUrl(raw);
                  }}
                  title="Open in browser"
                  className="bg-accent/[0.08] px-1 py-[1px] rounded text-[0.9em] font-mono text-accent/90 hover:underline underline-offset-2 cursor-pointer"
                >
                  {children}
                </button>
              );
            }
            const liveCommand = lookupLiveCommand(raw, commandIndexVersion);
            if (liveCommand) {
              return (
                <button
                  type="button"
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    openAgentCommand(liveCommand.command, {
                      id: liveCommand.id,
                      output: liveCommand.output,
                      run: false,
                    });
                  }}
                  title="Reveal running command"
                  className="bg-accent/[0.08] px-1 py-[1px] rounded text-[0.9em] font-mono text-accent/90 hover:underline underline-offset-2 cursor-pointer"
                >
                  {children}
                </button>
              );
            }
            return (
              <code className="bg-panel2/60 px-1 py-[1px] rounded text-[0.9em] font-mono text-txt/90" {...props}>
                {children}
              </code>
            );
          }
          return (
            <FencedCodeBlock
              className={className}
              commandIndexVersion={commandIndexVersion}
              {...props}
            >
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

function StreamingMarkdown({ text }: { text: string }) {
  const buf = splitStreamingMarkdown(text || "");
  const deferredFlushed = useDeferredValue(buf.flushed);
  // Never paint flushed-as-markdown plus a sibling lag <span>. That remounts
  // the trailing sentence as <p> then <span> then <p> again — the blink.
  if (buf.open) {
    return (
      <>
        {buf.flushed ? <PrettyMarkdown text={deferredFlushed} /> : null}
        <pre
          data-md-pending
          data-lang={buf.open.lang || undefined}
          className="block bg-panel/80 border border-accent/20 rounded-md p-3 overflow-x-auto font-mono text-[0.719rem] leading-[1.55] text-txt/90 my-2 whitespace-pre"
        >
          {buf.open.body + buf.hold}
        </pre>
      </>
    );
  }
  return (
    <>
      <PrettyMarkdown text={buf.flushed} />
      {buf.hold ? (
        <span data-md-hold className="font-mono">{buf.hold}</span>
      ) : null}
    </>
  );
}

// Memoized so a streaming bubble only re-parses when the text actually changes.
// The typewriter re-renders the parent every animation frame; without this the
// full remark/rehype pipeline would run each frame even when no character was
// added. Restores formatted-while-streaming without the old ~40% CPU cost.
const Markdown = memo(function Markdown({
  text,
  streaming = false,
}: {
  text: string;
  streaming?: boolean;
}) {
  if (streaming) return <StreamingMarkdown text={text} />;
  return <PrettyMarkdown text={text} />;
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
        {/* Plan/progress stays ordinary text; final answers keep Markdown
            so code fences / lists render for the user-facing reply. */}
        {isPlanOrProgressAssistant(msg) ? (
          <pre className="whitespace-pre-wrap font-sans font-normal text-[0.8125rem] leading-[1.7] m-0">
            {normalizePlainTextNarration(displayedText)}
          </pre>
        ) : (
          <Markdown text={displayedText} streaming={Boolean(msg.streaming)} />
        )}
        
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

function ActionCard({
  card,
  onToggle,
  duplicateCount = 1,
  activityGroupOpen = false,
}: {
  card: Card;
  onToggle: () => void;
  /** Exact-duplicate failed routing collapses within one investigation. */
  duplicateCount?: number;
  /**
   * True when this card is painted inside an open Investigating / Explored
   * fold. Nested worker rows must be visible then — counts already include
   * them, so a second forced-closed layer would lie about the timeline.
   */
  activityGroupOpen?: boolean;
}) {
  const toolName = toolRowLabel(card.kind || "");
  // Prefer the real CLI input (path/command/query), recovering from nested
  // goals / artifact headlines when the stream left ``goal`` empty.
  const cliInput = resolveCardCliInput(card);
  const inputKey = toolInputFieldKey(card.kind || "");
  const resultCommand = String(card.result?.command || "").trim();
  const commandKv =
    inputKey === "command" && resultCommand ? resultCommand : cliInput;
  const multiGoals = Array.isArray(card.goals) && card.goals.length > 1
    ? card.goals.map((g) => shortenGoal(g, 40)).join(" · ")
    : "";
  const rawGoal = multiGoals || commandKv || cliInput;
  const goalPreview = shortenGoal(rawGoal, 56);
  const meta = getCardMeta(card);
  const nested = Array.isArray(card.actions) ? card.actions : [];
  const effectivelyRunning = cardEffectivelyRunning(card);
  // One expand level: open ActivityGroup OR expanded parent card reveals
  // nested worker tools as a flat chronological list (not double-collapsed).
  const showNested = nested.length > 0 && (card.open || activityGroupOpen);
  const resultOutput = String(card.result?.output || card.result?.output_preview || "");
  const spillUri = String(card.result?.spill_uri || "").trim();
  const outputSpilled = Boolean(card.result?.output_spilled || spillUri);
  const spillChars =
    typeof card.result?.output_chars === "number" && card.result.output_chars > 0
      ? card.result.output_chars
      : null;
  const hasExitCode = typeof card.result?.exit_code === "number";
  const nonZeroExit = hasExitCode && card.result!.exit_code !== 0;

  // Hermes tool-row spec: monochrome. Success is SILENT (no glyph -- the row
  // reads as done without a checkmark); only running (spinner) and hard error
  // (destructive) carry a leading glyph. Gate suppressions are muted "blocked",
  // not red -- they are intentional harness redirects, not tool failures.
  const suppressed = isGateSuppressed(card);
  const isErr = (!!card.result?.error || nonZeroExit) && !suppressed;
  const { linkKind, value: goalValue } = classifyActionGoal(card.kind || "", rawGoal);
  const commandRevealId = String(card.id || card.result?.job_id || goalValue || "").trim();
  const commandRevealOutput = String(card.result?.output || "");

  // Bind this card's process id the way Hermes keys background terminals
  // by procId, then keep an open mirror in sync as output grows.
  useEffect(() => {
    if (linkKind !== "command" || !commandRevealId || !goalValue) return;
    registerAgentCommandSession({
      id: commandRevealId,
      command: goalValue,
      output: commandRevealOutput,
    });
    if (commandRevealOutput) syncAgentCommandOutput(commandRevealId, commandRevealOutput);
  }, [linkKind, commandRevealId, goalValue, commandRevealOutput]);

  const openCommandReveal = (command: string) => {
    const cmd = (command || "").trim();
    if (!cmd) return;
    openAgentCommand(cmd, {
      id: card.id || card.result?.job_id || cmd,
      output: card.result?.output || "",
      run: false,
    });
  };

  const onGoalClick = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (linkKind === "file") openAgentFile(goalValue);
    else if (linkKind === "url") openAgentUrl(goalValue);
    else if (linkKind === "command") openCommandReveal(goalValue);
    else if (linkKind === "image") openAgentImage(goalValue);
    else if (linkKind === "workspace") openAgentWorkspace(goalValue);
    else if (linkKind === "job") openAgentSwarmJob(goalValue);
    else if (linkKind === "spill") openAgentSpill(goalValue);
  };

  const onRunCommand = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    openAgentCommand(goalValue, { run: true });
  };

  return (
    <div className="flex flex-col w-full select-none">
      <div className="flex items-center justify-between w-full py-0.5 px-1 rounded-sm hover:bg-panel2/20 text-left text-[12px] font-sans font-normal group transition-colors">
        <div className="flex items-center gap-2 min-w-0 flex-1">
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={card.open}
            className="flex items-center gap-2 min-w-0 text-left bg-transparent border-0 p-0 cursor-pointer font-sans font-normal text-[12px]"
          >
            <div className="flex items-center justify-center w-3.5 h-3.5 shrink-0">
              {effectivelyRunning ? (
                <Loader2 size={11} className="animate-spin text-faint/60" />
              ) : isErr ? (
                <span className="w-1.5 h-1.5 rounded-full bg-risk/70" />
              ) : suppressed ? (
                <span className="w-1.5 h-1.5 rounded-full bg-faint/45" />
              ) : null}
            </div>
            <span className={`shrink-0 font-normal ${isErr ? "text-risk/80" : suppressed ? "text-faint/70" : "text-faint/80"}`}>
              {toolName}
            </span>
            {duplicateCount > 1 ? (
              <span className="shrink-0 text-faint/55 tabular-nums" title={`${duplicateCount} identical failures`}>
                ×{duplicateCount}
              </span>
            ) : null}
            {goalPreview && (linkKind === "none" || !goalValue) ? (
              <span className="text-faint/65 truncate max-w-[70%] font-normal" title={rawGoal}>
                {goalPreview}
              </span>
            ) : null}
            <ChevronRight
              size={11}
              className={`text-faint/35 group-hover:text-faint/60 transition shrink-0 ${
                card.open ? "rotate-90" : ""
              }`}
            />
          </button>
          {goalPreview && linkKind !== "none" && goalValue ? (
            <button
              type="button"
              onClick={onGoalClick}
              className="text-accent/75 hover:underline underline-offset-2 truncate max-w-[70%] font-normal cursor-pointer bg-transparent border-0 p-0 text-[12px] font-sans"
              title={
                linkKind === "file"
                  ? `Open ${goalValue}`
                  : linkKind === "url"
                  ? "Open in browser"
                  : linkKind === "image"
                  ? "View image"
                  : linkKind === "workspace"
                  ? "Open workspace"
                  : linkKind === "job"
                  ? "Open in Swarm Tracker"
                  : linkKind === "spill"
                  ? "Open spilled output"
                  : "Reveal command output"
              }
            >
              {goalPreview}
            </button>
          ) : null}
        </div>

        <div className="flex items-center gap-2 shrink-0 text-[10px] text-faint/50 select-none tabular-nums ml-2">
          {linkKind === "command" && goalValue && (
            <button
              type="button"
              onClick={onRunCommand}
              className="inline-flex items-center gap-0.5 px-1 py-0.5 rounded border border-edge/35 hover:bg-panel2/40 hover:text-txt cursor-pointer bg-transparent text-[10px] font-sans"
              title="Run in terminal"
            >
              <Play size={9} />
              Run
            </button>
          )}
          {meta && <span>{meta}</span>}
        </div>
      </div>

      {showNested && (
        <div className="mt-0.5 ml-5 pl-2 border-l border-edge/50 space-y-0.5">
          {nested.map((action) => {
            const nestedLabel = toolRowLabel(action.kind || "");
            const nestedGoal = shortenGoal(action.goal || "", 52);
            const nestedErr = Boolean(action.error) || action.status === "failed";
            const nestedLink = classifyActionGoal(action.kind || "", action.goal || "");
            const nestedClickable = nestedLink.linkKind !== "none" && !!nestedLink.value;
            return (
              <div
                key={action.action_id}
                className="flex items-center gap-2 py-0.5 px-1 text-[11px] font-sans font-normal text-faint/80"
                data-testid="nested-worker-action"
                data-action-id={action.action_id}
                data-status={action.status}
              >
                <div className="flex items-center justify-center w-3 h-3 shrink-0">
                  {action.status === "running" && effectivelyRunning ? (
                    <Loader2 size={10} className="animate-spin text-faint/60" />
                  ) : nestedErr ? (
                    <span className="w-1 h-1 rounded-full bg-risk/70" />
                  ) : null}
                </div>
                <span className={`shrink-0 ${nestedErr ? "text-risk/80" : "text-faint/75"}`}>
                  {nestedLabel}
                </span>
                {nestedGoal && nestedClickable ? (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      const v = nestedLink.value;
                      if (nestedLink.linkKind === "file") openAgentFile(v);
                      else if (nestedLink.linkKind === "url") openAgentUrl(v);
                      else if (nestedLink.linkKind === "command") openAgentCommand(v, { id: v, run: false });
                      else if (nestedLink.linkKind === "image") openAgentImage(v);
                      else if (nestedLink.linkKind === "workspace") openAgentWorkspace(v);
                      else if (nestedLink.linkKind === "job") openAgentSwarmJob(v);
                      else if (nestedLink.linkKind === "spill") openAgentSpill(v);
                    }}
                    className="truncate text-accent/75 hover:underline underline-offset-2 bg-transparent border-0 p-0 text-left cursor-pointer font-sans text-[11px]"
                    title={action.goal}
                  >
                    {nestedGoal}
                  </button>
                ) : nestedGoal ? (
                  <span className="truncate text-faint/65" title={action.goal}>
                    {nestedGoal}
                  </span>
                ) : null}
                {typeof action.duration_ms === "number" && action.status !== "running" ? (
                  <span className="ml-auto tabular-nums text-faint/40 shrink-0">
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
        <div className="mt-1 ml-5 pl-3 border-l border-edge/50 py-1.5 pr-3 bg-panel2/25 rounded-r-sm text-[11px] max-w-full text-txt/85 space-y-1 font-sans">
          {/* Never render an empty key row — that was the "goal" with no value. */}
          {commandKv ? (
            <KV
              k={inputKey}
              v={commandKv}
              linkKind={classifyActionGoal(card.kind || "", commandKv).linkKind}
              onCommandClick={() => openCommandReveal(commandKv)}
            />
          ) : null}
          {Array.isArray(card.goals) && card.goals.length > 1 && (
            <div className="space-y-0.5">
              {card.goals.map((g, i) => (
                <KV
                  key={`goal-${i}`}
                  k={`g${i + 1}`}
                  v={g}
                  linkKind={classifyActionGoal(card.kind || "", g).linkKind}
                />
              ))}
            </div>
          )}
          {card.cwd && <KV k="cwd" v={card.cwd} linkKind="workspace" />}
          {hasExitCode ? (
            <KV k="exit" v={String(card.result!.exit_code)} />
          ) : null}
          {card.result?.error && (
            <div className={`mt-1 font-sans ${suppressed ? "text-faint/80" : "text-risk"}`}>
              {suppressed ? card.result.error : `error: ${card.result.error}`}
            </div>
          )}
          {resultOutput.trim() ? (
            <ClickableProcessOutput text={resultOutput} />
          ) : null}
          {outputSpilled && spillUri ? (
            <button
              type="button"
              data-testid="spill-output-peek"
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                openAgentSpill(spillUri);
              }}
              className="mt-1 inline-flex items-center gap-1 text-accent/85 hover:underline underline-offset-2 cursor-pointer bg-transparent border-0 p-0 font-sans text-[11px]"
              title={`Open full spilled output (${spillUri})`}
            >
              {spillChars != null
                ? `Full output (${spillChars.toLocaleString()} chars)`
                : "Full output"}
            </button>
          ) : null}
          {card.result && !card.result.error && (
            <>
              {card.result.job_id && (
                <KV
                  k="job"
                  v={card.result.job_id || ""}
                  linkKind={looksLikeJobId(card.result.job_id) ? "job" : undefined}
                />
              )}
              {spillUri && looksLikeSpillUri(spillUri) ? (
                <KV k="spill" v={spillUri} linkKind="spill" />
              ) : null}
              {/* Dispatch-only ack (backgrounded run_implement/run_parallel): show
                  its status/message; the rich artifact fields aren't present yet. */}
              {Array.isArray(card.result.types) ? (
                <KV k="found" v={`${card.result.num ?? 0} artifacts · ${card.result.types.join(", ")}`} />
              ) : (card.result.message || card.result.status) ? (
                <KV k="status" v={card.result.message || card.result.status || ""} />
              ) : null}
              {(card.result.adapter === "demo" || card.result.adapter === "refused-demo") && (
                <div className="text-warn text-[10px] mt-1 font-sans">
                  demo substrate refused -- not real codebase analysis
                </div>
              )}
              {/* Never render demo placeholder findings as audit results. */}
              {card.result.adapter !== "demo" && card.result.adapter !== "refused-demo" &&
                (card.result.artifacts || []).map((a, i) => (
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
function ClickableProcessOutput({ text }: { text: string }) {
  const segments = tokenizeClickableOutput(text);
  return (
    <pre
      className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-all font-mono text-[10px] leading-snug text-txt/85 bg-panel/60 border border-edge/40 rounded px-2 py-1.5"
      data-testid="run-command-output"
    >
      {segments.map((seg, i) => {
        if (seg.kind === "url") {
          return (
            <button
              key={i}
              type="button"
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                openAgentUrl(seg.href);
              }}
              title="Open in browser"
              className="text-accent/90 hover:underline underline-offset-2 cursor-pointer bg-transparent border-0 p-0 font-inherit"
            >
              {seg.text}
            </button>
          );
        }
        if (seg.kind === "spill") {
          return (
            <button
              key={i}
              type="button"
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                openAgentSpill(seg.uri);
              }}
              title="Open spilled output"
              className="text-accent/90 hover:underline underline-offset-2 cursor-pointer bg-transparent border-0 p-0 font-inherit"
            >
              {seg.text}
            </button>
          );
        }
        if (seg.kind === "file") {
          return (
            <button
              key={i}
              type="button"
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                openAgentFile(seg.path);
              }}
              title={`Open ${seg.path}`}
              className="text-accent/90 hover:underline underline-offset-2 cursor-pointer bg-transparent border-0 p-0 font-inherit"
            >
              {seg.text}
            </button>
          );
        }
        return <span key={i}>{seg.text}</span>;
      })}
    </pre>
  );
}

const KV = ({
  k,
  v,
  linkKind,
  onCommandClick,
}: {
  k: string;
  v: string;
  linkKind?: AgentLinkKind;
  onCommandClick?: () => void;
}) => {
  const clickable =
    linkKind === "file"
    || linkKind === "url"
    || linkKind === "command"
    || linkKind === "image"
    || linkKind === "workspace"
    || linkKind === "job"
    || linkKind === "spill";
  return (
    <div className="flex gap-2 mb-0.5">
      <span className="text-muted w-14 shrink-0">{k}</span>
      {clickable && v ? (
        <button
          type="button"
          className="break-all text-left text-accent/85 hover:underline underline-offset-2"
          data-testid={
            linkKind === "job"
              ? "job-id-link"
              : linkKind === "spill"
                ? "spill-uri-link"
                : undefined
          }
          title={
            linkKind === "job"
              ? "Open in Swarm Tracker"
              : linkKind === "spill"
                ? "Open spilled output"
                : undefined
          }
          onClick={(e) => {
            e.stopPropagation();
            if (linkKind === "file") openAgentFile(v);
            else if (linkKind === "url") openAgentUrl(v);
            else if (linkKind === "image") openAgentImage(v);
            else if (linkKind === "workspace") openAgentWorkspace(v);
            else if (linkKind === "job") openAgentSwarmJob(v);
            else if (linkKind === "spill") openAgentSpill(v);
            else if (onCommandClick) onCommandClick();
            else openAgentCommand(v, { id: v, run: false });
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

/** Clickable job-id chips for swarm_pending pills (tracker deep-link). */
function SwarmJobIdChips({ jobIds }: { jobIds: string[] }) {
  const ids = jobIds.map((id) => String(id || "").trim()).filter(Boolean);
  if (ids.length === 0) return null;
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <span>(</span>
      {ids.map((id, i) => (
        <span key={id} className="inline-flex items-center gap-1">
          {i > 0 ? <span>,</span> : null}
          {looksLikeJobId(id) ? (
            <button
              type="button"
              data-testid="swarm-pending-job-chip"
              title="Open in Swarm Tracker"
              className="font-mono text-accent/85 hover:underline underline-offset-2 cursor-pointer bg-transparent border-0 p-0"
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                openAgentSwarmJob(id);
              }}
            >
              {id}
            </button>
          ) : (
            <span className="font-mono">{id}</span>
          )}
        </span>
      ))}
      <span>)</span>
    </span>
  );
}

function SwarmPendingPill({
  jobIds,
  objective,
  status,
}: {
  jobIds: string[];
  objective: string;
  status: SwarmPendingStatus;
}) {
  const truncatedObj = objective.length > 60 ? objective.slice(0, 60) + "..." : objective;
  const label = status === "failed"
    ? "swarm failed"
    : status === "ended"
      ? "swarm ended"
      : status === "done"
        ? "swarm done"
        : "swarm running";
  const shell =
    status === "failed"
      ? "bg-risk/10 border-risk/30 text-risk/80"
      : status === "ended"
        ? "bg-panel2/15 border-edge/20 text-faint"
        : status === "done"
          ? "bg-panel2/20 border-edge/30 text-faint"
          : "bg-panel2/60 border-edge/60 text-muted";
  const dot =
    status === "failed"
      ? "bg-risk/50"
      : status === "done"
        ? "bg-good/40"
        : "bg-faint/40";
  return (
    <div className={`flex items-center gap-1.5 py-1 px-3 rounded-full border text-[11px] w-fit my-1 select-none ${shell}`}>
      {status === "running"
        ? <Loader2 size={11} className="animate-spin text-accent" />
        : <span className={`w-1.5 h-1.5 rounded-full ${dot}`} />}
      <span className="inline-flex items-center gap-1 flex-wrap">
        <span>{label}: {truncatedObj}</span>
        <SwarmJobIdChips jobIds={jobIds} />
      </span>
    </div>
  );
}

// A swarm outcome in the transcript. Previously this dumped the entire worker
// summary as full-width green/red monospace text -- a "wall" that read as noise
// on a finished run. Now it's a compact status line (icon + verb + objective +
// file count) that stays collapsed by default; the full summary, file chips,
// and any error live behind a click. Status color is confined to the icon,
// label, and border so the body text stays readable instead of tinted.
function reuseStatusLabel(status?: string): string | null {
  const s = (status || "").trim().toLowerCase();
  if (s === "reused") return "reused";
  if (s === "partial") return "partially reverified";
  if (s === "invalidated") return "invalidated";
  if (s === "fresh") return "fresh";
  return null;
}

/** Bounded relative-path summary for partial reuse honesty (no secrets). */
function formatInvalidatedPaths(paths?: string[], limit = 6): string {
  const clean = (paths || [])
    .map((p) => String(p || "").replace(/\\/g, "/").trim())
    .filter(Boolean)
    .slice(0, limit);
  if (!clean.length) return "";
  const more = (paths || []).length - clean.length;
  return more > 0 ? `${clean.join(", ")} (+${more} more)` : clean.join(", ");
}

function SwarmJobIdButton({
  jobId,
  className,
}: {
  jobId: string;
  className?: string;
}) {
  const id = (jobId || "").trim();
  if (!id) return null;
  if (!looksLikeJobId(id)) {
    return <span className={className}>{id}</span>;
  }
  return (
    <button
      type="button"
      data-testid="swarm-result-job-link"
      title="Open in Swarm Tracker"
      className={`font-mono text-accent/85 hover:underline underline-offset-2 cursor-pointer bg-transparent border-0 p-0 ${className || ""}`}
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        openAgentSwarmJob(id);
      }}
    >
      {id}
    </button>
  );
}

function SwarmResultCard({ jobId, applied, files, summary, error, objective, heldForReview, analysisOk, reuseStatus, sourceJobId, reuseReason, invalidatedPaths, duplicateCount = 1 }: {
  jobId?: string;
  applied: boolean;
  files: string[];
  summary: string;
  error: string | null;
  objective?: string;
  heldForReview?: boolean;
  analysisOk?: boolean;
  reuseStatus?: string;
  sourceJobId?: string;
  reuseReason?: string;
  invalidatedPaths?: string[];
  duplicateCount?: number;
}) {
  const [open, setOpen] = useState(false);
  const obj = objective ? (objective.length > 70 ? objective.slice(0, 70) + "..." : objective) : "swarm";
  const reuseLabel = reuseStatusLabel(reuseStatus);
  const pathSummary = formatInvalidatedPaths(invalidatedPaths);
  const primaryJobId = (jobId || "").trim();
  // Operator honesty: held_for_review / analysis_ok are successful non-applies —
  // never paint them as "swarm done" (applied) or "swarm failed".
  const tone: "applied" | "held" | "analysis" | "failed" = applied
    ? "applied"
    : error
      ? "failed"
      : heldForReview
        ? "held"
        : analysisOk
          ? "analysis"
          : "failed";
  const label =
    tone === "applied"
      ? "swarm done"
      : tone === "held"
        ? "held for review"
        : tone === "analysis"
          ? "analysis done"
          : "swarm failed";
  const borderClass =
    tone === "applied"
      ? "border-good/30"
      : tone === "held"
        ? "border-accent/30"
        : tone === "analysis"
          ? "border-edge/50"
          : "border-risk/30";
  const labelClass =
    tone === "applied"
      ? "text-good"
      : tone === "held"
        ? "text-accent"
        : tone === "analysis"
          ? "text-muted"
          : "text-risk";
  // Full-swarm rejection reasons (e.g. environment_changed) must surface even
  // when the status is merely "fresh" — never drop the gate reason in the UI.
  const hasBody = !!(
    summary
    || (tone === "failed" && error)
    || (applied && files.length > 0)
    || tone === "held"
    || tone === "analysis"
    || primaryJobId
    || sourceJobId
    || reuseReason
    || pathSummary
    || reuseLabel
  );

  return (
    <div
      className={`rounded-md border w-fit max-w-full my-1 overflow-hidden select-none bg-panel/40 ${borderClass}`}
      data-testid="swarm-result-card"
      data-outcome={tone}
    >
      <button
        type="button"
        onClick={() => {
          // Post-hydrate, pending_review receipts are gone; held cards are the
          // durable Review re-entry (same focus+refresh as the SSE receipt).
          if (tone === "held") focusReviewTabAndRefresh();
          if (hasBody) setOpen((v) => !v);
        }}
        className={`flex items-center gap-2 px-2.5 py-1.5 text-[11px] w-full text-left transition-colors ${hasBody ? "hover:bg-panel2/40 cursor-pointer" : "cursor-default"}`}
        title={tone === "held" ? "Open Review tab" : (objective || undefined)}
      >
        {tone === "applied"
          ? <CheckCircle2 size={13} className="text-good shrink-0" />
          : tone === "held"
            ? <Eye size={13} className="text-accent shrink-0" />
            : tone === "analysis"
              ? <CheckCircle2 size={13} className="text-muted shrink-0" />
              : <XCircle size={13} className="text-risk shrink-0" />}
        <span className={`font-medium shrink-0 ${labelClass}`}>
          {label}
          {tone === "failed" && duplicateCount > 1 ? ` ×${duplicateCount}` : ""}
        </span>
        {reuseLabel && (
          <span
            className="text-[9px] font-mono text-muted bg-panel2/70 border border-edge/50 px-1.5 py-0.5 rounded shrink-0"
            title={pathSummary || reuseReason || sourceJobId || reuseLabel}
          >
            {reuseLabel}
          </span>
        )}
        <span className="text-muted truncate">{obj}</span>
        <span className="flex-1 min-w-[8px]" />
        {tone === "applied"
          ? (files.length > 0
            ? <span className="text-faint shrink-0 tabular-nums">{files.length} file{files.length === 1 ? "" : "s"}</span>
            : <span className="text-faint shrink-0 truncate max-w-[45%]">{summary}</span>)
          : tone === "held"
            ? <span className="text-accent/70 shrink-0 truncate max-w-[45%]">awaiting review</span>
            : tone === "analysis"
              ? <span className="text-faint shrink-0 truncate max-w-[45%]">{summary || "findings"}</span>
              : <span className="text-risk/70 shrink-0 truncate max-w-[45%]">{error || "error"}</span>}
        {hasBody && (open
          ? <ChevronDown size={12} className="text-faint shrink-0" />
          : <ChevronRight size={12} className="text-faint shrink-0" />)}
      </button>

      {open && hasBody && (
        <div className="px-2.5 pb-2 pt-1.5 border-t border-edge/30 flex flex-col gap-1.5">
          {primaryJobId ? (
            <div className="text-[10px] text-muted font-mono leading-relaxed break-words inline-flex items-center gap-1.5 flex-wrap">
              <span>job</span>
              <SwarmJobIdButton jobId={primaryJobId} />
            </div>
          ) : null}
          {(reuseLabel || reuseReason || sourceJobId) && (
            <div className="text-[10px] text-muted font-mono leading-relaxed break-words inline-flex items-center gap-1 flex-wrap">
              <span>{reuseLabel ? `validation ${reuseLabel}` : "validation"}</span>
              {sourceJobId ? (
                <>
                  <span>from</span>
                  <SwarmJobIdButton jobId={sourceJobId} />
                </>
              ) : null}
              {reuseReason ? <span>({reuseReason})</span> : null}
            </div>
          )}
          {Array.isArray(invalidatedPaths) && invalidatedPaths.length > 0 && (
            <div className="flex flex-col gap-1">
              <div className="text-[10px] text-muted font-mono">invalidated paths</div>
              <div className="flex flex-wrap gap-1">
                {invalidatedPaths.map((p) => {
                  const path = String(p || "").trim();
                  if (!path) return null;
                  return (
                    <button
                      key={path}
                      type="button"
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        openAgentFile(path);
                      }}
                      className="text-[9px] font-mono text-accent/85 bg-panel2/60 border border-edge/50 rounded px-1 py-0.5 hover:underline underline-offset-2 cursor-pointer"
                      title={`Open ${path}`}
                    >
                      {path.replace(/\\/g, "/")}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
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
