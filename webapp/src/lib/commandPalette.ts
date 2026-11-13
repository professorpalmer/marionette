/**
 * Cmd/Ctrl-K palette model for Marionette: curated commands plus sessions,
 * workspace files and pilot models. Pure, so the ranking unit-tests fast.
 */

import type { Session } from "./api";
import { expandAgentMemory } from "./memoryDeepLink";
import { modelLabelOf, providerLabelOf, providerOf } from "./pilotPickerModels";
import type { SessionSearchHit } from "./sessionSearch";
import { displaySessionListTitle } from "./sessionTitle";
import { recentSessionRows, sessionAge, workspaceName } from "./sessionNav";

export type CommandPaletteActionId =
  | "new-session"
  | "clear-transcript"
  | "focus-composer"
  | "open-swarm"
  | "open-settings"
  | "open-terminal"
  | "open-state"
  | "open-files"
  | "compact-now"
  | "toggle-right-dock"
  | "toggle-left-rail"
  | "open-memory"
  | "open-mcp"
  | "switch-model";

/** "mod" is Cmd on macOS and Ctrl elsewhere. Each key matches an App hotkey. */
export type ShortcutKey = "mod" | "shift" | "B" | "J" | "L" | "N" | "`";

export type CommandPaletteAction = {
  id: CommandPaletteActionId;
  label: string;
  /** Extra terms for fuzzy filter (space-separated). */
  keywords: string;
  shortcut?: readonly ShortcutKey[];
};

/** Stable curated list — keep small and honest. */
export const COMMAND_PALETTE_ACTIONS: CommandPaletteAction[] = [
  { id: "new-session", label: "New session", keywords: "chat create /new", shortcut: ["mod", "N"] },
  { id: "clear-transcript", label: "Clear transcript", keywords: "reset visible /clear" },
  { id: "focus-composer", label: "Focus composer", keywords: "input chat prompt", shortcut: ["mod", "L"] },
  { id: "switch-model", label: "Switch model", keywords: "pilot model picker change llm" },
  { id: "open-swarm", label: "Open Jobs", keywords: "swarm tracker jobs workers puppetmaster" },
  { id: "open-settings", label: "Open Settings", keywords: "preferences config", shortcut: ["mod", "shift", "J"] },
  { id: "open-terminal", label: "Open Terminal", keywords: "shell console", shortcut: ["mod", "`"] },
  { id: "open-state", label: "Open State", keywords: "files pane status" },
  { id: "open-files", label: "Open Files", keywords: "tree workspace" },
  { id: "compact-now", label: "Compact Now", keywords: "context summarize /compact" },
  { id: "toggle-right-dock", label: "Toggle right dock", keywords: "panel side", shortcut: ["mod", "J"] },
  { id: "toggle-left-rail", label: "Toggle left rail", keywords: "sessions sidebar", shortcut: ["mod", "B"] },
  { id: "open-memory", label: "Open Memory", keywords: "agent facts preferences advanced" },
  { id: "open-mcp", label: "Open MCP", keywords: "servers tools state" },
];

export type CommandPaletteRunHooks = {
  toggleLeft: () => void;
  toggleRight: () => void;
  /** Open Settings overlay to a page (e.g. advanced for Memory). */
  focusSettingsPage: (page: string) => void;
};

/** Token / substring match over label + keywords + id; higher is better. */
export function scoreCommandPaletteMatch(
  action: CommandPaletteAction,
  query: string,
): number | null {
  return scoreText(`${action.label} ${action.keywords} ${action.id}`, query);
}

/** Substring, all-tokens, then word-prefix match; higher is better. */
export function scoreText(text: string, query: string): number | null {
  const q = query.trim().toLowerCase();
  if (!q) return 0;
  const haystack = text.toLowerCase();
  if (haystack.includes(q)) {
    const at = haystack.indexOf(q);
    return 200 - Math.min(at, 100);
  }
  // Multi-word queries: every token must appear as a substring.
  const tokens = q.split(/\s+/).filter(Boolean);
  if (tokens.length > 1 && tokens.every((t) => haystack.includes(t))) {
    return 120;
  }
  // Prefix match against any whitespace-separated word (keeps "clea" → Clear).
  const words = haystack.split(/[\s/_-]+/).filter(Boolean);
  const prefixAt = words.findIndex((w) => w.startsWith(q));
  if (prefixAt >= 0) return 150 - Math.min(prefixAt, 50);
  return null;
}

export function filterCommandPaletteActions(
  actions: readonly CommandPaletteAction[],
  query: string,
): CommandPaletteAction[] {
  const q = query.trim();
  if (!q) return actions.slice();
  const ranked = actions
    .map((action) => {
      const score = scoreCommandPaletteMatch(action, q);
      return score === null ? null : { action, score };
    })
    .filter((row): row is { action: CommandPaletteAction; score: number } => row !== null)
    .sort((a, b) => b.score - a.score || a.action.label.localeCompare(b.action.label));
  return ranked.map((row) => row.action);
}

/**
 * Run a palette action via existing harness events / App layout hooks.
 * Clear transcript must NOT create a session (that is New session).
 */
export function runCommandPaletteAction(
  id: CommandPaletteActionId,
  hooks: CommandPaletteRunHooks,
): void {
  switch (id) {
    case "new-session":
      window.dispatchEvent(new Event("harness-new-session"));
      return;
    case "clear-transcript":
      window.dispatchEvent(new Event("harness-clear-transcript"));
      return;
    case "focus-composer":
      window.dispatchEvent(new Event("harness-focus-input"));
      return;
    case "open-swarm":
      window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "swarm" }));
      return;
    case "open-settings":
      window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "settings" }));
      return;
    case "open-terminal":
      window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "terminal" }));
      return;
    case "open-state":
      window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "state" }));
      return;
    case "open-files":
      window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "files" }));
      return;
    case "compact-now":
      window.dispatchEvent(new Event("harness-compact-session"));
      return;
    case "toggle-right-dock":
      hooks.toggleRight();
      return;
    case "toggle-left-rail":
      hooks.toggleLeft();
      return;
    case "open-memory":
      hooks.focusSettingsPage("advanced");
      window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "settings" }));
      // Mirror /mcp → harness-expand-mcp: Advanced alone leaves Agent Memory collapsed.
      expandAgentMemory();
      return;
    case "open-mcp":
      window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "mcp" }));
      return;
    case "switch-model":
      window.dispatchEvent(new Event("harness-open-model-picker"));
      return;
    default: {
      const _exhaustive: never = id;
      void _exhaustive;
    }
  }
}

/** Key caps for a shortcut: ["⌘", "⇧", "J"] on macOS, ["Ctrl", "Shift", "J"] elsewhere. */
export function shortcutCaps(keys: readonly ShortcutKey[], isMac: boolean): string[] {
  return keys.map((key) => {
    if (key === "mod") return isMac ? "\u2318" : "Ctrl";
    if (key === "shift") return isMac ? "\u21e7" : "Shift";
    return key;
  });
}

/** Spoken form for aria and titles: "Command+Shift+J" or "Ctrl+Shift+J". */
export function shortcutText(keys: readonly ShortcutKey[], isMac: boolean): string {
  return keys
    .map((key) => (key === "mod" ? (isMac ? "Command" : "Ctrl") : key === "shift" ? "Shift" : key))
    .join("+");
}

export type PaletteItem =
  | { kind: "command"; key: string; action: CommandPaletteAction }
  | { kind: "session"; key: string; sessionId: string; label: string; detail: string }
  | { kind: "file"; key: string; path: string; label: string; detail: string }
  | { kind: "model"; key: string; model: string; label: string; detail: string; current: boolean };

export type PaletteSectionId = "commands" | "sessions" | "files" | "models";

export type PaletteSection = { id: PaletteSectionId; title: string; items: PaletteItem[] };

export type PaletteSources = {
  sessions: readonly Session[];
  /** Full-text hits for the current query (transcript text, not only titles). */
  sessionHits: readonly SessionSearchHit[];
  currentSessionId: string | null;
  files: readonly string[];
  models: readonly string[];
  modelLabels?: Record<string, string>;
  currentModel: string;
  /** False while a pilot swap is in flight: models are not offered. */
  modelsEnabled: boolean;
  nowMs?: number;
};

const SECTION_LIMIT = 6;
const RECENT_SESSIONS = 5;
const SECTION_ORDER: readonly PaletteSectionId[] = ["commands", "sessions", "files", "models"];

type Ranked = { item: PaletteItem; score: number };

function sessionItem(session: Session, nowMs: number | undefined): PaletteItem {
  const parts = [workspaceName(session.workspace_root)];
  const lastActive = session.updated ?? session.created;
  if (lastActive) parts.push(sessionAge(lastActive, nowMs));
  return {
    kind: "session",
    key: `session:${session.id}`,
    sessionId: session.id,
    label: displaySessionListTitle(session.title),
    detail: parts.join(" \u00b7 "),
  };
}

function rankSessions(query: string, sources: PaletteSources): Ranked[] {
  const live = sources.sessions.filter((row) => row.id !== sources.currentSessionId && !row.archived);
  if (!query.trim()) {
    return recentSessionRows(live, sources.currentSessionId, RECENT_SESSIONS).map((row) => ({
      item: sessionItem(row, sources.nowMs),
      score: 0,
    }));
  }
  const byId = new Map(live.map((row) => [row.id, row]));
  const ranked: Ranked[] = [];
  for (const row of live) {
    const score = scoreText(`${row.title || ""} ${workspaceName(row.workspace_root)}`, query);
    if (score !== null) ranked.push({ item: sessionItem(row, sources.nowMs), score });
  }
  const seen = new Set(ranked.map((row) => row.item.key));
  for (const hit of sources.sessionHits) {
    const row = byId.get(hit.session_id);
    if (!row || seen.has(`session:${row.id}`)) continue;
    seen.add(`session:${row.id}`);
    const item = sessionItem(row, sources.nowMs);
    // A transcript hit ranks below any title hit; the snippet says why it matched.
    const snippet = hit.snippet.replace(/^(?:user|assistant|pilot):\s*/i, "");
    ranked.push({ item: snippet && item.kind === "session" ? { ...item, detail: snippet } : item, score: 90 });
  }
  return ranked;
}

/** Path match that favors the file name: exact, prefix, substring, then path. */
export function scoreFilePath(path: string, query: string): number | null {
  const q = query.trim().toLowerCase();
  if (!q) return null;
  const lower = path.toLowerCase();
  const base = lower.slice(lower.lastIndexOf("/") + 1);
  if (base === q) return 300;
  if (base.startsWith(q)) return 260;
  const inBase = base.indexOf(q);
  if (inBase >= 0) return 220 - Math.min(inBase, 20);
  const inPath = lower.indexOf(q);
  if (inPath >= 0) return 180 - Math.min(inPath, 60);
  const tokens = q.split(/\s+/).filter(Boolean);
  if (tokens.length > 1 && tokens.every((token) => lower.includes(token))) return 120;
  return null;
}

function rankFiles(query: string, sources: PaletteSources): Ranked[] {
  if (!query.trim()) return [];
  const ranked: Ranked[] = [];
  for (const path of sources.files) {
    const score = scoreFilePath(path, query);
    if (score === null) continue;
    const cut = path.lastIndexOf("/");
    ranked.push({
      // Shorter paths win ties: src/App.tsx before src/legacy/old/App.tsx.
      score: score - Math.min(path.length, 200) / 1000,
      item: {
        kind: "file",
        key: `file:${path}`,
        path,
        label: cut >= 0 ? path.slice(cut + 1) : path,
        detail: cut >= 0 ? path.slice(0, cut) : "",
      },
    });
  }
  return ranked;
}

function rankModels(query: string, sources: PaletteSources): Ranked[] {
  if (!query.trim() || !sources.modelsEnabled) return [];
  const ranked: Ranked[] = [];
  for (const model of sources.models) {
    const label = modelLabelOf(model, sources.modelLabels) || model;
    const provider = providerLabelOf(providerOf(model));
    const score = scoreText(`${label} ${provider} ${model}`, query);
    if (score === null) continue;
    ranked.push({
      score,
      item: {
        kind: "model",
        key: `model:${model}`,
        model,
        label,
        detail: provider,
        current: model === sources.currentModel,
      },
    });
  }
  return ranked;
}

function rankCommands(query: string): Ranked[] {
  return filterCommandPaletteActions(COMMAND_PALETTE_ACTIONS, query).map((action) => ({
    item: { kind: "command", key: `command:${action.id}`, action },
    score: scoreCommandPaletteMatch(action, query) ?? 0,
  }));
}

const SECTION_TITLES: Record<PaletteSectionId, string> = {
  commands: "Commands",
  sessions: "Sessions",
  files: "Files",
  models: "Models",
};

/**
 * Sections for one query. With no query: recent sessions, then commands.
 * With a query: each kind keeps its best matches, and the section with the
 * best match comes first (ties keep commands, sessions, files, models).
 */
export function buildPaletteSections(query: string, sources: PaletteSources): PaletteSection[] {
  const ranked: Record<PaletteSectionId, Ranked[]> = {
    commands: rankCommands(query),
    sessions: rankSessions(query, sources),
    files: rankFiles(query, sources),
    models: rankModels(query, sources),
  };
  const empty = !query.trim();
  const sections = SECTION_ORDER.map((id) => {
    const rows = empty ? ranked[id] : ranked[id].slice().sort((a, b) => b.score - a.score);
    const items = (id === "commands" ? rows : rows.slice(0, SECTION_LIMIT)).map((row) => row.item);
    return {
      section: { id, title: empty && id === "sessions" ? "Recent sessions" : SECTION_TITLES[id], items },
      best: rows.length ? Math.max(...rows.map((row) => row.score)) : -Infinity,
    };
  }).filter((row) => row.section.items.length > 0);
  if (empty) sections.sort((a, b) => Number(b.section.id === "sessions") - Number(a.section.id === "sessions"));
  else sections.sort((a, b) => b.best - a.best);
  return sections.map((row) => row.section);
}
