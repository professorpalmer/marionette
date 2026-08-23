import type { GroupedItem } from "../TranscriptList";

function isLiveTailRow(
  row: GroupedItem,
  index: number,
  lastLiveActivityIdx: number,
): boolean {
  if (row.kind === "msg" && row.msg.role === "assistant" && row.msg.streaming === true) {
    return true;
  }
  if (row.kind === "thinking" && row.streaming === true) {
    return true;
  }
  if (row.kind === "activity_group" && index === lastLiveActivityIdx) {
    return true;
  }
  return false;
}

export type TranscriptLiveTailPartition = {
  head: GroupedItem[];
  tail: GroupedItem[];
  /** Index in the full grouped array where tail begins (== head.length). */
  tailStartIndex: number;
};

/**
 * Keep streaming / live-investigation rows in normal document flow after the
 * virtual window so token growth pushes footer clearance in the same layout pass.
 */
export function partitionTranscriptLiveTail(
  grouped: GroupedItem[],
  opts: { lastLiveActivityIdx: number; agentLoopOpen: boolean },
): TranscriptLiveTailPartition {
  if (!opts.agentLoopOpen || grouped.length === 0) {
    return { head: grouped, tail: [], tailStartIndex: grouped.length };
  }

  let tailStart = grouped.length;
  for (let i = grouped.length - 1; i >= 0; i -= 1) {
    if (!isLiveTailRow(grouped[i], i, opts.lastLiveActivityIdx)) {
      break;
    }
    tailStart = i;
  }

  if (tailStart >= grouped.length) {
    return { head: grouped, tail: [], tailStartIndex: grouped.length };
  }

  return {
    head: grouped.slice(0, tailStart),
    tail: grouped.slice(tailStart),
    tailStartIndex: tailStart,
  };
}
