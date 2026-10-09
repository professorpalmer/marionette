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

/**
 * Aggregate card kinds into Cursor's explored summary:
 * "4 files, 7 searches, ran 1 command".
 */
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
    const phrase = `${n} ${n === 1 ? one : many}`;
    parts.push(b === "commands" ? `ran ${phrase}` : phrase);
  }
  return parts.join(", ");
}

type ToolVerbs = { past: string; live: string };

const TOOL_VERBS: Record<string, ToolVerbs> = {
  read_file: { past: "Read", live: "Reading" },
  read: { past: "Read", live: "Reading" },
  write_file: { past: "Wrote", live: "Writing" },
  edit_file: { past: "Edited", live: "Editing" },
  apply_hashline: { past: "Edited", live: "Editing" },
  hash_edit: { past: "Edited", live: "Editing" },
  grep: { past: "Grepped", live: "Grepping" },
  search: { past: "Searched", live: "Searching" },
  glob: { past: "Listed files", live: "Listing files" },
  run_command: { past: "Ran", live: "Running" },
  run_terminal: { past: "Ran", live: "Running" },
  execute: { past: "Ran", live: "Running" },
  shell: { past: "Ran", live: "Running" },
  query_wiki: { past: "Queried wiki", live: "Querying wiki" },
  wiki: { past: "Queried wiki", live: "Querying wiki" },
  web_fetch: { past: "Fetched", live: "Fetching" },
  fetch: { past: "Fetched", live: "Fetching" },
  codegraph_search: { past: "Queried graph", live: "Querying graph" },
  codegraph_context: { past: "Queried graph", live: "Querying graph" },
  codegraph: { past: "Queried graph", live: "Querying graph" },
  call_mcp: { past: "Called", live: "Calling" },
  mcp: { past: "Called", live: "Calling" },
  view_image: { past: "Viewed", live: "Viewing" },
  open_project: { past: "Opened", live: "Opening" },
  delete: { past: "Deleted", live: "Deleting" },
  move: { past: "Moved", live: "Moving" },
};

/**
 * Verb for one flat activity row: past tense when done ("Read", "Grepped",
 * "Ran"), progressive while live ("Reading"). Unknown tools keep their label.
 */
export function toolVerb(kind: string, live: boolean): string {
  const k = normalizeToolKind(kind) || (kind || "").toLowerCase().replace(/-/g, "_").trim();
  const verbs = TOOL_VERBS[k];
  if (verbs) return live ? verbs.live : verbs.past;
  const label = toolRowLabel(kind);
  return live ? `Running ${label.toLowerCase()}` : label;
}

/**
 * The one live status line under an Exploring fold (the "wheel" line).
 * A running tool names its verb and target; live reasoning is Thinking;
 * a quiet gap while the loop is open is Planning next moves.
 */
export function liveActivityLine(opts: {
  runningKind?: string | null;
  runningGoal?: string | null;
  liveThinking?: boolean;
}): string {
  const kind = String(opts.runningKind || "").trim();
  if (kind) {
    const goal = String(opts.runningGoal || "").trim();
    const verb = toolVerb(kind, true);
    return goal && !isRedundantToolGoal(kind, goal) ? `${verb} ${goal}` : verb;
  }
  if (opts.liveThinking) return "Thinking";
  return "Planning next moves";
}

/** Fold headline: "Exploring 4 files, 1 search" live; "Explored ..." done. */
export function exploringHeadline(kindSummary: string, live: boolean): string {
  const verb = live ? "Exploring" : "Explored";
  const summary = String(kindSummary || "").trim();
  return summary ? `${verb} ${summary}` : verb;
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
  /** Live exploringHeadline (kind counts); ignored when empty or Working... */
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

/**
 * Wall-clock span of a saved turn: from the user's message to the end of the
 * last action, read from the times the backend stamps on transcript rows, so
 * it survives a reload. Null when the rows predate those times.
 */
export function turnSpanMs(
  items: Array<{ kind: string; card?: { ts?: number; turn_ts?: number; result?: { duration_ms?: number | null } | null } }>,
): number | null {
  let start = Infinity;
  let end = -Infinity;
  for (const it of items) {
    const card = it.kind === "card" ? it.card : undefined;
    if (card?.ts == null || card.turn_ts == null) continue;
    start = Math.min(start, card.turn_ts);
    end = Math.max(end, card.ts + Math.max(0, card.result?.duration_ms ?? 0));
  }
  return end > start ? end - start : null;
}

export function maxKnown(a: number | null, b: number | null): number | null {
  if (a == null) return b;
  if (b == null) return a;
  return Math.max(a, b);
}

/**
 * Duration for the Worked for row. A live fold still on the same job uses
 * the wall-clock busy timer when it is longer than recorded tool slices —
 * that is the Still working… clock. Prior folds never inherit it.
 */
export function foldWorkDurationMs(opts: {
  fromItems: number | null;
  busyElapsedMs?: number | null;
  isLiveFold: boolean;
}): number | null {
  const items = opts.fromItems != null && opts.fromItems > 0 ? opts.fromItems : null;
  const busy =
    opts.isLiveFold && opts.busyElapsedMs != null && opts.busyElapsedMs > 0
      ? opts.busyElapsedMs
      : null;
  if (busy != null && (items == null || busy > items)) return busy;
  if (items != null) return items;
  return null;
}

/**
 * Worked for duration across the live → prior-fold handoff.
 * Live folds remember the wall-clock busy timer. Prior folds keep that
 * remembered value even when recorded thinking/tool slices are a 1s crumb
 * (local Completions often finalize reasoning long before the turn ends).
 * The cosmetic 1s fallback is never written into memory.
 */
export function resolveSealedWorkMs(opts: {
  fromItems: number | null;
  busyElapsedMs?: number | null;
  isLiveFold: boolean;
  rememberedMs?: number | null;
  hasVisibleWork: boolean;
}): { durationMs: number | null; rememberMs: number | null } {
  const resolved = foldWorkDurationMs({
    fromItems: opts.fromItems,
    busyElapsedMs: opts.busyElapsedMs,
    isLiveFold: opts.isLiveFold,
  });
  const remembered =
    opts.rememberedMs != null
    && Number.isFinite(opts.rememberedMs)
    && opts.rememberedMs > 0
      ? opts.rememberedMs
      : null;
  if (opts.isLiveFold && resolved != null && resolved > 0) {
    return { durationMs: resolved, rememberMs: resolved };
  }
  if (remembered != null && (resolved == null || remembered > resolved)) {
    return { durationMs: remembered, rememberMs: remembered };
  }
  if (resolved != null && resolved > 0) {
    return { durationMs: resolved, rememberMs: remembered };
  }
  if (opts.hasVisibleWork) {
    return { durationMs: 1000, rememberMs: remembered };
  }
  return { durationMs: null, rememberMs: remembered };
}

export type ActivityRow<T> =
  | { kind: "thought"; items: T[]; indexes: number[] }
  | { kind: "swarms"; items: T[]; indexes: number[] }
  | { kind: "item"; item: T; index: number };

const THOUGHT_SNAPSHOT_MIN = 12;

/** Keep one copy when the whole body is the same paragraph repeated. */
export function collapseRepeatedThoughtBlocks(text: string): string {
  const raw = String(text || "");
  const trimmed = raw.trim();
  if (!trimmed) return raw;
  const parts = trimmed.split(/\n\s*\n/).map((part) => part.trim()).filter(Boolean);
  if (
    parts.length >= 2
    && parts[0].length >= THOUGHT_SNAPSHOT_MIN
    && parts.every((part) => part === parts[0])
  ) {
    return parts[0];
  }
  return trimmed;
}

/** Return the new suffix to append, or empty when incoming is a replay. */
export function absorbThoughtSnapshot(
  accumulated: string,
  incoming: string,
  minChunk = THOUGHT_SNAPSHOT_MIN,
): string {
  const acc = accumulated || "";
  const inc = incoming || "";
  if (!inc) return "";
  const collapsedInc = collapseRepeatedThoughtBlocks(inc);
  if (!acc) return collapsedInc;
  if (inc.startsWith(acc)) {
    const rest = inc.slice(acc.length);
    if (!rest.trim()) return acc.length >= minChunk ? "" : inc;
    if (acc.length >= minChunk && rest.trim() === acc.trim()) return "";
    if (collapsedInc !== inc && collapsedInc.startsWith(acc)) {
      return collapsedInc.slice(acc.length);
    }
    return rest;
  }
  if (acc.length >= minChunk && inc.trim() === acc.trim()) return "";
  if (acc.length >= minChunk && collapsedInc.trim() === acc.trim()) return "";
  return inc;
}

/** Join consecutive sealed reasoning snapshots into one Thought body. */
export function joinThoughtFoldText(texts: string[]): string {
  const parts: string[] = [];
  for (const raw of texts) {
    const next = collapseRepeatedThoughtBlocks(String(raw || "")).trim();
    if (!next) continue;
    if (parts[parts.length - 1] === next) continue;
    parts.push(next);
  }
  return collapseRepeatedThoughtBlocks(parts.join("\n\n"));
}

/**
 * Partition an open activity fold into Cursor-style flat rows: one line per
 * tool, consecutive reasoning snapshots joined into one Thought row, and runs
 * of finished swarm receipts joined into one row. No nested folds.
 */
export function partitionActivityRows<T>(
  items: T[],
  meta: (item: T) => { isThinking: boolean; isTerminalSwarmPending?: boolean },
): ActivityRow<T>[] {
  const out: ActivityRow<T>[] = [];
  let i = 0;
  while (i < items.length) {
    const cur = meta(items[i]);
    if (cur.isThinking || cur.isTerminalSwarmPending) {
      const same = (item: T) => {
        const m = meta(item);
        return cur.isThinking ? m.isThinking : Boolean(m.isTerminalSwarmPending);
      };
      const group: T[] = [];
      const indexes: number[] = [];
      while (i < items.length && same(items[i])) {
        group.push(items[i]);
        indexes.push(i);
        i += 1;
      }
      if (cur.isThinking) out.push({ kind: "thought", items: group, indexes });
      else if (group.length >= 2) out.push({ kind: "swarms", items: group, indexes });
      else out.push({ kind: "item", item: group[0], index: indexes[0] });
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

/**
 * The step the open turn is on, for the todo list: the latest tool card of the
 * current turn, running or just finished. Latest rather than running, so the
 * line steps from one command to the next instead of blinking out between them.
 */
export function currentTurnStep(items: TurnItem[]): string | null {
  const card = [...cardsInTurn(items)].reverse().find((c) => (c.kind || "") !== "todo");
  if (!card) return null;
  const goal = shortenGoal(resolveCardCliInput(card) || "");
  const kind = toolFocusPhrase(card.kind || "");
  return [kind, goal].filter(Boolean).join(" ") || null;
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
  busyStartedAt?: number | null,
): number | null {
  if (status === "awaiting_swarm") return null;
  if (phase === "waiting") {
    const raw = prevStartedAt ?? nowMs;
    // busyNow can freeze at the last idle tick. A leftover stamp from that
    // era must not become "Waiting on <pilot> · 44s" on the next send.
    if (busyStartedAt != null && raw < busyStartedAt) return busyStartedAt;
    return raw;
  }
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
  const latest = itemsInCurrentTurn(items).at(-1) as { kind: string; msg?: { role?: string; streaming?: boolean; workerStream?: boolean } } | undefined;
  const writing = latest?.kind === "msg" && latest.msg?.role === "assistant" && Boolean(latest.msg.streaming) && !latest.msg.workerStream;
  const parts: string[] = [];
  if (runningKind) parts.push(runningKind);
  else if (runningGoal) parts.push(runningGoal);
  else if (toolPrep) parts.push(toolPrep);
  else if (running || status === "executing") parts.push("Investigating…");
  else if (writing) parts.push("Writing…");
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
 * The card an Investigating headline names. A running command's card exists a
 * beat before its arguments arrive; naming it then flashes the bare tool name
 * between every call, so name the latest card that has a goal until it does.
 */
export function headlineFocusCard<C extends { goal?: string }>(
  cards: readonly C[],
  running: C | undefined,
  goalOf: (card: C) => string,
): C | undefined {
  if (!running || goalOf(running)) return running;
  return [...cards].reverse().find((c) => goalOf(c)) ?? running;
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
