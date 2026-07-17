export const SLASH_COMMANDS = [
  { cmd: "/clear", desc: "Clear visible transcript" },
  { cmd: "/new", desc: "Clear visible transcript (new session)" },
  { cmd: "/compact", desc: "Trigger manual context compaction" },
  { cmd: "/model", desc: "Focus model picker to switch models" },
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
