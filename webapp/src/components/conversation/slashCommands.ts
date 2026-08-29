export const SLASH_COMMANDS = [
  { cmd: "/clear", desc: "Clear visible transcript" },
  { cmd: "/new", desc: "Start a new session" },
  { cmd: "/compact", desc: "Trigger manual context compaction" },
  { cmd: "/refine", desc: "Propose a harness refine (existing controller)" },
  { cmd: "/todo", desc: "View or edit the session nested TODO tree" },
  { cmd: "/model", desc: "Focus model picker to switch models" },
  { cmd: "/swarm", desc: "Focus Swarm tab" },
  { cmd: "/terminal", desc: "Focus Terminal" },
  { cmd: "/settings", desc: "Focus Settings" },
  { cmd: "/memory", desc: "Open Memory (Settings → Advanced)" },
  { cmd: "/mcp", desc: "Focus MCP (State + expand MCP)" },
  { cmd: "/files", desc: "Focus Files" },
  { cmd: "/state", desc: "Focus State" },
  { cmd: "/help", desc: "Render a small help note" },
];

export type SlashCommand = { cmd: string; desc: string };

export type MentionListingCap = {
  total?: number;
  capped?: number;
};

export function formatMentionListingCapMessage(meta: MentionListingCap): string {
  const { total, capped } = meta;
  if (typeof total === "number" && typeof capped === "number" && total > capped) {
    return `Showing ${capped.toLocaleString()} of ${total.toLocaleString()} files`;
  }
  if (typeof capped === "number") {
    return `File listing capped at ${capped.toLocaleString()} files`;
  }
  return "File listing is capped for large workspaces";
}

/** Merge built-in slash commands with custom /commands from the harness. */
export function mergeSlashCommands(
  custom: { name: string; description: string; scope?: string }[],
): SlashCommand[] {
  return [
    ...SLASH_COMMANDS,
    ...custom.map((c) => ({
      cmd: "/" + c.name,
      desc: c.description + " (custom)",
    })),
  ];
}

export function isBuiltInSlashCommand(cmd: string): boolean {
  return SLASH_COMMANDS.some((s) => s.cmd === cmd);
}
