/**
 * Where a command shows:
 * - "menu": in the slash menu.
 * - "expert": in the slash menu only in expert mode (internal tools).
 * - "typed": runs when typed; Cmd+K owns its menu entry, so the slash menu
 *   shows it only on an exact name match.
 */
export type SlashVisibility = "menu" | "expert" | "typed";

export type SlashCommand = { cmd: string; desc: string; visibility?: SlashVisibility };

/** Default menu order: most useful first. Use counts reorder it per viewer. */
export const SLASH_COMMANDS: SlashCommand[] = [
  { cmd: "/new", desc: "Start a new session" },
  { cmd: "/compact", desc: "Compact older history to free context" },
  { cmd: "/clear", desc: "Clear the visible transcript" },
  { cmd: "/todo", desc: "View or edit this session's task list" },
  { cmd: "/advise", desc: "Ask a second model for one opinion (no tools)" },
  { cmd: "/routines", desc: "List, pause, or edit scheduled routines" },
  { cmd: "/privacy", desc: "List or set file patterns the pilot must not read" },
  { cmd: "/help", desc: "Open the command palette (Cmd+K)" },
  { cmd: "/images-strip", desc: "Remove image attachments from session history" },
  { cmd: "/advice", desc: "Show the second opinions from this session" },
  { cmd: "/model", desc: "Open the model picker (Cmd+K)", visibility: "typed" },
  { cmd: "/refine", desc: "Propose a harness refine (internal)", visibility: "expert" },
  { cmd: "/ping", desc: "Keep the session cache warm: start, stop, or status", visibility: "expert" },
  { cmd: "/swarm", desc: "Open Jobs (Cmd+K)", visibility: "typed" },
  { cmd: "/terminal", desc: "Open Terminal (Cmd+K)", visibility: "typed" },
  { cmd: "/settings", desc: "Open Settings (Cmd+K)", visibility: "typed" },
  { cmd: "/memory", desc: "Open Memory (Cmd+K)", visibility: "typed" },
  { cmd: "/mcp", desc: "Open MCP (Cmd+K)", visibility: "typed" },
  { cmd: "/files", desc: "Open Files (Cmd+K)", visibility: "typed" },
  { cmd: "/state", desc: "Open State (Cmd+K)", visibility: "typed" },
];

/** Rows the menu shows before the user types a filter. */
export const SLASH_MENU_LIMIT = 8;

export type SlashMenuPrefs = {
  expert: boolean;
  /** Times each command was picked from the menu, keyed by "/cmd". */
  uses: Readonly<Record<string, number>>;
};

export type SlashMenu = {
  items: SlashCommand[];
  /** Listed commands left out by the limit; the user can type to reach them. */
  moreCount: number;
  /** Expert-only commands the current mode hides. */
  internalCount: number;
};

/**
 * The slash menu for one query. An empty query shows the top
 * SLASH_MENU_LIMIT commands by use (ties keep the default order). A typed
 * query shows every prefix match; a hidden command shows on an exact name.
 */
export function slashMenu(all: SlashCommand[], query: string, prefs: SlashMenuPrefs): SlashMenu {
  const order = new Map(all.map((c, i) => [c.cmd, i]));
  const rank = (a: SlashCommand, b: SlashCommand) =>
    (prefs.uses[b.cmd] ?? 0) - (prefs.uses[a.cmd] ?? 0) || (order.get(a.cmd) ?? 0) - (order.get(b.cmd) ?? 0);
  const listed = (c: SlashCommand) => {
    const visibility = c.visibility ?? "menu";
    return visibility === "menu" || (visibility === "expert" && prefs.expert);
  };
  const internalCount = prefs.expert ? 0 : all.filter((c) => c.visibility === "expert").length;
  const typed = "/" + query.toLowerCase();
  if (!query) {
    const candidates = all.filter(listed).sort(rank);
    return {
      items: candidates.slice(0, SLASH_MENU_LIMIT),
      moreCount: Math.max(0, candidates.length - SLASH_MENU_LIMIT),
      internalCount,
    };
  }
  const items = all
    .filter((c) => {
      const name = c.cmd.toLowerCase();
      return listed(c) ? name.startsWith(typed) : name === typed;
    })
    .sort(rank);
  return { items, moreCount: 0, internalCount };
}

export type MentionListingCap = {
  total?: number;
  capped?: number;
  folders_total?: number;
  folders_capped?: number;
};

export function formatMentionListingCapMessage(meta: MentionListingCap): string {
  const { total, capped, folders_total, folders_capped } = meta;
  const parts: string[] = [];
  if (typeof total === "number" && typeof capped === "number" && total > capped) {
    parts.push(`Showing ${capped.toLocaleString()} of ${total.toLocaleString()} files`);
  } else if (typeof capped === "number" && (typeof total !== "number" || total > capped)) {
    parts.push(`File listing capped at ${capped.toLocaleString()} files`);
  }
  if (
    typeof folders_total === "number"
    && typeof folders_capped === "number"
    && folders_total > folders_capped
  ) {
    parts.push(`Showing ${folders_capped.toLocaleString()} of ${folders_total.toLocaleString()} folders`);
  }
  if (parts.length) return parts.join(". ");
  return "File listing is capped for large workspaces";
}

/** The sample command that command_store.py seeds into a new commands dir. */
function isSeedExample(c: { name: string; description: string }): boolean {
  return c.name === "example" && c.description === "Example custom command";
}

/** Merge built-in slash commands with custom /commands from the harness. */
export function mergeSlashCommands(
  custom: { name: string; description: string; scope?: string }[],
): SlashCommand[] {
  return [
    ...SLASH_COMMANDS,
    ...custom.map((c): SlashCommand => {
      const command = { cmd: "/" + c.name, desc: c.description + " (custom)" };
      return isSeedExample(c) ? { ...command, visibility: "expert" } : command;
    }),
  ];
}

export function isBuiltInSlashCommand(cmd: string): boolean {
  return SLASH_COMMANDS.some((s) => s.cmd === cmd);
}
