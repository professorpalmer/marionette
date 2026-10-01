import type { GroupedItem } from "../components/TranscriptList";
import { stripMarkdownForPretext } from "../components/conversation/transcriptRowHeight";

/**
 * In-chat find (Cmd/Ctrl+F). The feed is virtualized, so matches come from the
 * row data, not the DOM: off-screen rows have no DOM to search. Navigation
 * scrolls the row into view; painting then highlights the mounted text with
 * the CSS Custom Highlight API, which never touches React-owned nodes.
 */

export type FindMatch = { row: number; occurrence: number };

/** Message prose a reader can see: user/pilot bubbles and steers. "" elsewhere. */
export function searchableRowText(row: GroupedItem): string {
  if (row.kind === "steer") return row.text;
  if (row.kind !== "msg" || row.msg.workerStream) return "";
  return row.msg.role === "assistant" ? stripMarkdownForPretext(row.msg.text) : row.msg.text;
}

function occurrences(haystack: string, needle: string): number {
  if (!needle) return 0;
  let count = 0;
  for (let at = haystack.indexOf(needle); at !== -1; at = haystack.indexOf(needle, at + needle.length)) count++;
  return count;
}

/** Case-insensitive matches in reading order. */
export function findMatches(rowTexts: readonly string[], query: string): FindMatch[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [];
  const matches: FindMatch[] = [];
  rowTexts.forEach((text, row) => {
    const n = occurrences(text.toLowerCase(), needle);
    for (let occurrence = 0; occurrence < n; occurrence++) matches.push({ row, occurrence });
  });
  return matches;
}

/** Step through matches, wrapping at either end. */
export function stepMatch(active: number, total: number, delta: 1 | -1): number {
  if (total <= 0) return -1;
  if (active < 0) return delta > 0 ? 0 : total - 1;
  return (active + delta + total) % total;
}

const ALL = "transcript-find";
const CURRENT = "transcript-find-current";

type HighlightRegistry = { set(name: string, h: unknown): void; delete(name: string): void };

function registry(): HighlightRegistry | null {
  const css = (globalThis as { CSS?: { highlights?: HighlightRegistry } }).CSS;
  const ctor = (globalThis as { Highlight?: unknown }).Highlight;
  return css?.highlights && typeof ctor === "function" ? css.highlights : null;
}

function rangesIn(body: Element, needle: string): Range[] {
  const ranges: Range[] = [];
  const walker = body.ownerDocument.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = (node.nodeValue || "").toLowerCase();
    for (let at = text.indexOf(needle); at !== -1; at = text.indexOf(needle, at + needle.length)) {
      const range = body.ownerDocument.createRange();
      range.setStart(node, at);
      range.setEnd(node, at + needle.length);
      ranges.push(range);
    }
  }
  return ranges;
}

/**
 * Highlight every mounted match under `root`, and the active one distinctly.
 * Returns the active match's range when its row is mounted (for centering).
 */
export function paintFindHighlights(
  root: Element | null,
  query: string,
  active: FindMatch | null,
): Range | null {
  const highlights = registry();
  if (!highlights) return null;
  const needle = query.trim().toLowerCase();
  if (!root || !needle) {
    highlights.delete(ALL);
    highlights.delete(CURRENT);
    return null;
  }
  const HighlightCtor = (globalThis as unknown as { Highlight: new (...r: Range[]) => unknown }).Highlight;
  const all: Range[] = [];
  let current: Range | null = null;
  for (const row of Array.from(root.querySelectorAll<HTMLElement>("[data-row-index]"))) {
    const index = Number(row.dataset.rowIndex);
    const ranges = Array.from(row.querySelectorAll(".transcript-msg-body")).flatMap((body) => rangesIn(body, needle));
    all.push(...ranges);
    if (active && index === active.row && ranges.length) {
      // Rendered markdown can show fewer hits than the raw text counted.
      current = ranges[Math.min(active.occurrence, ranges.length - 1)]!;
    }
  }
  highlights.set(ALL, new HighlightCtor(...all));
  if (current) highlights.set(CURRENT, new HighlightCtor(current));
  else highlights.delete(CURRENT);
  return current;
}

export function clearFindHighlights(): void {
  const highlights = registry();
  highlights?.delete(ALL);
  highlights?.delete(CURRENT);
}
