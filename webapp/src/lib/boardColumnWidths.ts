/** N-way right-board column widths. Index 0 is the rightmost column. */

export const CARD_LAYOUT_STORAGE_KEY = "pmharness.board.cardLayouts.v1";
export const GRID_COLUMN_COUNT = 12;
export const MIN_CARD_COLUMN_SPAN = 1;

export function minGroupWidth(groupCount: number): number {
  return groupCount <= 4 ? 2 : 1;
}

export function clampCardColumnSpan(value: number, fallback: number): number {
  if (!Number.isFinite(value)) return fallback;
  return Math.max(MIN_CARD_COLUMN_SPAN, Math.min(GRID_COLUMN_COUNT, Math.round(value)));
}

/** Left-edge handle on every column except the leftmost (highest index). */
export function showColumnResizeHandle(groupIndex: number, groupCount: number): boolean {
  return groupCount > 1 && groupIndex >= 0 && groupIndex < groupCount - 1;
}

export function normalizeGroupWidths(requested: number[], preferredGroupIndex: number): number[] {
  if (requested.length === 0) return [];
  const minimumWidth = minGroupWidth(requested.length);
  const widths = requested.map(width => Math.max(minimumWidth, Math.min(GRID_COLUMN_COUNT, width)));
  const totalRequested = widths.reduce((total, width) => total + width, 0);

  if (totalRequested <= GRID_COLUMN_COUNT) {
    let remaining = GRID_COLUMN_COUNT - totalRequested;
    const order = [...widths.keys()].sort((a, b) => {
      if (a === preferredGroupIndex) return -1;
      if (b === preferredGroupIndex) return 1;
      return a - b;
    });
    let cursor = 0;
    while (remaining > 0) {
      widths[order[cursor % order.length]] += 1;
      remaining -= 1;
      cursor += 1;
    }
    return widths;
  }

  const primaryIndex = preferredGroupIndex >= 0 && preferredGroupIndex < widths.length
    ? preferredGroupIndex
    : widths.indexOf(Math.max(...widths));
  const primaryWidth = Math.min(widths[primaryIndex], GRID_COLUMN_COUNT - minimumWidth * (widths.length - 1));
  const normalized = widths.map(() => minimumWidth);
  normalized[primaryIndex] = primaryWidth;
  let remaining = GRID_COLUMN_COUNT - primaryWidth - minimumWidth * (widths.length - 1);
  const secondaryOrder = [...widths.keys()]
    .filter(index => index !== primaryIndex)
    .sort((a, b) => widths[b] - widths[a]);

  for (const index of secondaryOrder) {
    if (remaining <= 0) break;
    const extraCapacity = Math.max(0, widths[index] - minimumWidth);
    const extra = Math.min(extraCapacity, remaining);
    normalized[index] += extra;
    remaining -= extra;
  }
  return normalized;
}

/** Grow/shrink one column against its left neighbor only. Other columns stay put. */
export function applyPairwiseColumnResize(
  widths: readonly number[],
  groupIndex: number,
  nextSpan: number,
): number[] {
  const groupCount = widths.length;
  if (groupCount <= 1) return widths.slice();
  const minWidth = minGroupWidth(groupCount);
  const neighborIndex = groupIndex + 1;
  if (neighborIndex >= groupCount) return widths.slice();
  const current = widths.map(width => Math.max(minWidth, Math.min(GRID_COLUMN_COUNT, Math.round(width))));
  const pairTotal = current[groupIndex] + current[neighborIndex];
  const span = Number.isFinite(nextSpan) ? Math.round(nextSpan) : current[groupIndex];
  const primary = Math.max(minWidth, Math.min(pairTotal - minWidth, span));
  const next = current.slice();
  next[groupIndex] = primary;
  next[neighborIndex] = pairTotal - primary;
  return next;
}
