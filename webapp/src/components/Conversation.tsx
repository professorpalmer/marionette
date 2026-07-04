import { useEffect, useLayoutEffect, useRef, useState, useCallback, memo } from "react";
import { ChevronRight, Loader2, Send, Zap, Square, Folder, ChevronDown, ChevronUp, GripVertical, Trash2, GitBranch, ListChecks, Play, Copy, Check, Pencil, RefreshCw, FileText, History, X, Code, Share2, CheckCircle2, XCircle } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import "highlight.js/styles/github-dark.css";
import { api, type Config } from "../lib/api";
import { usePolling } from "../lib/usePolling";
import PilotPicker from "./PilotPicker";
import { pickFolder } from "../lib/transport";
import FileEditorPane from "./FileEditorPane";

type Msg = {
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
type Card = {
  id: string; goal: string; cwd?: string | null;
  running: boolean; open: boolean;
  kind?: string;
  // Fields are optional because a card's result can be a full tool outcome
  // (num/types/artifacts) OR a lightweight dispatch ack (status/message) for a
  // backgrounded run_implement/run_parallel job. Rendering must not assume the
  // rich shape -- expanding a dispatch-only card used to crash on types.join.
  result?: { job_id?: string; num?: number; types?: string[]; adapter?: string;
             artifacts?: { type: string; headline: string }[]; error?: string;
             status?: string; message?: string; duration_ms?: number };
};
type Item =
  | { kind: "msg"; msg: Msg }
  | { kind: "card"; card: Card }
  | { kind: "thinking"; text: string }
  | { kind: "swarm_pending"; job_ids: string[]; objective: string; resolved?: boolean }
  | { kind: "swarm_result"; job_id: string; applied: boolean; files: string[]; summary: string; error: string | null; objective?: string }
  | { kind: "checkpoint"; id: string; label: string; trigger: string }
  | { kind: "compaction"; before_tokens: number; after_tokens: number }
  | { kind: "codegraph_context"; symbols: number; query: string }
  | { kind: "command_blocked"; command: string; category: string; reason: string; matched: string }
  | { kind: "auth_failure"; message: string; id?: string }
  | { kind: "steer"; text: string };

type GroupedItem =
  | { kind: "msg"; msg: Msg }
  | { kind: "thinking"; text: string }
  | { kind: "swarm_pending"; job_ids: string[]; objective: string; resolved?: boolean }
  | { kind: "swarm_result"; job_id: string; applied: boolean; files: string[]; summary: string; error: string | null; objective?: string }
  | { kind: "checkpoint"; id: string; label: string; trigger: string }
  | { kind: "compaction"; before_tokens: number; after_tokens: number }
  | { kind: "codegraph_context"; symbols: number; query: string }
  | { kind: "command_blocked"; command: string; category: string; reason: string; matched: string }
  | { kind: "auth_failure"; message: string; id?: string }
  | { kind: "steer"; text: string }
  | { kind: "activity_group"; items: ( { kind: "card"; card: Card } | { kind: "thinking"; text: string } | { kind: "codegraph_context"; symbols: number; query: string } | { kind: "msg"; msg: Msg } )[] };

function getSimilarity(s1: string, s2: string): number {
  const norm1 = s1.toLowerCase().replace(/[^a-z0-9]/g, "");
  const norm2 = s2.toLowerCase().replace(/[^a-z0-9]/g, "");
  
  if (!norm1 || !norm2) return 0;
  if (norm1 === norm2) return 1.0;
  
  if (norm1.startsWith(norm2) || norm2.startsWith(norm1)) {
    return 1.0;
  }
  
  const w1 = s1.toLowerCase().replace(/[^a-z0-9\s]/g, "").split(/\s+/).filter(Boolean);
  const w2 = s2.toLowerCase().replace(/[^a-z0-9\s]/g, "").split(/\s+/).filter(Boolean);
  const set1 = new Set(w1);
  const set2 = new Set(w2);
  let intersect = 0;
  set1.forEach(w => {
    if (set2.has(w)) intersect++;
  });
  const wordJaccard = intersect / (set1.size + set2.size - intersect);
  
  const getBigrams = (s: string) => {
    const bigrams = new Set<string>();
    for (let i = 0; i < s.length - 1; i++) {
      bigrams.add(s.substring(i, i + 2));
    }
    return bigrams;
  };
  const b1 = getBigrams(norm1);
  const b2 = getBigrams(norm2);
  if (b1.size > 0 && b2.size > 0) {
    let bIntersect = 0;
    b1.forEach(b => {
      if (b2.has(b)) bIntersect++;
    });
    const charJaccard = bIntersect / (b1.size + b2.size - bIntersect);
    return Math.max(wordJaccard, charJaccard);
  }
  
  return wordJaccard;
}

function deduplicateConsecutiveAssistantMessages(items: Item[]): Item[] {
  const result: Item[] = [];
  for (const item of items) {
    if (item.kind === "msg" && item.msg.role === "assistant") {
      const last = result[result.length - 1];
      if (last && last.kind === "msg" && last.msg.role === "assistant") {
        const lastText = last.msg.text;
        const newText = item.msg.text;
        
        if (getSimilarity(lastText, newText) > 0.85) {
          if (newText.length > lastText.length) {
            result[result.length - 1] = item;
          }
          continue;
        }
      }
    }
    result.push(item);
  }
  return result;
}

function groupAgentActivity(items: Item[], intermediateItems: Set<Item>): GroupedItem[] {
  // Mental model (Cursor/Hermes): a turn is [user msg] + [investigation] +
  // [final answer]. The investigation -- every tool card, reasoning block,
  // codegraph chip, AND per-step micro-narration -- folds into ONE collapsible
  // box. Only the user message and the FINAL assistant answer render standalone.
  //
  // To classify an assistant message as "mid-investigation" (fold) vs "final
  // answer" (standalone), we look ahead: if a tool card appears later in this
  // turn (before the next user message), this assistant msg is just per-step
  // chatter and belongs in the box. The last assistant msg with no card after it
  // is the real answer.
  const grouped: GroupedItem[] = [];
  let currentGroup: ( { kind: "card"; card: Card } | { kind: "thinking"; text: string } | { kind: "codegraph_context"; symbols: number; query: string } | { kind: "msg"; msg: Msg } )[] = [];

  const flush = () => {
    if (currentGroup.length > 0) {
      grouped.push({ kind: "activity_group", items: currentGroup });
      currentGroup = [];
    }
  };

  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    if (item.kind === "thinking" && (!item.text || !item.text.trim())) continue;

    if (item.kind === "msg") {
      // Cursor-tight model: intermediate per-step narration -- an assistant
      // message with MORE tool activity still to come this turn -- folds INTO the
      // collapsed activity box as a tight muted line, instead of a full standalone
      // bubble that spams the transcript and scrolls the view. Only user messages
      // and the FINAL assistant answer break out standalone at full size.
      if (item.msg.role === "assistant" && intermediateItems.has(item) && !item.msg.streaming) {
        currentGroup.push(item);
      } else {
        flush();
        grouped.push(item);
      }
    } else if (item.kind === "swarm_pending" || item.kind === "swarm_result" || item.kind === "checkpoint" || item.kind === "compaction" || item.kind === "command_blocked" || item.kind === "auth_failure" || item.kind === "steer") {
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

const SLASH_COMMANDS = [
  { cmd: "/clear", desc: "Clear visible transcript" },
  { cmd: "/new", desc: "Clear visible transcript (new session)" },
  { cmd: "/compact", desc: "Trigger manual context compaction" },
  { cmd: "/model", desc: "Focus model picker to switch models" },
  { cmd: "/help", desc: "Render a small help note" }
];



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
function stableItemKey(it: GroupedItem, i: number): string {
  switch (it.kind) {
    case "msg":
      return `msg-${objKey(it.msg)}`;
    case "activity_group":
      // Key off the first inner item's identity so the group stays stable as
      // long as its lead item does; falls back to index for empty groups.
      return `grp-${it.items[0] ? objKey(it.items[0]) : i}`;
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
    case "auth_failure":
      return `auth-${it.id || i}`;
    case "steer":
      return `steer-${i}`;
    case "thinking":
      return `think-${i}`;
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
const RENDER_WINDOW = 200;

// PERF: Memoized transcript renderer. Its props are intentionally free of the
// composer `input` (or any per-keystroke state), so React.memo lets typing skip
// re-rendering the whole transcript. Only transcript-affecting state (items,
// status, compactingStatus, editingIndex, auto, plan) plus stable callbacks are
// passed in; all callbacks are useCallback-stabilized in the parent so the memo
// comparison holds.
type TranscriptListProps = {
  items: Item[];
  status: "idle" | "thinking" | "executing" | "done" | "error" | "streaming";
  compactingStatus: string | null;
  editingIndex: number | null;
  auto: boolean;
  plan: boolean;
  scrollContainerRef: React.RefObject<HTMLDivElement | null>;
  onEditMessage: (idx: number, originalText: string) => void;
  onExecuteSend: (msg: string, useAuto: boolean, usePlan?: boolean) => void;
  onImageClick: (url: string) => void;
  onSetCard: (id: string, patch: Partial<Card>) => void;
  onExecutePlan: (planText: string) => void;
};

const TranscriptList = memo(function TranscriptList({
  items,
  status,
  compactingStatus,
  editingIndex,
  auto,
  plan,
  scrollContainerRef,
  onEditMessage,
  onExecuteSend,
  onImageClick,
  onSetCard,
  onExecutePlan,
}: TranscriptListProps) {
  const intermediateItems = new Set<Item>();
  let hasSeenCardOrAssistantMsgInTurn = false;
  for (let j = items.length - 1; j >= 0; j--) {
    const item = items[j];
    if (item.kind === "msg" && item.msg.role === "user") {
      hasSeenCardOrAssistantMsgInTurn = false;
    } else if (item.kind === "msg" && item.msg.role === "assistant") {
      if (hasSeenCardOrAssistantMsgInTurn) {
        intermediateItems.add(item);
      }
      hasSeenCardOrAssistantMsgInTurn = true;
    } else if (item.kind === "card") {
      hasSeenCardOrAssistantMsgInTurn = true;
    }
  }

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
      const isNotBusy = status === "idle" || status === "done" || status === "error";
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
      if (it.resolved) {
        return (
          <div key={key} className="flex items-center gap-1.5 py-1 px-3 rounded-full bg-panel2/20 border border-edge/30 text-[11px] text-faint w-fit my-1 select-none">
            <span className="w-1.5 h-1.5 rounded-full bg-good/40" />
            <span>swarm done: {truncatedObj} ({jobIdsStr})</span>
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
      return (
        <div key={key} className="flex items-start gap-1.5 py-1.5 px-3 rounded-lg bg-red-500/8 border border-red-500/30 text-[11px] text-red-300/90 w-fit max-w-full my-1 select-none" title={it.matched ? `matched: ${it.matched}` : undefined}>
          <span className="font-medium shrink-0">Blocked in full-auto:</span>
          <span className="min-w-0">
            <span className="text-red-300/70">{it.reason}</span>
            {it.command ? <code className="block mt-0.5 text-[10px] text-faint/80 font-mono truncate">{it.command}</code> : null}
          </span>
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
      return <ThinkingBlock key={key} text={it.text} />;
    } else if (it.kind === "activity_group") {
      return (
        <ActivityGroup
          key={key}
          items={it.items}
          onToggleCard={(card) => onSetCard(card.id, { open: !card.open })}
        />
      );
    }
    return null;
  });

  const isBusy = status === "thinking" || status === "executing" || status === "streaming";
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
      {isBusy && !compactingStatus && (
        <div className="flex items-center gap-1.5 py-1 text-[12px] text-muted select-none mt-1 pl-0.5">
          <Loader2 size={12} className="animate-spin text-muted" />
          <span>{status === "thinking" ? "thinking..." : status === "streaming" ? "streaming..." : "running..."}</span>
        </div>
      )}
    </>
  );
});

export default function Conversation({ config, activeSessionId, onArtifacts, onJobChange }: {
  config: Config | null;
  activeSessionId: string | null;
  onArtifacts: (a: { type: string; headline: string }[]) => void;
  onJobChange: () => void;
}) {
  const [items, setItems] = useState<Item[]>([]);

  const [openTabs, setOpenTabs] = useState<{ path: string; isDirty: boolean }[]>([]);
  const [activeTab, setActiveTab] = useState<string>("chat");

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

  const handleTabDirtyChange = (path: string, isDirty: boolean) => {
    setOpenTabs((prev) =>
      prev.map((t) => (t.path === path ? { ...t, isDirty } : t))
    );
  };

  useEffect(() => {
    const handleOpenFile = (e: CustomEvent<{ path: string }>) => {
      const filePath = e.detail.path;
      setOpenTabs((prev) => {
        const exists = prev.some((t) => t.path === filePath);
        if (exists) return prev;
        return [...prev, { path: filePath, isDirty: false }];
      });
      setActiveTab(filePath);
    };
    window.addEventListener("harness-open-file", handleOpenFile as EventListener);
    return () => {
      window.removeEventListener("harness-open-file", handleOpenFile as EventListener);
    };
  }, []);
  const [input, setInput] = useState("");
  const [status, setStatus] = useState<"idle"|"thinking"|"executing"|"done"|"error"|"streaming">("idle");
  const [auto, setAuto] = useState(false);
  const [plan, setPlan] = useState(false);
  const [distillNotice, setDistillNotice] = useState<string | null>(null);
  const [wikiPrepared, setWikiPrepared] = useState<{ pages: any[]; autoIngested: boolean } | null>(null);
  const cancelRef = useRef<null | (() => void)>(null);
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

  const [pendingJobIds, setPendingJobIds] = useState<string[]>([]);
  const processedSwarmJobIdsRef = useRef<string[]>([]);
  const [backendPendingSwarms, setBackendPendingSwarms] = useState(false);

  const [attachedImages, setAttachedImages] = useState<{ path: string; name: string; previewUrl: string }[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  // Auto-fade upload errors after 6s so a transient failure doesn't sit in the
  // composer looking permanently broken (it used to persist until the next
  // upload attempt). Pass null to clear immediately.
  const flashUploadError = (msg: string | null) => {
    setUploadError(msg);
    if (msg) {
      window.setTimeout(() => setUploadError((cur) => (cur === msg ? null : cur)), 6000);
    }
  };
  const [lightboxUrl, setLightboxUrl] = useState<string | null>(null);

  // Compacting & Context breakdown states
  const [compactingStatus, setCompactingStatus] = useState<string | null>(null);
  const [showContextPanel, setShowContextPanel] = useState(false);
  const [contextUsage, setContextUsage] = useState<import("../lib/api").ContextUsageResponse | null>(null);

  // Ergonomics states
  const [allFiles, setAllFiles] = useState<string[]>([]);
  const [mentionSearch, setMentionSearch] = useState<string | null>(null);
  const [mentionIndex, setMentionIndex] = useState<number>(-1);
  const [filteredFiles, setFilteredFiles] = useState<string[]>([]);
  const [selectedFileIndex, setSelectedFileIndex] = useState<number>(0);
  const [symbolResults, setSymbolResults] = useState<{ name: string; kind: string; path: string; line: number }[]>([]);
  const [codegraphStatus, setCodegraphStatus] = useState<string | null>(null);

  const [slashSearch, setSlashSearch] = useState<string | null>(null);
  const [selectedSlashIndex, setSelectedSlashIndex] = useState<number>(0);

  const [editingIndex, setEditingIndex] = useState<number | null>(null);

  const [customCommands, setCustomCommands] = useState<{ name: string; description: string; scope: string }[]>([]);

  const allSlashCommands = [
    ...SLASH_COMMANDS,
    ...customCommands.map(c => ({
      cmd: "/" + c.name,
      desc: c.description + " (custom)"
    }))
  ];

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
  useEffect(() => {
    const el = feedRef.current;
    if (!el) return;
    const onScroll = () => {
      pinnedToBottomRef.current =
        el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);
  useEffect(() => {
    const el = feedRef.current;
    if (!el) return;
    if (pinnedToBottomRef.current) {
      el.scrollTo(0, el.scrollHeight);
    }
  }, [items]);

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

  useEffect(() => {
    if (activeSessionId) {
      api.sessionTranscript(activeSessionId)
        .then((res) => {
          let loadedItems: any[] = [];
          if (res.display && res.display.length > 0) {
            loadedItems = res.display.map((m: any) => {
              if (m.type === "card") {
                return {
                  kind: "card" as const,
                  card: {
                    id: m.id,
                    goal: m.goal,
                    cwd: m.cwd || null,
                    kind: m.kind,
                    running: false,
                    open: false,
                    result: m.result || undefined
                  }
                };
              } else {
                return {
                  kind: "msg" as const,
                  msg: {
                    role: m.role as "user" | "assistant",
                    text: m.text || ""
                  }
                };
              }
            });
          } else {
            loadedItems = (res.history || [])
              .filter((m: any) => m.role === "assistant" || (m.role === "user" && m.content && !m.content.startsWith("(")))
              .map((m: any) => ({
                kind: "msg" as const,
                msg: {
                  role: m.role as "user" | "assistant",
                  text: m.content || ""
                }
              }));
          }
          setItems(deduplicateConsecutiveAssistantMessages(loadedItems));

          // Gather all artifacts from (a) card entries in res.display
          const displayArtifacts: { type: string; headline: string }[] = [];
          if (res.display && res.display.length > 0) {
            res.display.forEach((m: any) => {
              if (m.type === "card" && m.result && Array.isArray(m.result.artifacts)) {
                m.result.artifacts.forEach((art: any) => {
                  if (art && art.type && art.headline) {
                    displayArtifacts.push({ type: art.type, headline: art.headline });
                  }
                });
              }
            });
          }

          // Merge helper to deduplicate by type+headline
          const mergeAndEmit = (fetchedArts: { type: string; headline: string }[]) => {
            const seen = new Set<string>();
            const unique: { type: string; headline: string }[] = [];
            
            displayArtifacts.concat(fetchedArts).forEach((art) => {
              const key = `${art.type}::${art.headline}`;
              if (!seen.has(key)) {
                seen.add(key);
                unique.push(art);
              }
            });

            if (unique.length > 0) {
              onArtifacts(unique);
            }
          };

          // Fetch artifacts from (b) res.job_ids
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
              const flattened = allJobArts.flat();
              mergeAndEmit(flattened);
            });
          } else {
            mergeAndEmit([]);
          }
        })
        .catch(() => {
          setItems([]);
        });
    } else {
      setItems([]);
    }
  }, [activeSessionId]);

  useEffect(() => {
    setPendingJobIds([]);
    processedSwarmJobIdsRef.current = [];
    setBackendPendingSwarms(false);
    if (activeSessionId) {
      api.getSessionState()
        .then((res) => {
          if (res) {
            setBackendPendingSwarms(res.pending_swarms);
            // After a backend restart (self-edit apply), the transcript reloads
            // with an unanswered user turn. Auto-continue it so the conversation
            // survives the swap -- the Hermes-style seamless self-edit loop.
            if (res.resume_pending) {
              setTimeout(() => resumeTriggerRef.current(), 300);
            }
          }
        })
        .catch(() => {});
    }
  }, [activeSessionId]);

  const setCard = (id: string, patch: Partial<Card>) =>
    setItems((prev) => prev.map((it) => {
      if (it.kind === "card" && it.card.id === id) {
        return { kind: "card", card: { ...it.card, ...patch } };
      }
      return it;
    }));

  useEffect(() => {
    const onFocus = () => { taRef.current?.focus(); };
    window.addEventListener("harness-focus-input", onFocus);
    return () => window.removeEventListener("harness-focus-input", onFocus);
  }, []);

  // Auto-grow textarea effect (Cursor-like dynamic expansion)
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
  }, [input]);

  // Load workspace files for @-mention dropdown
  useEffect(() => {
    api.getWorkspaceFiles()
      .then((res) => {
        if (res && res.files) {
          setAllFiles(res.files);
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
    setEditingIndex(idx);
    setInput(originalText);
    setTimeout(() => {
      if (taRef.current) {
        taRef.current.focus();
      }
    }, 10);
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
      send();
    }
  };

  const handleSwarmResult = (d: any) => {
    const job_id = d.job_id;
    if (!job_id) return;

    if (processedSwarmJobIdsRef.current.includes(job_id)) return;
    processedSwarmJobIdsRef.current.push(job_id);

    setPendingJobIds((p) => p.filter(id => id !== job_id));

    setItems((prevItems) => {
      const pendingItem = prevItems.find(it => it.kind === "swarm_pending" && it.job_ids.includes(job_id));
      const pendingObj = pendingItem && pendingItem.kind === "swarm_pending" ? pendingItem.objective : "";
      const finalObjective = d.objective || pendingObj || "";

      // Resolve matching swarm_pending chip
      const updated = prevItems.map((item) => {
        if (item.kind === "swarm_pending" && item.job_ids.includes(job_id)) {
          return { ...item, resolved: true };
        }
        return item;
      });

      // Check if we already have this swarm_result in updated (double check)
      const alreadyHasResult = updated.some(it => it.kind === "swarm_result" && it.job_id === job_id);
      if (alreadyHasResult) return updated;

      const res_obj = d.result || d;

      return [
        ...updated,
        {
          kind: "swarm_result" as const,
          job_id: job_id,
          applied: res_obj.applied,
          files: res_obj.files || [],
          summary: res_obj.summary || "",
          error: res_obj.error || null,
          objective: finalObjective
        }
      ];
    });
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
            res.results.forEach((evt) => {
              const anyEvt = evt as any;
              if (anyEvt.kind === "swarm_result" && anyEvt.data) {
                handleSwarmResult(anyEvt.data);
              } else if (anyEvt.kind === "pilot_resume") {
                // Background job finished while the session was idle. The backend
                // already extended history with the result + continuation; kick
                // off a keep-alive turn so the pilot continues without a prompt.
                resumeTriggerRef.current();
              } else if (anyEvt.kind === "distilled" && anyEvt.data) {
                const d = anyEvt.data;
                const parts: string[] = [];
                if (d.skill && d.skill.status === "proposed") {
                  const { name } = d.skill;
                  parts.push(`proposed 1 skill${name ? ` ("${name}")` : ""}`);
                }
                if (d.rules) {
                  const pCount = d.rules.proposed?.length || 0;
                  if (pCount > 0) {
                    parts.push(`proposed ${pCount} rule${pCount === 1 ? "" : "s"}`);
                  }
                }
                if (parts.length > 0) {
                  const notice = `Self-learning: ${parts.join(", ")} - review in Skills tab`;
                  setDistillNotice(notice);
                  setTimeout(() => setDistillNotice((cur) => (cur === notice ? null : cur)), 8000);
                }
              } else if (anyEvt.kind === "wiki_prepared" && anyEvt.data) {
                const d = anyEvt.data;
                const pages = d.pages || [];
                if (pages.length > 0) {
                  if (d.auto_ingested) {
                    setDistillNotice(`Wiki: ${pages.length} page${pages.length === 1 ? "" : "s"} auto-ingested (local orchestration)`);
                    const notice = `Wiki: ${pages.length} page${pages.length === 1 ? "" : "s"} auto-ingested (local orchestration)`;
                    setTimeout(() => setDistillNotice((cur) => (cur === notice ? null : cur)), 8000);
                  } else {
                    setWikiPrepared({ pages, autoIngested: false });
                  }
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
  const appendStreamingText = (chunk: string) => {
    if (!chunk) return;
    setItems((p) => {
      const lastIdx = p.length - 1;
      if (lastIdx >= 0 && p[lastIdx].kind === "msg") {
        const lastMsg = p[lastIdx] as { kind: "msg"; msg: Msg };
        if (lastMsg.msg.role === "assistant" && lastMsg.msg.streaming) {
          const updated = [...p];
          updated[lastIdx] = { kind: "msg", msg: { ...lastMsg.msg, text: lastMsg.msg.text + chunk } };
          return updated;
        }
      }
      return [...p, { kind: "msg", msg: { role: "assistant", text: chunk, streaming: true, isPlan: planTurnRef.current } }];
    });
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
    // Reveal speed. A fixed live cadence (3 chars/frame ~= 180 cps) falls
    // arbitrarily far behind a fast agentic worker, so the visible text lags
    // seconds-to-minutes behind reality until the final flush -- defeating the
    // "live" stream. Make the live slice scale with the pending backlog: 3
    // chars/frame keeps a smooth, readable floor when the buffer is small, but
    // as it grows we drain proportionally faster so cadence stays smooth and
    // never lags arbitrarily. On done, drain a larger slice to finish promptly.
    const perFrame = typeDoneRef.current
      ? Math.max(12, Math.ceil(buf.length / 4))
      : Math.max(3, Math.ceil(buf.length / 8));
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

  const executeSend = (msg: string, useAuto: boolean, usePlan: boolean = false, resume: boolean = false) => {
    planTurnRef.current = usePlan;
    const imgsToSend = resume ? [] : [...attachedImages];
    const imgPaths = imgsToSend.map((img) => img.path);
    if (!resume) {
      // A resume turn carries no new user message -- the pilot is continuing off
      // a finished background job, so we don't add a user bubble or send images.
      setAttachedImages([]);
      setItems((p) => [...p, { kind: "msg", msg: { role: "user", text: msg, images: imgsToSend } }]);
    }
    setStatus("thinking");
    const streamer = resume
      ? (cb: any, done: any, err: any) => api.resume(cb, done, err)
      : useAuto
      ? (cb: any, done: any, err: any) => api.auto(msg, cb, done, err)
      : (cb: any, done: any, err: any) => api.chat(msg, cb, done, err, usePlan, imgPaths);
    cancelRef.current = streamer((ev: any) => {
      const d = ev.data || {};
      if (ev.kind === "compacting") {
        setCompactingStatus(d.message || "Summarizing chat context");
      } else if (ev.kind === "command_blocked") {
        setItems((p) => [...p, { kind: "command_blocked" as const, command: d.command || "", category: d.category || "", reason: d.reason || "", matched: d.matched || "" }]);
      } else if (ev.kind === "swarm_auth_failure") {
        // A provider rejected the API key. Surface it as a loud, persistent
        // banner so a dead/revoked key is never silently read as a generic
        // "completed without findings" degrade. Deduped by action id.
        setItems((p) => (
          p.some((it) => it.kind === "auth_failure" && it.id === d.id)
            ? p
            : [...p, { kind: "auth_failure" as const, message: d.message || "", id: d.id }]
        ));
      } else if (ev.kind === "wiki_prepared") {
        const pages = d.pages || [];
        if (pages.length > 0) {
          if (d.auto_ingested) {
            // Silent-auto mode already ingested -- just a quiet confirmation footnote.
            setDistillNotice(`Wiki: ${pages.length} page${pages.length === 1 ? "" : "s"} auto-ingested (local orchestration)`);
            const notice = `Wiki: ${pages.length} page${pages.length === 1 ? "" : "s"} auto-ingested (local orchestration)`;
            setTimeout(() => setDistillNotice((cur) => (cur === notice ? null : cur)), 8000);
          } else {
            // Prepare-and-approve: surface the pages for one-click ingest.
            setWikiPrepared({ pages, autoIngested: false });
          }
        }
      } else if (ev.kind === "codegraph_context") {
        setItems((p) => [...p, { kind: "codegraph_context" as const, symbols: d.symbols || 0, query: d.query || "" }]);
      } else if (ev.kind === "compaction") {
        setCompactingStatus(null);
        setItems((p) => [...p, { kind: "compaction" as const, before_tokens: d.before_tokens, after_tokens: d.after_tokens }]);
        window.dispatchEvent(new Event("harness-context-changed"));
      } else if (ev.kind === "thinking") {
        setCompactingStatus(null);
        setStatus("thinking");
        setItems((p) => [...p, { kind: "thinking", text: d.text || "" }]);
      } else if (ev.kind === "message_delta") {
        setCompactingStatus(null);
        setStatus("streaming");
        // Ensure a streaming bubble exists, then queue the text for the
        // typewriter loop (smooth cadence) instead of painting on arrival.
        setItems((p) => {
          const lastIdx = p.length - 1;
          if (lastIdx >= 0 && p[lastIdx].kind === "msg") {
            const lastMsg = p[lastIdx] as { kind: "msg"; msg: Msg };
            if (lastMsg.msg.role === "assistant" && lastMsg.msg.streaming) {
              return p;
            }
          }
          return [...p, { kind: "msg", msg: { role: "assistant", text: "", streaming: true, isPlan: planTurnRef.current } }];
        });
        typeBufRef.current += (d.text || "");
        startTypewriter();
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
          setItems((p) => {
            const lastIdx = p.length - 1;
            if (lastIdx >= 0 && p[lastIdx].kind === "msg") {
              const lastMsg = p[lastIdx] as { kind: "msg"; msg: Msg };
              if (lastMsg.msg.role === "assistant" && lastMsg.msg.streaming && lastMsg.msg.workerStream) {
                return p;
              }
            }
            return [...p, { kind: "msg", msg: { role: "assistant", text: "", streaming: true, workerStream: true, isPlan: planTurnRef.current } }];
          });
          typeBufRef.current += (d.text || "");
          startTypewriter();
        }
      } else if (ev.kind === "message") {
        setCompactingStatus(null);
        setStatus("thinking");
        // Drain any queued typed text before finalizing, so the bubble is whole.
        flushTypewriter();
        setItems((p0) => {
          // A pilot message must never adopt a worker's ephemeral stream: drop a
          // trailing worker-stream preview before finalizing the pilot's own text.
          const p = (p0.length > 0 && p0[p0.length - 1].kind === "msg" && (p0[p0.length - 1] as { kind: "msg"; msg: Msg }).msg.workerStream)
            ? p0.slice(0, -1) : p0;
          const lastIdx = p.length - 1;
          if (lastIdx >= 0 && p[lastIdx].kind === "msg") {
            const lastMsg = p[lastIdx] as { kind: "msg"; msg: Msg };
            if (lastMsg.msg.role === "assistant" && lastMsg.msg.streaming) {
              // Finalize the streaming bubble in place. If the final text is
              // empty, KEEP whatever already streamed into the bubble (don't wipe
              // visible narration) -- only drop the bubble when it never had any
              // text at all. This preserves the text -> tool -> text -> tool
              // thought chain instead of making intermediate prose vanish when a
              // step finalizes with an empty cleaned-say.
              const finalText = d.text || lastMsg.msg.text || "";
              if (!finalText.trim()) {
                return p.slice(0, lastIdx);
              }
              const updatedMsg = {
                ...lastMsg.msg,
                text: finalText,
                streaming: false
              };
              const updatedItems = [...p];
              updatedItems[lastIdx] = { kind: "msg", msg: updatedMsg };
              return deduplicateConsecutiveAssistantMessages(updatedItems);
            }
          }
          if (!d.text) {
            return p;
          }
          return deduplicateConsecutiveAssistantMessages([...p, { kind: "msg", msg: { role: "assistant", text: d.text || "", isPlan: planTurnRef.current } }]);
        });
      } else if (ev.kind === "action_start") {
        setCompactingStatus(null);
        setStatus("executing");
        setItems((p) => [...p, { kind: "card", card: {
          // Default tool cards to collapsed always: they used to mount open while
          // running and snap shut on action_result, which read as a flicker.
          // Start collapsed; the user can click to expand (onToggleCard).
          id: d.id, goal: d.goal, cwd: d.cwd, running: true, open: false, kind: d.kind } }]);
      } else if (ev.kind === "action_result") {
        setCompactingStatus(null);
        setStatus("thinking");
        // The swarm is done: its structured artifacts/summary land below. Retire
        // the ephemeral, height-capped worker-stream PREVIEW, but don't let the
        // reasoning vanish -- convert it into a COLLAPSED reasoning row (folded
        // into the activity group), the way Cursor keeps "Thought for Xs" after
        // the fact. A non-worker streaming bubble (the pilot's own narration) is
        // still finalized in place.
        flushTypewriter();
        setItems((p) => {
          const lastIdx = p.length - 1;
          if (lastIdx >= 0 && p[lastIdx].kind === "msg") {
            const lastMsg = p[lastIdx] as { kind: "msg"; msg: Msg };
            if (lastMsg.msg.role === "assistant" && lastMsg.msg.streaming) {
              const finalText = (lastMsg.msg.text || "").trim();
              if (lastMsg.msg.workerStream) {
                const withoutStream = p.slice(0, lastIdx);
                return finalText
                  ? [...withoutStream, { kind: "thinking" as const, text: finalText }]
                  : withoutStream;
              }
              if (!finalText) {
                return p.slice(0, lastIdx);
              }
              const updated = [...p];
              updated[lastIdx] = { kind: "msg", msg: { ...lastMsg.msg, streaming: false } };
              return updated;
            }
          }
          return p;
        });
        // Fallback: if the card carries an auth_failure but the dedicated
        // swarm_auth_failure event was missed, still raise the loud banner so a
        // dead key is never buried in a quiet "completed" card. Deduped by id.
        if (d.auth_failure) {
          setItems((p) => (
            p.some((it) => it.kind === "auth_failure" && it.id === d.id)
              ? p
              : [...p, { kind: "auth_failure" as const, message: d.auth_failure, id: d.id }]
          ));
        }
        setCard(d.id, { running: false, open: false, result: d });
        if (d.artifacts && !d.error) onArtifacts(d.artifacts);
        onJobChange();
        setItems((prev) => {
          const cardItem = prev.find((it) => it.kind === "card" && it.card.id === d.id);
          if (cardItem && cardItem.kind === "card" && cardItem.card.kind === "open_project" && !d.error) {
            window.dispatchEvent(new Event("harness-config-changed"));
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
        const parts: string[] = [];
        if (d.skill && d.skill.status === "proposed") {
          const { name } = d.skill;
          parts.push(`proposed 1 skill${name ? ` ("${name}")` : ""}`);
        }
        if (d.rules) {
          const pCount = d.rules.proposed?.length || 0;
          if (pCount > 0) {
            parts.push(`proposed ${pCount} rule${pCount === 1 ? "" : "s"}`);
          }
        }
        if (parts.length > 0) {
          const notice = `Self-learning: ${parts.join(", ")} - review in Skills tab`;
          setDistillNotice(notice);
          // Quiet footnote: auto-fade after 8s so it never lingers like a push notif.
          setTimeout(() => setDistillNotice((cur) => (cur === notice ? null : cur)), 8000);
        }
      } else if (ev.kind === "auto_halt") {
        setStatus("done");
        setItems((p) => [...p, { kind: "msg", msg: { role: "assistant", text: "HALT: " + (d.reason || "") } }]);
      } else if (ev.kind === "swarm_pending") {
        const job_ids = d.job_ids || [];
        setPendingJobIds((p) => [...p, ...job_ids]);
        setItems((p) => [
          ...p,
          {
            kind: "swarm_pending" as const,
            job_ids,
            objective: d.objective || "",
            resolved: false,
          },
        ]);
      } else if (ev.kind === "checkpoint") {
        setItems((p) => [...p, { kind: "checkpoint" as const, id: d.id, label: d.label, trigger: d.trigger }]);
        window.dispatchEvent(new Event("harness-repo-mutated"));
      } else if (ev.kind === "swarm_result") {
        handleSwarmResult(d);
      } else if (ev.kind === "pilot_resume") {
        // A background job finished and the backend injected a continuation into
        // history. Queue a keep-alive turn; it fires from this turn's onDone.
        resumeQueuedRef.current = true;
      } else if (ev.kind === "assistant_done") {
        setStatus("done");
        fetchContextUsage();
        // The backend derives a session title from the first user message
        // (set_title_if_default). Tell the sidebar to refetch so the auto-named
        // session shows up immediately, Cursor/Hermes-style, instead of staying
        // "New session" until reload.
        window.dispatchEvent(new Event("harness-config-changed"));
      } else if (ev.kind === "error") {
        setCompactingStatus(null);
        setStatus("error");
        setItems((p) => [...p, { kind: "msg", msg: { role: "assistant", text: "[error] " + (d.error || "") } }]);
      }
    }, () => { flushTypewriter(); setStatus("done"); cancelRef.current = null; setCompactingStatus(null); maybeRunQueuedResume(); },
       () => { flushTypewriter(); setStatus("error"); cancelRef.current = null; setCompactingStatus(null); maybeRunQueuedResume(); });
  };

  // Keep-alive driver: after a turn ends, if a background swarm finished while it
  // was running (resumeQueuedRef), fire a continuation turn so the pilot assesses
  // the result and takes the next step on its own -- no user prompt, no autopilot.
  // Chains naturally: each continuation can dispatch more work whose completion
  // queues the next resume, so the pilot "runs run runs" until the work is done.
  const maybeRunQueuedResume = () => {
    if (!resumeQueuedRef.current) return;
    // Still busy? Leave the flag set -- the next turn's onDone (or the poll) will
    // pick it up. Only clear it once we've actually committed to running.
    if (cancelRef.current) return;
    resumeQueuedRef.current = false;
    setTimeout(() => {
      if (cancelRef.current) { resumeQueuedRef.current = true; return; }
      executeSendRef.current("", false, false, true);
    }, 60);
  };

  // A pilot_resume can also arrive via the swarm-results poll while the session is
  // idle (the common background-job case). Trigger a continuation immediately.
  const triggerResume = () => {
    if (cancelRef.current) { resumeQueuedRef.current = true; return; }
    executeSendRef.current("", false, false, true);
  };
  resumeTriggerRef.current = triggerResume;

  const send = () => {
    const msg = input.trim(); if (!msg) return;

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
        setItems((p) => [...p, { kind: "thinking", text: "Compacting session context on backend..." }]);
        api.compactSession()
          .then((res) => {
            setStatus("done");
            setItems((p) => [
              ...p,
              {
                kind: "msg",
                msg: {
                  role: "assistant",
                  text: "System Note: Manual context compaction complete (" + res.before_tokens + " -> " + res.after_tokens + " tokens)."
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
                  text: "[error] Compaction failed: " + (err.message || err)
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
        const helpText = "Available Slash Commands:\n\n" +
          allSlashCommands.map(s => `* \`${s.cmd}\` - ${s.desc}`).join("\n") +
          "\n\nType @ to list and mention files in your message context.";
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

      const isBuiltIn = SLASH_COMMANDS.some(s => s.cmd === cmd);
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
                    text: "[error] Render failed: " + (err.message || err)
                  }
                }
              ]);
            });
          return;
        }
      }
    }

    // ERGONOMICS CHOICE: Loaded edit back into composer. On send, we send as a new turn
    // (appending as a fresh turn) to prevent corrupting the backend session history
    // while providing a seamless correction/resubmission flow.
    setEditingIndex(null);

    const isBusy = status === "thinking" || status === "executing" || status === "streaming";

    if (isBusy) {
      setInput("");
      api.steerSession(msg)
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
                text: "[error] Steer failed: " + (err.message || err)
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
    cancelRef.current?.();
    cancelRef.current = null;
    flushTypewriter();
    setStatus("idle");
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
    <main
      className="flex flex-col h-full min-w-0 bg-bg"
      style={{
        // Subtle depth: a faint cool highlight pooled near the top that fades
        // into the base, so the canvas reads as a lit surface rather than a flat
        // greyscale slab. Very low contrast on purpose.
        backgroundImage:
          "radial-gradient(120% 80% at 50% -10%, rgba(139,150,196,0.06), rgba(139,150,196,0) 60%)",
      }}
    >
      <header className="flex items-center justify-between px-6 border-b border-edge"
         style={{ paddingTop: 12, paddingBottom: 10, WebkitAppRegion: "drag" } as React.CSSProperties}>
        <span className="flex items-baseline gap-1.5 select-none" style={{ WebkitAppRegion: "no-drag" } as React.CSSProperties}>
          <span className="font-semibold text-[13px] text-txt/90 tracking-tight">Marionette</span>
          <span className="text-faint/70 text-[10px] font-normal">—</span>
          <span className="text-muted/80 text-[10px] font-medium tracking-wide uppercase">The Puppetmaster Harness</span>
        </span>
        <div style={{ WebkitAppRegion: "no-drag" } as React.CSSProperties}><StatusPill status={status} /></div>
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

      {activeTab === "chat" ? (
        <>
          <div ref={feedRef} className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-6 py-6 flex flex-col gap-1">
          {items.length === 0 && (
            <div className="text-muted text-[13px] mt-32 text-center leading-relaxed">
              Message the pilot. It plans, investigates via swarms, and explains.
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
                    setTimeout(() => setDistillNotice((cur) => (cur === notice ? null : cur)), 6000);
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
            {/* Editing indicator */}
            {editingIndex !== null && (
              <div className="flex items-center justify-between px-3.5 py-1.5 bg-panel border-b border-edge text-[11.5px] text-accent select-none rounded-t-2xl">
                <span className="flex items-center gap-1.5">
                  <Pencil size={11} />
                  <span>Editing message #{editingIndex + 1}</span>
                </span>
                <button
                  onClick={() => {
                    setEditingIndex(null);
                    setInput("");
                  }}
                  className="text-faint hover:text-muted transition font-medium text-[10px] px-1.5 py-0.5 rounded border border-edge bg-panel2/50 hover:bg-panel2"
                >
                  Cancel
                </button>
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
            {mentionSearch !== null && (filteredFiles.length > 0 || symbolResults.length > 0) && (
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
              className="w-full bg-transparent px-3 pt-2.5 pb-1 text-[0.8125rem] resize-none focus:outline-none overflow-y-auto placeholder:text-faint" />
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
              {status === "thinking" || status === "executing" || status === "streaming"
                ? <button onClick={stop} className="px-2 h-[20px] rounded-md bg-risk/15 text-risk text-[10.5px] font-medium flex items-center gap-1"><Square size={9} />Stop</button>
                : <button onClick={send} disabled={!input.trim() && attachedImages.length === 0}
                    className="px-2.5 h-[20px] rounded-md bg-accent text-black/90 text-[10.5px] font-semibold flex items-center gap-1 hover:brightness-110 disabled:opacity-40 disabled:cursor-default transition">
                    <Send size={9} />{auto ? "Run" : plan ? "Plan" : "Send"}</button>}
            </div>
          </div>
        </div>
      </div>
    </>
  ) : (
    <FileEditorPane
      path={activeTab}
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


function WorkspaceChip() {
  const [ws, setWs] = useState<{ repo: string; branch: string; recents?: string[] } | null>(null);
  const [open, setOpen] = useState(false);
  const refresh = () => api.getWorkspace().then((w) => setWs(w as any)).catch(() => {});
  useEffect(() => {
    refresh();
    const h = () => refresh();
    window.addEventListener("harness-config-changed", h);
    return () => window.removeEventListener("harness-config-changed", h);
  }, []);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    const onClick = () => setOpen(false);
    window.addEventListener("keydown", onKey);
    window.addEventListener("click", onClick);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("click", onClick); };
  }, [open]);

  const openPath = async (p: string) => {
    setOpen(false);
    try {
      const res = await api.openWorkspace(p);
      if ((res as any).ok) { refresh(); window.dispatchEvent(new Event("harness-config-changed")); }
    } catch { /* ignore */ }
  };
  const browse = async () => {
    const picked = await pickFolder();
    if (picked) await openPath(picked);
  };
  const base = (p: string) => p ? p.replace(/\/+$/, "").split("/").pop() || p : "";
  const name = ws?.repo ? base(ws.repo) : "No folder";
  const recents = (ws?.recents || []).filter((r) => r !== ws?.repo);

  return (
    <div className="flex items-center gap-1.5 px-1 pb-1.5 text-[11px] relative">
      <button
        onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}
        className="flex items-center gap-1 text-muted hover:text-txt transition rounded px-1 py-0.5 hover:bg-panel2/60">
        <Folder size={11} className="text-faint" />
        <span className="font-medium">{name}</span>
        <ChevronDown size={11} className="text-faint" />
      </button>
      {ws?.branch ? <span className="text-faint flex items-center gap-0.5"><GitBranch size={10} />{ws.branch}</span> : null}
      <span className="text-faint/70">Local</span>
      {open && (
        <div onClick={(e) => e.stopPropagation()}
          className="absolute bottom-full left-0 mb-1 w-64 bg-panel border border-edge rounded-lg shadow-xl shadow-black/40 py-1 z-50">
          {recents.length > 0 && (
            <>
              <div className="text-[9px] uppercase tracking-wider text-faint px-3 py-1">Recents</div>
              {recents.map((r) => (
                <button key={r} onClick={() => openPath(r)}
                  className="w-full text-left px-3 py-1.5 hover:bg-panel2 transition flex flex-col">
                  <span className="text-txt font-medium text-[11px]">{base(r)}</span>
                  <span className="text-faint text-[9px] font-mono truncate">{r}</span>
                </button>
              ))}
              <div className="border-t border-edge/50 my-1" />
            </>
          )}
          <button onClick={browse}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 transition flex items-center gap-2 text-txt text-[11px]">
            <Folder size={12} className="text-accent" /> Open Folder...
          </button>
        </div>
      )}
    </div>
  );
}

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

function getCardMeta(card: Card): string | null {
  if (card.running) return null;
  const parts: string[] = [];

  const duration = card.result?.duration_ms;
  if (typeof duration === "number") {
    parts.push(`${duration}ms`);
  }

  if (card.result?.error) {
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
}: {
  items: ( { kind: "card"; card: Card } | { kind: "thinking"; text: string } | { kind: "codegraph_context"; symbols: number; query: string } | { kind: "msg"; msg: Msg } )[];
  onToggleCard: (card: Card) => void;
}) {
  // Compact-by-default: a whole investigation collapses to one summary line
  // (Cursor-style "Investigated -- N steps"), expandable to the full list.
  const [open, setOpen] = useState(false);

  const cards = items.filter((it) => it.kind === "card") as { kind: "card"; card: Card }[];
  const cgItems = items.filter((it) => it.kind === "codegraph_context") as { kind: "codegraph_context"; symbols: number; query: string }[];
  const actionCount = cards.length;
  const anyRunning = cards.some((c) => c.card.running);
  const narrationMsgs = items.filter(
    (it) => it.kind === "msg" && (it as { kind: "msg"; msg: Msg }).msg.text.trim()
  ) as { kind: "msg"; msg: Msg }[];
  const thinkingItems = items.filter(
    (it) => it.kind === "thinking" && (it as { kind: "thinking"; text: string }).text.trim()
  ) as { kind: "thinking"; text: string }[];

  // A group with NO tool actions, no narration AND no reasoning (just a lone
  // CodeGraph chip from the per-step auto-injection) would render a misleading
  // "0 steps" box -- suppress it. But folded intermediate narration OR a reasoning
  // trace must still show (collapsed), so reasoning never silently vanishes from
  // the step list the way it used to.
  if (actionCount === 0 && narrationMsgs.length === 0 && thinkingItems.length === 0) {
    return null;
  }

  const narrationPreview = narrationMsgs.length
    ? narrationMsgs[narrationMsgs.length - 1].msg.text.trim().split("\n", 1)[0]
    : (thinkingItems.length
        ? thinkingItems[thinkingItems.length - 1].text.trim().split("\n", 1)[0]
        : "");

  // Build a tight one-line summary of what kinds of actions ran.
  const kindCounts: Record<string, number> = {};
  for (const c of cards) {
    const k = c.card.kind || "action";
    kindCounts[k] = (kindCounts[k] || 0) + 1;
  }
  const kindLabel = (k: string) => k.replace(/_/g, " ");
  const kindSummary = Object.entries(kindCounts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3)
    .map(([k, n]) => `${n} ${kindLabel(k)}${n === 1 ? "" : "s"}`)
    .join(", ");

  const renderInner = (it: typeof items[number], idx: number) => {
    if (it.kind === "card") return <ActionCard key={idx} card={it.card} onToggle={() => onToggleCard(it.card)} />;
    if (it.kind === "thinking") return <ThinkingBlock key={idx} text={it.text} />;
    if (it.kind === "msg") {
      // Per-step micro-narration: small muted line inside the box.
      if (!it.msg.text || !it.msg.text.trim()) return null;
      return (
        <div key={idx} className="text-[12px] text-muted/90 py-0.5 leading-relaxed whitespace-pre-wrap">
          {it.msg.text}
        </div>
      );
    }
    if (it.kind === "codegraph_context") {
      return (
        <div key={idx} className="flex items-center gap-1.5 py-0.5 text-[10px] text-faint/70 select-none" title={it.query ? `CodeGraph consulted for: ${it.query}` : "CodeGraph consulted"}>
          <Share2 size={9} className="text-faint/60" />
          <span>CodeGraph consulted{it.symbols > 0 ? ` -- ${it.symbols} symbols` : ""}</span>
        </div>
      );
    }
    return null;
  };

  // Tiny groups (1-2 actions, no codegraph noise, no narration) render inline --
  // collapsing them would add a click for no benefit.
  const hasMsg = items.some((it) => it.kind === "msg" && (it as { kind: "msg"; msg: Msg }).msg.text.trim());
  if (actionCount <= 2 && cgItems.length === 0 && !hasMsg) {
    return (
      <div className="flex flex-col gap-0.5 pl-3 border-l-2 border-edge/40 my-1 w-full">
        {items.map(renderInner)}
      </div>
    );
  }

  return (
    <div className="my-1 w-full">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-panel2/20 border border-edge/30 hover:bg-panel2/40 transition text-[11px] text-muted w-fit select-none"
      >
        {open ? <ChevronDown size={11} className="text-faint/70" /> : <ChevronRight size={11} className="text-faint/70" />}
        {anyRunning ? <Loader2 size={11} className="animate-spin text-faint" /> : <Share2 size={10} className="text-faint/70" />}
        <span className="text-txt/70 font-medium tracking-tight">
          {actionCount > 0 ? (anyRunning ? "Investigating" : "Investigated") : "Thought"}
        </span>
        <span className="text-faint truncate max-w-[46ch] normal-case">
          {actionCount > 0
            ? `${actionCount} step${actionCount === 1 ? "" : "s"}${kindSummary ? ` -- ${kindSummary}` : ""}`
            : narrationPreview}
        </span>
        {cgItems.length > 0 && (
          <span className="ml-0.5 text-faint/70">+ CodeGraph</span>
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


function ThinkingBlock({ text }: { text: string }) {
  // Cursor/Hermes-style compression: reasoning collapses to a single header line
  // by default (a faint preview of the first line hints at the content), and
  // expands into a height-capped, scrollable window rather than dumping its full
  // height inline. Unbounded inline reasoning is what used to blow up the window
  // and bury the actual answer, so the compact default is the legibility win.
  const [expanded, setExpanded] = useState(false);

  if (!text || !text.trim()) {
    return null;
  }

  const preview = text.trim().split("\n", 1)[0].slice(0, 160);

  return (
    <div className="flex flex-col w-full py-0.5 min-w-0">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="flex items-center gap-1 text-faint/70 hover:text-muted transition font-mono text-[10px] text-left w-full min-w-0 select-none uppercase tracking-wide"
        aria-expanded={expanded}
        title={expanded ? "Collapse reasoning" : "Expand reasoning"}
      >
        {expanded ? <ChevronDown size={9} className="text-faint/70 shrink-0" /> : <ChevronRight size={9} className="text-faint/70 shrink-0" />}
        <span className="shrink-0">reasoning</span>
        {!expanded && (
          <span className="ml-1 truncate normal-case tracking-normal font-sans text-faint/50">{preview}</span>
        )}
      </button>
      {expanded && (
        <div className="mt-0.5 pl-2.5 ml-1 border-l-2 border-edge/40 overflow-y-auto overscroll-contain text-faint/85 text-[11px] whitespace-pre-wrap leading-[1.65] max-w-[92%] max-h-[34dvh]">
          {text}
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

function FencedCodeBlock({ className, children, ...props }: any) {
  const [copied, setCopied] = useState(false);
  const codeText = nodeToText(children).replace(/\n$/, "");
  
  const handleCopy = () => {
    navigator.clipboard.writeText(codeText);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };
  
  return (
    <div className="relative group/code my-2">
      <code className={`${className || ""} block bg-panel/80 border border-accent/20 rounded-md p-3 pr-10 overflow-x-auto font-mono text-[0.719rem] leading-[1.55] text-txt/90`} {...props}>
        {children}
      </code>
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
// opens in the editor, and everything else (mailto, anchors) falls through to
// default behavior.
function isExternalUrl(href: string): boolean {
  return /^https?:\/\//i.test(href);
}

function looksLikeFilePath(href: string): boolean {
  if (!href) return false;
  if (/^(https?|mailto|tel|data):/i.test(href) || href.startsWith("#")) return false;
  const clean = href.replace(/^file:\/\//, "");
  // A slash, or a dotted extension (optionally trailed by :line[:col]).
  return /[\\/]/.test(clean) || /\.\w{1,8}(:\d+){0,2}$/.test(clean);
}

function openMarkdownHref(href: string, e: React.MouseEvent): void {
  if (!href) return;
  if (isExternalUrl(href)) {
    e.preventDefault();
    // Stash the URL so a BrowserPane that mounts *because* of the focus below can
    // consume it -- the open-url event alone races the pane's listener setup.
    (window as any).__pmPendingBrowserUrl = href;
    window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "browser" }));
    window.dispatchEvent(new CustomEvent("harness-open-url", { detail: { url: href } }));
    return;
  }
  if (looksLikeFilePath(href)) {
    e.preventDefault();
    const path = href.replace(/^file:\/\//, "").replace(/:(\d+)(:\d+)?$/, "");
    window.dispatchEvent(new CustomEvent("harness-open-file", { detail: { path } }));
  }
}

function Markdown({ text }: { text: string }) {
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
            onClick={() => { if (src && isExternalUrl(src)) openMarkdownHref(src, { preventDefault() {} } as React.MouseEvent); }}
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
      {text}
    </ReactMarkdown>
  );
}

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
                  <div key={idx} className="relative w-11 h-11 rounded overflow-hidden border border-edge bg-panel flex-shrink-0">
                    <img
                      src={img.previewUrl}
                      alt={img.name}
                      onClick={() => onImageClick?.(img.previewUrl)}
                      className="w-full h-full object-cover rounded cursor-pointer hover:opacity-85 transition-opacity"
                    />
                  </div>
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
  const toolName = card.kind || "swarm";
  const meta = getCardMeta(card);

  // Hermes tool-row spec: monochrome. Success is SILENT (no glyph -- the row
  // reads as done without a checkmark); only running (spinner) and error
  // (destructive) carry a leading glyph. Tool name is secondary grey, not
  // colored/bold. Meta is the faintest tertiary tier.
  const isErr = !!card.result?.error;
  return (
    <div className="flex flex-col w-full select-none">
      <button
        onClick={onToggle}
        className="flex items-center justify-between w-full py-1 px-2 rounded-md hover:bg-panel2/40 text-left text-[11px] font-mono group transition-colors"
      >
        <div className="flex items-center gap-2 min-w-0 flex-1">
          <div className="flex items-center justify-center w-3.5 h-3.5 shrink-0">
            {card.running ? (
              <Loader2 size={11} className="animate-spin text-faint/70" />
            ) : isErr ? (
              <span className="w-1.5 h-1.5 rounded-full bg-risk/70" />
            ) : null}
          </div>
          <span className={`font-medium shrink-0 ${isErr ? "text-risk/85" : "text-txt/70"}`}>
            {toolName}
          </span>
          <span className="text-faint/85 truncate max-w-[70%] font-normal" title={card.goal}>
            {card.goal}
          </span>
        </div>

        <div className="flex items-center gap-2 shrink-0 text-[10px] text-faint/60 select-none tabular-nums">
          {meta && <span>{meta}</span>}
          <ChevronRight
            size={11}
            className={`text-faint/40 group-hover:text-faint/70 transition shrink-0 ${
              card.open ? "rotate-90" : ""
            }`}
          />
        </div>
      </button>

      {card.open && (
        <div className="mt-1 ml-5 pl-3 border-l border-edge py-1.5 pr-3 bg-panel2/40 rounded-r-md text-[11px] max-w-full text-txt/90 space-y-1">
          <KV k="goal" v={card.goal} />
          {card.cwd && <KV k="cwd" v={card.cwd} />}
          {card.result?.error && <div className="text-risk mt-1 font-sans">error: {card.result.error}</div>}
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
const KV = ({ k, v }: { k: string; v: string }) => (
  <div className="flex gap-2 mb-0.5"><span className="text-muted w-11 shrink-0">{k}</span><span className="break-all">{v}</span></div>
);

function StatusPill({ status }: { status: string }) {
  const m: Record<string, string> = {
    idle: "text-faint", thinking: "text-accent", executing: "text-warn",
    done: "text-good", error: "text-risk",
  };
  const dot: Record<string, string> = {
    idle: "bg-faint", thinking: "bg-accent animate-pulse", executing: "bg-warn animate-pulse",
    done: "bg-good", error: "bg-risk",
  };
  return <span className={`text-[10.5px] flex items-center gap-1.5 ${m[status] || m.idle}`}>
    <span className={`w-1.5 h-1.5 rounded-full ${dot[status] || dot.idle}`} />{status}</span>;
}

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
          ? <span className="text-faint shrink-0 tabular-nums">{files.length} file{files.length === 1 ? "" : "s"}</span>
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
                <span key={f} className="text-[9px] font-mono text-muted bg-panel2/60 border border-edge/50 rounded px-1 py-0.5">
                  {f}
                </span>
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
