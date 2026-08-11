/**
 * Curated Cmd/Ctrl-K operator actions for Marionette.
 * Discovers existing event-bus / settings hooks — not a plugin marketplace.
 */

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
  | "open-mcp";

export type CommandPaletteAction = {
  id: CommandPaletteActionId;
  label: string;
  /** Extra terms for fuzzy filter (space-separated). */
  keywords: string;
};

/** Stable curated list — keep small and honest. */
export const COMMAND_PALETTE_ACTIONS: CommandPaletteAction[] = [
  { id: "new-session", label: "New session", keywords: "chat create /new" },
  { id: "clear-transcript", label: "Clear transcript", keywords: "reset visible /clear" },
  { id: "focus-composer", label: "Focus composer", keywords: "input chat prompt" },
  { id: "open-swarm", label: "Open Swarm", keywords: "jobs workers" },
  { id: "open-settings", label: "Open Settings", keywords: "preferences config" },
  { id: "open-terminal", label: "Open Terminal", keywords: "shell console" },
  { id: "open-state", label: "Open State", keywords: "files pane status" },
  { id: "open-files", label: "Open Files", keywords: "tree workspace" },
  { id: "compact-now", label: "Compact Now", keywords: "context summarize /compact" },
  { id: "toggle-right-dock", label: "Toggle right dock", keywords: "panel side" },
  { id: "toggle-left-rail", label: "Toggle left rail", keywords: "sessions sidebar" },
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
  const q = query.trim().toLowerCase();
  if (!q) return 0;
  const haystack = `${action.label} ${action.keywords} ${action.id}`.toLowerCase();
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
      return;
    case "open-mcp":
      window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "mcp" }));
      return;
    default: {
      const _exhaustive: never = id;
      void _exhaustive;
    }
  }
}
