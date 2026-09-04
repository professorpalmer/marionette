/**
 * Live busy-turn progress for the transcript footer and header pill.
 *
 * A long diagnose used to sit on "running..." while tools burned tokens
 * invisibly. These helpers derive a scannable label from the same cards the
 * activity fold already knows about -- pure, so vitest can pin the contract.
 */

import { waitHintForBusyProgress } from "./composerWaitHint";

export type BusyStatus = "idle" | "thinking" | "executing" | "done" | "error" | "streaming" | string;

export type TurnCard = {
  id: string;
  goal: string;
  kind: string;
  running: boolean;
  goals?: string[];
  result?: { job_id?: string | null; status?: string | null; artifacts?: Array<{ headline?: string }> } | null;
  actions?: Array<{ status?: string; kind?: string; goal?: string }>;
};

export type TurnItem =
  | { kind: "msg"; msg: { role: string; text: string; streaming?: boolean } }
  | { kind: "card"; card: TurnCard }
  | { kind: "tool_prep"; name: string }
  | { kind: "thinking"; text: string; streaming?: boolean }
  | { kind: string; [key: string]: unknown };

export type BusyProgress = {
  /** Short phase word: waiting / thinking / running / streaming */
  phase: string;
  /** Full scannable line for the transcript footer */
  label: string;
  /** Compact label for the header StatusPill */
  pill: string;
  step: number;
  runningGoal: string;
  runningKind: string;
};

/** Normalize Cursor ACP / stream-json kinds (readToolCall → read_file family). */
export function normalizeToolKind(kind: string): string {
  let k = (kind || "").trim();
  if (!k) return "";
  if (k.endsWith("ToolCall")) k = k.slice(0, -"ToolCall".length);
  k = k
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase()
    .replace(/-/g, "_")
    .replace(/\s+/g, "_")
    .replace(/^_+|_+$/g, "");
  if (k === "tool" || k === "function" || k === "unknown" || k === "other" || k === "tool_call") {
    return "";
  }
  // ACP ToolKind → Marionette row families.
  if (k === "execute" || k === "shell" || k === "bash") return "run_command";
  if (k === "read") return "read_file";
  if (k === "edit" || k === "write" || k === "delete" || k === "move") {
    return k === "write" ? "write_file" : k === "edit" ? "edit_file" : k;
  }
  if (k === "fetch") return "web_fetch";
  return k;
}

/** Cursor-style row label for a tool card (Read / Grep / Run / Query wiki). */
export function toolRowLabel(kind: string): string {
  const k = normalizeToolKind(kind) || (kind || "").toLowerCase().replace(/-/g, "_").trim();
  const known: Record<string, string> = {
    read_file: "Read",
    read: "Read",
    write_file: "Write",
    edit_file: "Edit",
    apply_hashline: "Edit",
    hash_edit: "Edit",
    grep: "Grep",
    search: "Search",
    glob: "Glob",
    run_command: "Run",
    run_terminal: "Run",
    execute: "Run",
    shell: "Run",
    query_wiki: "Query wiki",
    wiki: "Query wiki",
    web_fetch: "Fetch",
    fetch: "Fetch",
    codegraph_search: "Query",
    codegraph_context: "Query",
    codegraph: "Query",
    call_mcp: "MCP",
    mcp: "MCP",
    get_mcp_tools: "MCP",
    list_mcp_resources: "MCP",
    read_mcp_resource: "MCP",
    mcp_auth: "MCP",
    view_image: "View",
    open_project: "Open",
    relocate_session: "Relocate",
    delete: "Delete",
    move: "Move",
  };
  if (known[k]) return known[k];
  if (!k) return "Tool";
  return k
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/** True when goal is just a restatement of the kind label ("read file", "tool"). */
export function isRedundantToolGoal(kind: string, goal: string): boolean {
  const g = (goal || "").trim().toLowerCase().replace(/_/g, " ").replace(/\s+/g, " ");
  // Model-junk placeholders sometimes land as the Run goal ("null | tail -40").
  const firstTok = g.split(/[\s|]+/, 1)[0] || "";
  if (!g || g === "tool" || g === "function" || g === "unknown"
    || firstTok === "null" || firstTok === "none" || firstTok === "undefined") {
    return true;
  }
  const focus = toolFocusPhrase(kind).toLowerCase().replace(/_/g, " ").replace(/\s+/g, " ");
  const label = toolRowLabel(kind).toLowerCase();
  if (g === focus || g === label) return true;
  // "read file" vs kind read_file / label Read
  const kindPhrase = (normalizeToolKind(kind) || kind || "")
    .toLowerCase()
    .replace(/_/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return !!kindPhrase && g === kindPhrase;
}

/** Primary CLI-style input key for an expanded tool card (path / command / query / …). */
export function toolInputFieldKey(kind: string): string {
  const k = normalizeToolKind(kind);
  if (
    k === "read_file"
    || k === "write_file"
    || k === "edit_file"
    || k === "hash_edit"
    || k === "view_image"
    || k === "list_dir"
    || k === "open_project"
    || k === "delete_file"
    || k === "create_file"
    || k === "read"
  ) {
    return "path";
  }
  if (k === "run_command" || k === "bash" || k === "shell") return "command";
  if (
    k === "web_search"
    || k === "search_codegraph"
    || k === "search_files"
    || k === "search_state"
    || k === "search_tools"
    || k === "query_wiki"
    || k.includes("codegraph")
    || k.startsWith("search_")
  ) {
    return "query";
  }
  if (k === "web_fetch" || k === "read_pdf" || k === "fetch") return "url";
  return "goal";
}

type CardInputSource = {
  kind?: string;
  goal?: string;
  goals?: string[];
  actions?: Array<{ goal?: string }>;
  result?: {
    artifacts?: Array<{ headline?: string; type?: string }>;
  } | null;
};

/**
 * Resolve the real CLI/syntax input for a tool card. Prefer live goal(s), then
 * nested worker goals, then recover from artifact headlines when the stream
 * never stamped a path/query (empty "goal" dropdown).
 */
export function resolveCardCliInput(card: CardInputSource): string {
  const kind = card.kind || "";
  const push = (out: string[], raw: string) => {
    const t = String(raw || "").trim();
    // isRedundantToolGoal also drops literal null/none/undefined placeholders.
    if (t && !isRedundantToolGoal(kind, t)) out.push(t);
  };
  const candidates: string[] = [];
  push(candidates, card.goal || "");
  for (const g of card.goals || []) push(candidates, String(g || ""));
  for (const a of card.actions || []) push(candidates, a.goal || "");
  if (candidates[0]) return candidates[0];

  for (const art of card.result?.artifacts || []) {
    const h = String(art.headline || "").trim();
    if (!h) continue;
    const labeled = h.match(
      /^(?:CodeGraph search|Search|Grep|Read|Wrote|Write|Edit|Ran|Run|Fetch):\s*(.+)$/i,
    );
    if (labeled?.[1]?.trim()) return labeled[1].trim();
    const pathish = h.match(
      /^(?:Read|Wrote|Writing|Edited)\s+(?:\d+\s+\w+\s+)?(?:to\s+)?(.+)$/i,
    );
    if (pathish?.[1]?.trim()) return pathish[1].trim();
    if (/[\\/]|\.\w{1,8}\b/.test(h) && h.length < 400) return h;
  }
  return "";
}

/** True when a card is owned by a durable dispatch job (command / batch / swarm). */
export function cardHasDurableJob(card: {
  result?: {
    job_id?: string | null;
    status?: string | null;
    terminal_receipt?: unknown;
  } | null;
}): boolean {
  const jobId = String(card.result?.job_id || "").trim();
  if (jobId) return true;
  return Boolean(
    card.result
    && typeof card.result === "object"
    && (card.result as { terminal_receipt?: unknown }).terminal_receipt,
  );
}

function resultLooksTerminal(result: {
  job_id?: string | null;
  status?: string | null;
  terminal_receipt?: unknown;
} | null | undefined): boolean {
  if (!result) return false;
  // Explicit terminal receipt always settles the card spinner.
  if ((result as { terminal_receipt?: unknown }).terminal_receipt) return true;
  const jobId = String(result.job_id || "").trim();
  if (!jobId) return true; // non-dispatch tool outcome (read/search/…)
  const s = String(result.status || "").trim().toLowerCase();
  return (
    s === "complete"
    || s === "completed"
    || s === "done"
    || s === "ok"
    || s === "success"
    || s === "failed"
    || s === "error"
    || s === "cancelled"
    || s === "canceled"
    || s === "timeout"
    || s === "truncated"
    || s === "interrupted"
    || s === "stalled"
  );
}

/**
 * Whether a card should still show a spinner / keep Investigating open.
 * Clears stale ``running`` when a terminal result body is already present
 * (orphaned prep/pending still count as running until reconcile settles them).
 */
export function cardEffectivelyRunning(card: {
  running?: boolean;
  result?: {
    job_id?: string | null;
    status?: string | null;
    terminal_receipt?: unknown;
  } | null;
  actions?: Array<{ status?: string }>;
}): boolean {
  const settled = resultLooksTerminal(card.result);
  if (settled) return false;
  if (card.running) return true;
  return (card.actions || []).some((a) => a.status === "running");
}

/** Soft focus phrase for live headlines ("run command", "read file"). */
export function toolFocusPhrase(kind: string): string {
  const label = toolRowLabel(kind);
  if (!label || label === "Tool") {
    const fallback = normalizeToolKind(kind) || (kind || "").replace(/_/g, " ").trim();
    return fallback.replace(/_/g, " ");
  }
  return label.toLowerCase();
}

type ExplorationBucket =
  | "files"
  | "searches"
  | "commands"
  | "edits"
  | "wiki"
  | "fetches"
  | "other";

/** Bucket a tool kind into Cursor-style exploration categories. */
export function explorationBucket(kind: string): ExplorationBucket {
  const k = normalizeToolKind(kind) || (kind || "").toLowerCase().replace(/-/g, "_").trim();
  if (
    k === "read_file"
    || k === "read"
    || k === "view_image"
    || k === "open_project"
    || k.startsWith("read_")
  ) {
    return "files";
  }
  if (
    k === "write_file"
    || k === "edit_file"
    || k === "hash_edit"
    || k === "apply_hashline"
    || k === "delete"
    || k === "move"
    || k.startsWith("write_")
    || k.startsWith("edit_")
  ) {
    return "edits";
  }
  if (
    k === "grep"
    || k === "search"
    || k === "glob"
    || k.includes("grep")
    || k.includes("search")
    || k.includes("codegraph")
  ) {
    return "searches";
  }
  if (
    k === "run_command"
    || k === "run_terminal"
    || k === "execute"
    || k === "shell"
    || k.includes("command")
    || k.includes("terminal")
    || k.startsWith("run_")
  ) {
    return "commands";
  }
  if (k.includes("wiki")) return "wiki";
  if (k.includes("fetch") || k === "web_fetch") return "fetches";
  return "other";
}

const BUCKET_LABELS: Record<ExplorationBucket, [string, string]> = {
  files: ["file", "files"],
  searches: ["search", "searches"],
  commands: ["command", "commands"],
  edits: ["edit", "edits"],
  wiki: ["wiki query", "wiki queries"],
  fetches: ["fetch", "fetches"],
  other: ["step", "steps"],
};

const BUCKET_ORDER: ExplorationBucket[] = [
  "files",
  "searches",
  "commands",
  "edits",
  "wiki",
  "fetches",
  "other",
];

/** Read / search / wiki / fetch cards may collapse into one activity shelf. */
export function isExplorationShelfKind(kind: string): boolean {
  const b = explorationBucket(kind);
  return b === "files" || b === "searches" || b === "wiki" || b === "fetches";
}

export type ExplorationShelfRow<T> =
  | { kind: "item"; item: T; index: number }
  | { kind: "shelf"; items: T[]; indexes: number[] };

/**
 * Collapse consecutive exploration tool cards into one shelf. Commands and
 * edits stay individual so a Run/Write never hides inside a Read group.
 */
/** First card id — appending more exploration cards must not remount the shelf. */
export function explorationShelfAnchorId(
  cardIds: Array<string | undefined | null>,
): string {
  for (const id of cardIds) {
    const text = String(id || "").trim();
    if (text) return `expl-shelf-${text}`;
  }
  return "expl-shelf";
}

export function partitionExplorationShelf<T>(
  items: T[],
  cardKind: (item: T) => string | null,
): ExplorationShelfRow<T>[] {
  const out: ExplorationShelfRow<T>[] = [];
  let i = 0;
  while (i < items.length) {
    const kind = cardKind(items[i]);
    if (kind && isExplorationShelfKind(kind)) {
      const start = i;
      const group: T[] = [];
      const indexes: number[] = [];
      while (i < items.length) {
        const nextKind = cardKind(items[i]);
        if (!nextKind || !isExplorationShelfKind(nextKind)) break;
        group.push(items[i]);
        indexes.push(i);
        i += 1;
      }
      if (group.length >= 2) {
        out.push({ kind: "shelf", items: group, indexes });
      } else {
        out.push({ kind: "item", item: group[0], index: start });
      }
      continue;
    }
    out.push({ kind: "item", item: items[i], index: i });
    i += 1;
  }
  return out;
}

/** Aggregate card kinds into "3 files, 1 search" (Cursor explored summary). */
export function aggregateExplorationSummary(kinds: string[]): string {
  const counts: Partial<Record<ExplorationBucket, number>> = {};
  for (const kind of kinds) {
    const b = explorationBucket(kind);
    counts[b] = (counts[b] || 0) + 1;
  }
  const parts: string[] = [];
  for (const b of BUCKET_ORDER) {
    const n = counts[b];
    if (!n) continue;
    const [one, many] = BUCKET_LABELS[b];
    parts.push(`${n} ${n === 1 ? one : many}`);
  }
  return parts.join(", ");
}

/** Run / shell / terminal tool cards nest under a Ran N command fold.
 *  Also treat edit/other actionable tools as Ran rows so nested Thought can
 *  live between tool steps (Cursor stacked folds). Exploration shelves still
 *  win for consecutive file/search reads via partition order.
 */
export function isCommandCardKind(kind: string): boolean {
  const k = (kind || "").trim();
  if (!k) return false;
  if (isExplorationShelfKind(k)) return false;
  return true;
}

/** Compact duration for Worked for / Thought chrome (`23s`, `6m`, `1m 5s`). */
export function formatFoldDuration(ms: number): string {
  return formatBusyElapsed(ms);
}

/** Sealed outer activity fold — replaces Explored once the turn seals. */
export function workedForLabel(durationMs?: number | null): string {
  // No timer → hide the whole Worked for row (not a bare label).
  if (durationMs == null || !Number.isFinite(durationMs) || durationMs <= 0) {
    return "";
  }
  // Visible chrome with a real elapsed always shows at least 1s (never 0s).
  const shown = Math.max(durationMs, 1000);
  const label = formatFoldDuration(shown);
  return label ? `Worked for ${label}` : "";
}

/**
 * Spoken-prose empty fallback (`clean_say` / Bubble `cleanAssistantText`).
 * Fold chrome must never adopt this string as a live/sealed title.
 */
export function isWorkingEllipsisFallback(text: string): boolean {
  return /^Working\.\.\.?$/i.test(String(text || "").trim());
}

/**
 * Outer work-fold chrome — live Investigating… / Still working…; sealed Worked for.
 * Never returns the spoken-prose "Working..." fallback.
 */
export function workFoldLabel(opts: {
  live?: boolean;
  /** Swarm/hold pause — StatusPill-aligned cue instead of Investigating… */
  pausePoint?: boolean;
  durationMs?: number | null;
  /** Live investigatingHeadline (focus / kind counts); ignored when empty or Working... */
  headline?: string | null;
}): string {
  if (opts.live) {
    if (opts.pausePoint) return "Still working…";
    const headline = String(opts.headline || "").trim();
    if (headline && !isWorkingEllipsisFallback(headline)) return headline;
    return "Investigating…";
  }
  return workedForLabel(opts.durationMs);
}

/** Reasoning fold chrome — live pulses Thinking…; sealed tucks to Thought {Ns}. */
export function thoughtFoldLabel(opts: {
  live?: boolean;
  durationMs?: number | null;
}): string {
  if (opts.live) return "Thinking…";
  if (
    opts.durationMs != null
    && Number.isFinite(opts.durationMs)
    && opts.durationMs >= 1000
  ) {
    const label = formatFoldDuration(opts.durationMs);
    return label ? `Thought ${label}` : "Thought";
  }
  return "Thought";
}

/** Mid-summary command fold — expand shows specific Ran {goal} lines. */
export function ranCommandsLabel(count: number): string {
  const n = Math.max(0, Math.floor(count));
  return n === 1 ? "Ran 1 command" : `Ran ${n} commands`;
}

/** Mid-summary swarm-lifecycle fold — expand shows per-job SwarmPendingPill rows. */
export function swarmDoneFoldLabel(
  count: number,
  outcome: "done" | "failed" | "partial" = "done",
): string {
  const n = Math.max(0, Math.floor(count));
  const noun =
    outcome === "failed"
      ? "Swarm failed"
      : outcome === "partial"
        ? "Swarm results"
        : "Swarm done";
  return n <= 1 ? noun : `${noun} · ${n}`;
}

/** One expanded command row under Ran N — `Ran {goal}`. */
export function ranGoalLine(goal: string): string {
  const g = String(goal || "").trim();
  return g ? `Ran ${g}` : "Ran command";
}

/**
 * Wall time for a sealed Worked for row: sum of known card / thinking /
 * nested-action durations. Returns null when nothing recorded.
 */
export function activityWorkDurationMs(
  items: Array<{
    kind: string;
    card?: {
      result?: { duration_ms?: number | null } | null;
      actions?: Array<{ duration_ms?: number | null }>;
    };
    duration_ms?: number | null;
  }>,
): number | null {
  let total = 0;
  let any = false;
  for (const it of items) {
    if (it.kind === "thinking" && typeof it.duration_ms === "number" && Number.isFinite(it.duration_ms)) {
      total += Math.max(0, it.duration_ms);
      any = true;
    }
    if (it.kind === "card" && it.card) {
      const d = it.card.result?.duration_ms;
      if (typeof d === "number" && Number.isFinite(d)) {
        total += Math.max(0, d);
        any = true;
      }
      for (const action of it.card.actions || []) {
        if (typeof action.duration_ms === "number" && Number.isFinite(action.duration_ms)) {
          total += Math.max(0, action.duration_ms);
          any = true;
        }
      }
    }
  }
  return any ? total : null;
}

export type StackedActivityRow<T> =
  | { kind: "thought"; items: T[]; indexes: number[] }
  | { kind: "commands"; items: T[]; indexes: number[] }
  | { kind: "swarms"; items: T[]; indexes: number[] }
  | { kind: "shelf"; items: T[]; indexes: number[] }
  | { kind: "item"; item: T; index: number };

/** Join consecutive sealed reasoning snapshots into one Thought body. */
export function joinThoughtFoldText(texts: string[]): string {
  const parts = texts.map((t) => String(t || "").trim()).filter(Boolean);
  return parts.join("\n\n");
}

/**
 * Partition an open activity fold into Cursor-style stacked rows:
 * leading Thought siblings, nestable Ran N command groups (with interleaved
 * thoughts inside), exploration shelves, then other items.
 */
export function partitionStackedActivity<T>(
  items: T[],
  meta: (item: T) => {
    cardKind: string | null;
    isThinking: boolean;
    isTerminalSwarmPending?: boolean;
  },
): StackedActivityRow<T>[] {
  const out: StackedActivityRow<T>[] = [];
  let i = 0;
  let seenCommand = false;

  while (i < items.length) {
    const cur = meta(items[i]);

    if (cur.isThinking && !seenCommand) {
      const group: T[] = [];
      const indexes: number[] = [];
      while (i < items.length && meta(items[i]).isThinking) {
        group.push(items[i]);
        indexes.push(i);
        i += 1;
      }
      out.push({ kind: "thought", items: group, indexes });
      continue;
    }

    if (cur.cardKind && isCommandCardKind(cur.cardKind)) {
      seenCommand = true;
      const group: T[] = [];
      const indexes: number[] = [];
      while (i < items.length) {
        const next = meta(items[i]);
        if (next.cardKind && isCommandCardKind(next.cardKind)) {
          group.push(items[i]);
          indexes.push(i);
          i += 1;
          continue;
        }
        // Thoughts (and only thoughts) nest inside the Ran fold between commands.
        if (next.isThinking) {
          group.push(items[i]);
          indexes.push(i);
          i += 1;
          continue;
        }
        break;
      }
      out.push({ kind: "commands", items: group, indexes });
      continue;
    }

    if (cur.isThinking) {
      const group: T[] = [];
      const indexes: number[] = [];
      while (i < items.length && meta(items[i]).isThinking) {
        group.push(items[i]);
        indexes.push(i);
        i += 1;
      }
      out.push({ kind: "thought", items: group, indexes });
      continue;
    }

    if (cur.cardKind && isExplorationShelfKind(cur.cardKind)) {
      const start = i;
      const group: T[] = [];
      const indexes: number[] = [];
      while (i < items.length) {
        const nextKind = meta(items[i]).cardKind;
        if (!nextKind || !isExplorationShelfKind(nextKind)) break;
        group.push(items[i]);
        indexes.push(i);
        i += 1;
      }
      if (group.length >= 2) {
        out.push({ kind: "shelf", items: group, indexes });
      } else {
        out.push({ kind: "item", item: group[0], index: start });
      }
      continue;
    }

    if (cur.isTerminalSwarmPending) {
      const group: T[] = [];
      const indexes: number[] = [];
      while (i < items.length && meta(items[i]).isTerminalSwarmPending) {
        group.push(items[i]);
        indexes.push(i);
        i += 1;
      }
      if (group.length >= 2) {
        out.push({ kind: "swarms", items: group, indexes });
      } else {
        out.push({ kind: "item", item: group[0], index: indexes[0] });
      }
      continue;
    }

    out.push({ kind: "item", item: items[i], index: i });
    i += 1;
  }
  return out;
}

/** Items after the last user message (current turn), or all if none. */
export function itemsInCurrentTurn(items: TurnItem[]): TurnItem[] {
  let lastUser = -1;
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "msg" && (it as { msg: { role: string } }).msg.role === "user") {
      lastUser = i;
      break;
    }
  }
  return lastUser >= 0 ? items.slice(lastUser + 1) : items;
}

function cardsInTurn(items: TurnItem[]): TurnCard[] {
  const out: TurnCard[] = [];
  for (const it of itemsInCurrentTurn(items)) {
    if (it.kind === "card" && (it as { card: TurnCard }).card) {
      out.push((it as { card: TurnCard }).card);
    }
  }
  return out;
}

/** Prefer basename-ish tail of a path/goal so the pill stays readable. */
export function shortenGoal(goal: string, max = 42): string {
  const g = (goal || "").trim().replace(/\s+/g, " ");
  if (!g) return "";
  const parts = g.split(/[/\\]/);
  const tail = parts[parts.length - 1] || g;
  if (tail.length <= max) return tail;
  return tail.slice(0, max - 1) + "…";
}

export function formatBusyElapsed(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "";
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  const rem = sec % 60;
  if (min < 60) return rem ? `${min}m ${rem}s` : `${min}m`;
  const hr = Math.floor(min / 60);
  const mRem = min % 60;
  return mRem ? `${hr}h ${mRem}m` : `${hr}h`;
}

/** Latch the Waiting-on-provider clock; clear when chrome leaves that phase. */
export function latchWaitingPhaseStartedAt(
  prevStartedAt: number | null | undefined,
  phase: string,
  nowMs: number,
  status?: string | null,
): number | null {
  if (status === "awaiting_swarm") return null;
  if (phase === "waiting") return prevStartedAt ?? nowMs;
  return null;
}

function turnHasAssistantText(items: TurnItem[]): boolean {
  for (const it of itemsInCurrentTurn(items)) {
    if (it.kind === "msg") {
      const msg = (it as { msg: { role: string; text?: string } }).msg;
      if (msg.role === "assistant" && (msg.text || "").trim()) return true;
    }
  }
  return false;
}

function turnHasThinking(items: TurnItem[]): boolean {
  for (const it of itemsInCurrentTurn(items)) {
    if (it.kind === "thinking" && String((it as { text?: string }).text || "").trim()) {
      return true;
    }
  }
  return false;
}

/** True once the turn shows reasoning, tools, or assistant text (not bare TTFT). */
export function turnHasLiveProgressSignal(items: TurnItem[]): boolean {
  let toolPrep = "";
  for (const it of [...itemsInCurrentTurn(items)].reverse()) {
    if (it.kind === "tool_prep") {
      toolPrep = String((it as { name?: string }).name || "").trim();
      break;
    }
  }
  return (
    cardsInTurn(items).length > 0
    || Boolean(toolPrep)
    || turnHasThinking(items)
    || turnHasAssistantText(items)
  );
}

/** True when the current turn already ran tools / tool_prep (agent loop). */
export function turnHasInvestigationActivity(items: TurnItem[]): boolean {
  for (const it of itemsInCurrentTurn(items)) {
    if (it.kind === "card" || it.kind === "tool_prep") return true;
  }
  return false;
}

/**
 * True when the current turn already shows a finished assistant answer and
 * nothing is still live. Used ONLY to clear busy chrome on pure chat turns
 * while SSE status lags after the final answer (T5).
 *
 * Tool-using turns MUST NOT be inferred complete from transcript shape:
 * mid-turn narration after finished cards looks identical to a final answer,
 * and treating it as complete blinks the header to idle and drops Steer
 * between tool batches. Those turns stay busy until assistant_done / idle.
 */
export function turnLooksAnswerComplete(items: TurnItem[]): boolean {
  const turn = itemsInCurrentTurn(items);
  // Agent / tool loops: never early-complete from shape alone.
  if (turnHasInvestigationActivity(items)) return false;

  let lastAssistant: { text?: string; streaming?: boolean } | null = null;
  for (let i = 0; i < turn.length; i++) {
    const it = turn[i];
    if (it.kind === "msg") {
      const msg = (it as { msg: { role: string; text?: string; streaming?: boolean } }).msg;
      if (msg.role === "assistant") {
        lastAssistant = msg;
      }
    }
  }
  if (!lastAssistant || !(lastAssistant.text || "").trim()) return false;
  if (lastAssistant.streaming === true) return false;

  for (const it of turn) {
    if (it.kind === "tool_prep") return false;
    if (
      it.kind === "thinking"
      && (it as { streaming?: boolean }).streaming === true
    ) {
      return false;
    }
  }
  return true;
}

/**
 * Whether the transcript busy footer should render for this status + items.
 *
 * Tool loops keep the step/timer line for the whole open agent turn — including
 * running cards, thinking, and the gap between tool calls. Hiding it whenever
 * ``turnHasVisibleBusySurface`` was true made "Still working…" vanish while
 * tools were still running and again in the silent beat before the next call.
 *
 * Pure-chat typewriter still owns the live signal (no investigation fold yet),
 * so a streaming bubble/thinking row there does not also stack a footer.
 *
 * ``agentLoopOpen`` covers status flaps to idle while ``turnOpen`` / hold is
 * still latched, so the footer does not drop between SSE tool events.
 */
export function shouldShowBusyFooter(
  items: TurnItem[],
  status: BusyStatus,
  agentLoopOpen: boolean = false,
): boolean {
  if (status === "awaiting_swarm") return true;
  const busy =
    status === "thinking"
    || status === "executing"
    || status === "streaming"
    || agentLoopOpen;
  if (!busy) return false;
  if (turnLooksAnswerComplete(items)) return false;
  if (!turnHasInvestigationActivity(items) && turnHasVisibleBusySurface(items)) {
    return false;
  }
  return true;
}

/**
 * Derive the live busy line from transcript cards + stream status.
 * When idle/done/error, returns empty labels (caller hides the row).
 * Pre-token TTFT: "Waiting on provider…" until reasoning or tools start.
 * Post-answer SSE lag: empty labels once the assistant bubble looks complete.
 */
/** Short model label for wait chrome (drop provider prefix when present). */
export function shortPilotModelLabel(driver: string | null | undefined): string {
  const raw = (driver || "").trim();
  if (!raw) return "";
  const model = raw.includes(":") ? raw.split(":").slice(1).join(":") : raw;
  // Prefer the leaf id for long OpenRouter-style paths.
  const leaf = model.includes("/") ? model.split("/").pop() || model : model;
  return leaf.length > 28 ? `${leaf.slice(0, 26)}…` : leaf;
}

export function deriveBusyProgress(
  items: TurnItem[],
  status: BusyStatus,
  elapsedMs?: number | null,
  opts?: {
    modelLabel?: string | null;
    waitHint?: string | null;
    providerElapsedMs?: number | null;
  },
): BusyProgress {
  const awaitingSwarm = status === "awaiting_swarm";
  const busy =
    status === "thinking"
    || status === "executing"
    || status === "streaming"
    || awaitingSwarm;
  const cards = cardsInTurn(items);
  const step = cards.length;
  const running = [...cards].reverse().find((c) => cardEffectivelyRunning(c));
  const runningKind = (running?.kind || "").replace(/_/g, " ").trim();
  const runningGoal = shortenGoal(resolveCardCliInput(running || {}) || "");

  let toolPrep = "";
  for (const it of [...itemsInCurrentTurn(items)].reverse()) {
    if (it.kind === "tool_prep") {
      toolPrep = String((it as { name?: string }).name || "").replace(/_/g, " ").trim();
      break;
    }
  }

  const hasSignal =
    cards.length > 0
    || Boolean(toolPrep)
    || turnHasThinking(items)
    || turnHasAssistantText(items);

  let phase = "idle";
  if (status === "streaming") phase = "streaming";
  else if (running || status === "executing") phase = "running";
  else if (awaitingSwarm) phase = "waiting";
  else if (busy && !hasSignal) phase = "waiting";
  else if (status === "thinking" || busy) phase = "thinking";

  const waitClockMs =
    opts && opts.providerElapsedMs != null ? opts.providerElapsedMs : elapsedMs;
  const waitElapsed =
    busy && waitClockMs != null && waitClockMs >= 1000
      ? formatBusyElapsed(waitClockMs)
      : "";
  const elapsed =
    busy && elapsedMs != null && elapsedMs >= 1000
      ? formatBusyElapsed(elapsedMs)
      : "";

  // T5: answer already on screen — clear busy labels even if status lags.
  // Exception: background swarm await — summary is on screen on purpose while
  // workers fly; keep "Still working…" chrome (Cursor-style pause point).
  if (busy && !awaitingSwarm && turnLooksAnswerComplete(items)) {
    return {
      phase: "idle",
      label: "",
      pill: "idle",
      step,
      runningGoal,
      runningKind,
    };
  }

  const hint = waitHintForBusyProgress(opts?.waitHint, {
    hasSignal,
    turnFailed: status === "error",
  })?.trim() || "";

  // Settled error still shows a leftover driver-failure hint. Recovered
  // turns already cleared it in waitHintForBusyProgress.
  if (status === "error" && hint) {
    return {
      phase: "error",
      label: hint,
      pill: hint,
      step,
      runningGoal,
      runningKind,
    };
  }

  if (!busy) {
    return {
      phase,
      label: "",
      pill: String(status || "idle"),
      step,
      runningGoal,
      runningKind,
    };
  }

  // Background job pause: paint the await hint as the primary line (not
  // "Waiting on <pilot>" — the pilot turn already ended).
  if (awaitingSwarm) {
    const line = hint || "Still working…";
    const waiting = elapsed ? `${line} · ${elapsed}` : line;
    return {
      phase: "waiting",
      label: waiting,
      pill: waiting,
      step,
      runningGoal,
      runningKind,
    };
  }

  // T3: honesty before first token / tool — do not pretend we are "thinking".
  // Provider-idle wait hints are for genuine silent periods only. A live
  // command/tool card means we are executing, not waiting on the provider.
  if (hint && !running) {
    const model = shortPilotModelLabel(opts?.modelLabel || "");
    const who = model ? `Waiting on ${model}` : "Waiting on provider";
    let waiting = waitElapsed ? `${who}… · ${waitElapsed}` : `${who}…`;
    waiting = `${waiting} · ${hint}`;
    return {
      phase: "waiting",
      label: waiting,
      pill: waiting,
      step,
      runningGoal,
      runningKind,
    };
  }

  if (!hasSignal) {
    const model = shortPilotModelLabel(opts?.modelLabel || "");
    const who = model ? `Waiting on ${model}` : "Waiting on provider";
    let waiting = waitElapsed ? `${who}… · ${waitElapsed}` : `${who}…`;
    return {
      phase: "waiting",
      label: waiting,
      pill: waiting,
      step,
      runningGoal,
      runningKind,
    };
  }

  // Footer keeps a quiet step line; header pill uses Investigating / Still
  // working… — never raw phase enums (running/thinking/streaming).
  const parts: string[] = [];
  if (runningKind) parts.push(runningKind);
  else if (runningGoal) parts.push(runningGoal);
  else if (toolPrep) parts.push(toolPrep);
  else if (running || status === "executing") parts.push("Investigating…");
  else parts.push("Still working…");
  if (step > 0) parts.push(`step ${step}`);
  if (elapsed) parts.push(elapsed);

  const label = parts.join(" · ");

  const pillChrome =
    running || status === "executing" || phase === "running"
      ? "Investigating…"
      : "Still working…";
  const pillParts: string[] = [pillChrome];
  if (runningKind) pillParts.push(runningKind);
  else if (toolPrep) pillParts.push(toolPrep);
  if (step > 0) pillParts.push(`${step}`);
  if (elapsed) pillParts.push(elapsed);

  return {
    phase,
    label,
    pill: pillParts.join(" · "),
    step,
    runningGoal,
    runningKind,
  };
}

/**
 * Cursor-style Investigating / Explored headline for the activity fold.
 * Live: "Investigating · run command …" (or kind counts).
 * Done: "Explored 3 files, 1 search".
 */
export function investigatingHeadline(
  actionCount: number,
  anyRunning: boolean,
  runningKind: string,
  runningGoal: string,
  kindSummary: string,
): string {
  // Reasoning-only Investigating (Cursor thought stream before tool_call).
  if (actionCount <= 0) return anyRunning ? "Investigating…" : "";
  if (anyRunning) {
    // Avoid "tool tool" / "read read" when kind and goal are the same string
    // (Cursor CLI tool_prep used to set both from the hint name).
    const kind = (runningKind || "").trim();
    const goal = (runningGoal || "").trim();
    const k = kind.toLowerCase().replace(/_/g, " ");
    const g = goal.toLowerCase().replace(/_/g, " ");
    let focus = "";
    if (kind && goal) {
      if (!g || g === k || g === "tool") focus = kind;
      else if (!k || k === "tool") focus = goal;
      else focus = `${kind} ${goal}`;
    } else {
      focus = kind || goal;
    }
    if (focus) return `Investigating · ${focus}`;
    if (kindSummary) return `Investigating · ${kindSummary}`;
    return "Investigating…";
  }
  if (kindSummary) return `Explored ${kindSummary}`;
  return `Explored ${actionCount} step${actionCount === 1 ? "" : "s"}`;
}

/**
 * True when the current turn's activity fold is actively investigating.
 * Includes gaps between tool steps when the agent loop is still open
 * (``agentLoopOpen``) so Investigating / Stop / Steer do not blink idle.
 *
 * Durable background command/batch/swarm jobs remain visible as cards after
 * the pilot closes, but must not keep Investigating / Stop / Steer pinned.
 */
export function turnHasLiveInvestigation(
  items: TurnItem[],
  agentLoopOpen: boolean = false,
): boolean {
  for (const it of itemsInCurrentTurn(items)) {
    if (it.kind === "card") {
      const card = (it as { card: TurnCard }).card;
      if (card && cardEffectivelyRunning(card)) {
        if (!agentLoopOpen && cardHasDurableJob(card)) continue;
        return true;
      }
    }
    if (it.kind === "tool_prep") {
      if (agentLoopOpen) return true;
      continue;
    }
    if (
      it.kind === "thinking"
      && (it as { streaming?: boolean; text?: string }).streaming
      && String((it as { text?: string }).text || "").trim()
    ) {
      return true;
    }
  }
  // Between tool batches: cards exist, none running, loop still open.
  if (agentLoopOpen && turnHasInvestigationActivity(items)) return true;
  return false;
}

/**
 * True when some transcript chrome already visibly signals foreground work in
 * the current turn: a running non-durable tool card / tool_prep, a streaming
 * thinking row, or a streaming assistant bubble.
 *
 * Durable background jobs are pollable card UI — they must not hold Stop/Steer
 * after the pilot runner has gone idle.
 */
export function turnHasVisibleBusySurface(
  items: TurnItem[],
  opts: { includeToolPrep?: boolean } = {},
): boolean {
  const includeToolPrep = opts.includeToolPrep !== false;
  for (const it of itemsInCurrentTurn(items)) {
    if (it.kind === "card") {
      const card = (it as { card: TurnCard }).card || ({} as TurnCard);
      if (cardEffectivelyRunning(card) && !cardHasDurableJob(card)) {
        return true;
      }
    }
    if (it.kind === "tool_prep") {
      if (includeToolPrep) return true;
      continue;
    }
    if (it.kind === "thinking" && (it as { streaming?: boolean }).streaming === true) {
      return true;
    }
    if (it.kind === "msg") {
      const msg = (it as { msg: { role: string; streaming?: boolean } }).msg;
      if (msg.role === "assistant" && msg.streaming === true) return true;
    }
  }
  return false;
}

/**
 * Quiet "Still working" cue: shows the moment the turn is busy with nothing
 * else on screen indicating work, and stays until a real busy surface takes
 * over. No arming timer — the old 2s stall debounce left an idle-looking gap
 * between tool calls (card finishes → footer hidden → cue not armed yet),
 * which read as an idle→working flicker at every tool boundary.
 *
 * When the Investigating fold is already live (including tool-gap stickiness
 * via ``agentLoopOpen``), that collapsed header is the sticky busy signal —
 * do not paint a second under-fold "Still working…" that blinks on/off as
 * tools enter and leave ``turnHasVisibleBusySurface``.
 */
export function quietWorkingCueVisible(
  items: TurnItem[],
  status: BusyStatus,
  compacting: boolean,
  busyFooterShown: boolean,
  agentLoopOpen: boolean = false,
): boolean {
  if (compacting || busyFooterShown) return false;
  const busy =
    status === "thinking" || status === "executing" || status === "streaming";
  if (!busy) return false;
  // Investigating chrome already owns the live signal (header stays sticky
  // across tool gaps when the loop is open). Suppress the under-fold cue.
  if (turnHasLiveInvestigation(items, agentLoopOpen)) return false;
  return !turnHasVisibleBusySurface(items);
}
