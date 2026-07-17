/**
 * Live busy-turn progress for the transcript footer and header pill.
 *
 * A long diagnose used to sit on "running..." while tools burned tokens
 * invisibly. These helpers derive a scannable label from the same cards the
 * activity fold already knows about -- pure, so vitest can pin the contract.
 */

export type BusyStatus = "idle" | "thinking" | "executing" | "done" | "error" | "streaming" | string;

export type TurnCard = {
  id: string;
  goal: string;
  kind: string;
  running: boolean;
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
  if (!g || g === "tool" || g === "function" || g === "unknown") return true;
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
 * False when the answer already looks complete despite a lagging busy status.
 */
export function shouldShowBusyFooter(items: TurnItem[], status: BusyStatus): boolean {
  const busy =
    status === "thinking" || status === "executing" || status === "streaming";
  if (!busy) return false;
  if (turnLooksAnswerComplete(items)) return false;
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
  opts?: { modelLabel?: string | null; waitHint?: string | null },
): BusyProgress {
  const busy =
    status === "thinking" || status === "executing" || status === "streaming";
  const cards = cardsInTurn(items);
  const step = cards.length;
  const running = [...cards].reverse().find((c) => c.running);
  const runningKind = (running?.kind || "").replace(/_/g, " ").trim();
  const runningGoal = shortenGoal(running?.goal || "");

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
  else if (busy && !hasSignal) phase = "waiting";
  else if (status === "thinking" || busy) phase = "thinking";

  const elapsed =
    busy && elapsedMs != null && elapsedMs >= 1000
      ? formatBusyElapsed(elapsedMs)
      : "";

  // T5: answer already on screen — clear busy labels even if status lags.
  if (busy && turnLooksAnswerComplete(items)) {
    return {
      phase: "idle",
      label: "",
      pill: "idle",
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

  // T3: honesty before first token / tool — do not pretend we are "thinking".
  if (!hasSignal) {
    const model = shortPilotModelLabel(opts?.modelLabel || "");
    const hint = (opts?.waitHint || "").trim();
    const who = model ? `Waiting on ${model}` : "Waiting on provider";
    let waiting = elapsed ? `${who}… · ${elapsed}` : `${who}…`;
    if (hint) {
      waiting = `${waiting} · ${hint}`;
    }
    return {
      phase: "waiting",
      label: waiting,
      pill: waiting,
      step,
      runningGoal,
      runningKind,
    };
  }

  const parts: string[] = [];
  if (status === "streaming") parts.push("streaming");
  else if (running || status === "executing") parts.push("running");
  else parts.push("thinking");

  if (runningKind) parts.push(runningKind);
  else if (runningGoal) parts.push(runningGoal);
  else if (toolPrep) parts.push(toolPrep);
  if (step > 0) parts.push(`step ${step}`);
  if (elapsed) parts.push(elapsed);

  const label = parts.join(" · ");

  const pillParts: string[] = [phase];
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
  if (actionCount <= 0) return "";
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
 */
export function turnHasLiveInvestigation(
  items: TurnItem[],
  agentLoopOpen: boolean = false,
): boolean {
  for (const it of itemsInCurrentTurn(items)) {
    if (it.kind === "card" && (it as { card: TurnCard }).card?.running) return true;
    if (it.kind === "tool_prep") return true;
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
 * True when some transcript chrome already visibly signals work in the current
 * turn: a running tool card / tool_prep (Investigating spinner), a streaming
 * thinking row, or a streaming assistant bubble.
 */
export function turnHasVisibleBusySurface(items: TurnItem[]): boolean {
  for (const it of itemsInCurrentTurn(items)) {
    if (it.kind === "card" && (it as { card: TurnCard }).card?.running) return true;
    if (it.kind === "tool_prep") return true;
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
 */
export function quietWorkingCueVisible(
  items: TurnItem[],
  status: BusyStatus,
  compacting: boolean,
  busyFooterShown: boolean,
): boolean {
  if (compacting || busyFooterShown) return false;
  const busy =
    status === "thinking" || status === "executing" || status === "streaming";
  if (!busy) return false;
  return !turnHasVisibleBusySurface(items);
}
