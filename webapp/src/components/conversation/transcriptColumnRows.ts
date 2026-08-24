import type { GroupedItem } from "../TranscriptList";

/** Main-column presentation kinds — the feed paints only these four row types. */
export type TranscriptColumnRowKind = "msg" | "question" | "file" | "activity";

/**
 * Map a grouped transcript row to its main-column presentation kind.
 * GroupedItem may retain inner variants (swarm_result, thinking, …) but the
 * column paints only msg / question / file / activity strip.
 */
export function columnRowKind(row: GroupedItem): TranscriptColumnRowKind {
  switch (row.kind) {
    case "msg":
    case "steer":
      return "msg";
    case "command_approval":
    case "secret_request":
      return "question";
    case "pending_review":
      return "file";
    case "activity_group":
      return "activity";
    default:
      return "activity";
  }
}

/** Presentation kinds for every grouped row (same length as input). */
export function columnRowKinds(rows: GroupedItem[]): TranscriptColumnRowKind[] {
  return rows.map(columnRowKind);
}
