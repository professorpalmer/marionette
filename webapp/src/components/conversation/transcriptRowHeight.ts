/**
 * Pretext-backed row height estimates for the virtual transcript feed.
 *
 * prepare(text, font) once per row id+text+font; layout(prepared, maxWidth,
 * lineHeight) is the cheap streaming ruler. Rows with code/images/mermaid
 * still get a DOM measureElement pass after mount settle — Pretext cannot
 * know fenced blocks or media chrome.
 */

import { layout, prepare, type PreparedText } from "@chenglou/pretext";
import type { GroupedItem, Msg } from "../TranscriptList";

/** Canvas font strings synced with TranscriptList bubble typography. */
export const TRANSCRIPT_USER_FONT =
  'normal 13px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
export const TRANSCRIPT_ASSISTANT_FONT =
  'normal 13px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
export const TRANSCRIPT_CHIP_FONT =
  'normal 10.5px ui-monospace, SFMono-Regular, Menlo, monospace';

export const TRANSCRIPT_USER_LINE_HEIGHT_PX = 21;
export const TRANSCRIPT_ASSISTANT_LINE_HEIGHT_PX = 22;
export const TRANSCRIPT_CHIP_LINE_HEIGHT_PX = 16;

/** Cursor-style clamp for long pasted user messages (matches Bubble). */
export const TRANSCRIPT_USER_CLAMP_PX = 160;

/** Fallback when Pretext or extraction cannot run (jsdom without canvas). */
export const TRANSCRIPT_ROW_FALLBACK_PX = 72;

const FEED_COLUMN_MAX_PX = 768;
const FEED_HORIZONTAL_PADDING_PX = 48;

/** Fixed chrome for chip / status rows that never use Pretext body text. */
const FIXED_ROW_HEIGHT_PX: Partial<Record<GroupedItem["kind"], number>> = {
  codegraph_context: 28,
  vault_cite: 36,
  command_blocked: 32,
  auto_status: 32,
  auto_halt: 32,
  verifying: 32,
  auto_verify: 32,
  verification: 32,
  quality_gate: 32,
  turn_terminal: 36,
  steer: 28,
  checkpoint: 32,
  pending_review: 40,
  compaction: 36,
};

export type TranscriptRowPretextSpec = {
  text: string;
  font: string;
  lineHeight: number;
  maxWidth: number;
  whiteSpace?: "normal" | "pre-wrap";
  /** Extra pixels beyond layout height (label, padding, buttons). */
  chromePx: number;
  /** Cap prose height (user clamp). */
  maxProsePx?: number;
};

export function transcriptFeedInnerWidth(scrollClientWidth: number): number {
  const column = Math.min(FEED_COLUMN_MAX_PX, scrollClientWidth) - FEED_HORIZONTAL_PADDING_PX;
  return Math.max(240, column);
}

export function transcriptBubbleMaxWidth(
  feedInnerWidth: number,
  role: Msg["role"],
): number {
  const ratio = role === "user" ? 0.85 : 0.95;
  return Math.floor(feedInnerWidth * ratio);
}

export function transcriptRowCacheKey(
  rowId: string,
  text: string,
  font: string,
  whiteSpace: "normal" | "pre-wrap" = "normal",
): string {
  return `${rowId}\0${font}\0${whiteSpace}\0${text}`;
}

export function hasMarkdownCodeFence(text: string): boolean {
  return /```/.test(text);
}

export function hasMermaidFence(text: string): boolean {
  return /```\s*mermaid/i.test(text);
}

export function hasMarkdownImage(text: string): boolean {
  return /!\[[^\]]*\]\([^)]+\)/.test(text);
}

/** Strip assistant traceback noise — mirrors Bubble's cleanAssistantText (height-only). */
export function assistantTextForMeasure(raw: string): string {
  const lines = raw.split("\n");
  const cleaned: string[] = [];
  let inTraceback = false;

  for (const line of lines) {
    const stripped = line.trim();
    if (stripped.startsWith("USER: (") || stripped.includes("completed with exit code")) {
      continue;
    }
    if (/^\s*Traceback\s*\(most\s+recent\s+call\s+last\):/i.test(stripped)) {
      inTraceback = true;
      continue;
    }
    if (inTraceback) {
      if (stripped === "") continue;
      if (line.startsWith(" ") || line.startsWith("\t")) continue;
      inTraceback = false;
      continue;
    }
    if (
      stripped.includes("During handling of the above exception")
      || stripped.includes("The above exception was the direct cause")
    ) {
      continue;
    }
    cleaned.push(line);
  }

  let result = cleaned.join("\n").trim();
  result = result.replace(/\n{3,}/g, "\n\n");
  // Match Bubble cleanAssistantText — empty after strip stays empty (never
  // "Working...", which must not leak into fold chrome or height placeholders).
  return result;
}

function isPlanOrProgressAssistant(msg: Msg): boolean {
  return Boolean(msg.isPlan) || msg.channel === "progress";
}

/**
 * Rows Pretext cannot fully model — these get measureElement after mount settle.
 */
export function rowNeedsDomMeasure(item: GroupedItem): boolean {
  switch (item.kind) {
    case "msg": {
      const msg = item.msg;
      if (msg.images?.length) return true;
      if (msg.workerStream) return true;
      const text =
        msg.role === "user" ? msg.text : assistantTextForMeasure(msg.text);
      if (hasMarkdownCodeFence(text)) return true;
      if (hasMermaidFence(text)) return true;
      if (hasMarkdownImage(text)) return true;
      return false;
    }
    case "activity_group":
      return true;
    case "command_approval":
    case "secret_request":
    case "auth_failure":
    case "swarm_pending":
    case "swarm_result":
      return true;
    default:
      return false;
  }
}

export function rowPretextSpec(
  item: GroupedItem,
  feedInnerWidth: number,
): TranscriptRowPretextSpec | null {
  switch (item.kind) {
    case "msg": {
      const msg = item.msg;
      if (msg.workerStream) {
        return {
          text: msg.role === "user" ? msg.text : assistantTextForMeasure(msg.text),
          font: TRANSCRIPT_CHIP_FONT,
          lineHeight: 19,
          maxWidth: transcriptBubbleMaxWidth(feedInnerWidth, "assistant"),
          whiteSpace: "pre-wrap",
          chromePx: 56,
        };
      }
      if (msg.role === "user") {
        const imageExtra = (msg.images?.length ?? 0) * 88;
        return {
          text: msg.text,
          font: TRANSCRIPT_USER_FONT,
          lineHeight: TRANSCRIPT_USER_LINE_HEIGHT_PX,
          maxWidth: transcriptBubbleMaxWidth(feedInnerWidth, "user"),
          whiteSpace: "pre-wrap",
          chromePx: 28 + imageExtra,
          maxProsePx: TRANSCRIPT_USER_CLAMP_PX,
        };
      }
      const text = assistantTextForMeasure(msg.text);
      if (isPlanOrProgressAssistant(msg)) {
        return {
          text,
          font: TRANSCRIPT_ASSISTANT_FONT,
          lineHeight: TRANSCRIPT_ASSISTANT_LINE_HEIGHT_PX,
          maxWidth: transcriptBubbleMaxWidth(feedInnerWidth, "assistant"),
          whiteSpace: "pre-wrap",
          chromePx: 24,
        };
      }
      // Markdown prose: measure stripped inline text; fenced blocks use DOM settle.
      const proseOnly = stripMarkdownForPretext(text);
      return {
        text: proseOnly,
        font: TRANSCRIPT_ASSISTANT_FONT,
        lineHeight: TRANSCRIPT_ASSISTANT_LINE_HEIGHT_PX,
        maxWidth: transcriptBubbleMaxWidth(feedInnerWidth, "assistant"),
        whiteSpace: "normal",
        chromePx: 24,
      };
    }
    case "thinking": {
      const preview = item.text.split("\n").find((ln) => ln.trim())?.trim() ?? "";
      return {
        text: preview,
        font: TRANSCRIPT_ASSISTANT_FONT,
        lineHeight: 20,
        maxWidth: feedInnerWidth,
        whiteSpace: "normal",
        chromePx: 20,
      };
    }
    case "auth_failure":
      return {
        text: item.message,
        font: TRANSCRIPT_ASSISTANT_FONT,
        lineHeight: TRANSCRIPT_ASSISTANT_LINE_HEIGHT_PX,
        maxWidth: feedInnerWidth,
        whiteSpace: "pre-wrap",
        chromePx: 56,
      };
    case "steer":
      return {
        text: item.text,
        font: TRANSCRIPT_ASSISTANT_FONT,
        lineHeight: TRANSCRIPT_ASSISTANT_LINE_HEIGHT_PX,
        maxWidth: feedInnerWidth,
        whiteSpace: "pre-wrap",
        chromePx: 16,
      };
    default:
      return null;
  }
}

/** Remove fenced blocks and most markdown chrome so Pretext measures prose only. */
export function stripMarkdownForPretext(text: string): string {
  let out = text.replace(/```[\s\S]*?```/g, " ");
  out = out.replace(/!\[[^\]]*\]\([^)]+\)/g, " ");
  out = out.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
  out = out.replace(/^#{1,6}\s+/gm, "");
  out = out.replace(/(\*\*|__|\*|_|~~|`)/g, "");
  out = out.replace(/\n{3,}/g, "\n\n");
  return out.trim() || " ";
}

export type TranscriptRowHeightCache = {
  estimateRowHeight: (
    item: GroupedItem,
    rowId: string,
    feedInnerWidth: number,
  ) => number;
  clear: () => void;
};

export function createTranscriptRowHeightCache(): TranscriptRowHeightCache {
  const preparedByKey = new Map<string, PreparedText>();

  function layoutHeight(
    rowId: string,
    spec: TranscriptRowPretextSpec,
  ): number {
    const cacheKey = transcriptRowCacheKey(
      rowId,
      spec.text,
      spec.font,
      spec.whiteSpace ?? "normal",
    );
    let handle = preparedByKey.get(cacheKey);
    if (!handle) {
      try {
        handle = prepare(spec.text, spec.font, {
          whiteSpace: spec.whiteSpace ?? "normal",
        });
        preparedByKey.set(cacheKey, handle);
      } catch {
        return TRANSCRIPT_ROW_FALLBACK_PX;
      }
    }
    try {
      const { height, lineCount } = layout(handle, spec.maxWidth, spec.lineHeight);
      const prose =
        lineCount === 0
          ? spec.lineHeight
          : Math.max(height, spec.lineHeight);
      const capped =
        spec.maxProsePx != null
          ? Math.min(prose, spec.maxProsePx)
          : prose;
      return Math.max(24, Math.ceil(capped + spec.chromePx));
    } catch {
      return TRANSCRIPT_ROW_FALLBACK_PX;
    }
  }

  function estimateRowHeight(
    item: GroupedItem,
    rowId: string,
    feedInnerWidth: number,
  ): number {
    const fixed = FIXED_ROW_HEIGHT_PX[item.kind];
    if (fixed != null) return fixed;

    const spec = rowPretextSpec(item, feedInnerWidth);
    if (!spec) {
      return rowNeedsDomMeasure(item)
        ? TRANSCRIPT_ROW_FALLBACK_PX * 2
        : TRANSCRIPT_ROW_FALLBACK_PX;
    }
    return layoutHeight(rowId, spec);
  }

  return {
    estimateRowHeight,
    clear: () => preparedByKey.clear(),
  };
}

/** Signal that changes on every stream token / fold membership so rows remasure. */
export function rowMeasureSignal(item: GroupedItem): string {
  switch (item.kind) {
    case "msg":
      return `msg:${item.msg.streaming ? 1 : 0}:${item.msg.workerStream ? 1 : 0}:${item.msg.text.length}:${item.msg.text.slice(-32)}`;
    case "thinking":
      return `think:${item.streaming ? 1 : 0}:${item.text.length}:${item.text.slice(-32)}`;
    case "activity_group":
      return `fold:${item.items.length}`;
    default:
      return item.kind;
  }
}

/** Immediate attach — skip the 2-rAF settle so tokens / folds remasure this frame. */
export function shouldRemeasureImmediately(item: GroupedItem): boolean {
  if (item.kind === "activity_group" || item.kind === "thinking") return true;
  if (item.kind === "msg" && (item.msg.streaming || item.msg.workerStream)) return true;
  return false;
}

/** Whether DOM measureElement may attach for this row (after mount settle). */
export function shouldAttachDomMeasure(
  item: GroupedItem,
  feedSettled: boolean,
): boolean {
  // Investigating / Explored folds and live stream rows change height in place.
  // Attach measureElement even before feed settle so tokens / expand cannot
  // paint over the next virtual row.
  if (shouldRemeasureImmediately(item)) return true;
  return feedSettled && rowNeedsDomMeasure(item);
}
