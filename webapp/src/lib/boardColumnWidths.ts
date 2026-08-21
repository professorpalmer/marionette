/** N-way right-board column widths. Index 0 is the rightmost column. */

export const CARD_LAYOUT_STORAGE_KEY = "pmharness.board.cardLayouts.v1";
export const GRID_COLUMN_COUNT = 12;
export const MIN_CARD_COLUMN_SPAN = 1;

export function minGroupWidth(groupCount: number): number {
  return groupCount <= 4 ? 2 : 1;
}

export function clampCardColumnSpan(value: number, fallback: number): number {
  if (!Number.isFinite(value)) return fallback;
  return Math.max(MIN_CARD_COLUMN_SPAN, Math.min(GRID_COLUMN_COUNT, value));
}

/** Left-edge handle on every column except the leftmost (highest index). */
export function showColumnResizeHandle(groupIndex: number, groupCount: number): boolean {
  return groupCount > 1 && groupIndex >= 0 && groupIndex < groupCount - 1;
}

/** Visual CSS grid column (1-based, left-to-right) for a right-indexed group. */
export function groupGridColumn(groupIndex: number, groupCount: number): string {
  if (groupCount <= 1) return "1";
  return String(groupCount - groupIndex);
}

/**
 * Track list for the board grid, visual left-to-right.
 * The leftmost column is 1fr so shell-resize grows only that column; neighbors
 * keep a pixel width once the board has been measured.
 */
export function columnTrackTemplate(widths: readonly number[], boardWidth = 0): string {
  if (widths.length <= 1) return "minmax(0, 1fr)";
  const total = widths.reduce((sum, width) => sum + width, 0) || 1;
  const visual = widths.slice().reverse();
  if (!Number.isFinite(boardWidth) || boardWidth <= 0) {
    return visual.map(width => `minmax(0, ${width}fr)`).join(" ");
  }
  return visual.map((width, visualIndex) => {
    if (visualIndex === 0) return "minmax(0, 1fr)";
    return `${(width / total) * boardWidth}px`;
  }).join(" ");
}

export function columnSpanFromPointerDelta(opts: {
  startSpan: number;
  startClientX: number;
  clientX: number;
  boardWidth: number;
}): number {
  if (!Number.isFinite(opts.boardWidth) || opts.boardWidth <= 0) return opts.startSpan;
  return opts.startSpan + (opts.startClientX - opts.clientX) / (opts.boardWidth / GRID_COLUMN_COUNT);
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
    while (remaining >= 1) {
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
  const current = widths.map(width => Math.max(minWidth, Math.min(GRID_COLUMN_COUNT, width)));
  const pairTotal = current[groupIndex] + current[neighborIndex];
  const span = Number.isFinite(nextSpan) ? nextSpan : current[groupIndex];
  const primary = Math.max(minWidth, Math.min(pairTotal - minWidth, span));
  const next = current.slice();
  next[groupIndex] = primary;
  next[neighborIndex] = pairTotal - primary;
  return next;
}

/**
 * When the shell resizer changes board width, keep every column except the
 * leftmost at its previous pixel size. Extra (or missing) width goes to the
 * leftmost column so dragging the left edge does not scale the whole board.
 */
export function absorbShellResize(
  widths: readonly number[],
  oldBoardWidth: number,
  newBoardWidth: number,
): number[] {
  if (widths.length <= 1) return widths.slice();
  if (!(oldBoardWidth > 0 && newBoardWidth > 0) || oldBoardWidth === newBoardWidth) {
    return widths.slice();
  }
  const minWidth = minGroupWidth(widths.length);
  const total = widths.reduce((sum, width) => sum + width, 0) || 1;
  const px = widths.map(width => (width / total) * oldBoardWidth);
  const leftmost = widths.length - 1;
  const minPx = (minWidth / GRID_COLUMN_COUNT) * newBoardWidth;
  let lockedSum = 0;
  for (let index = 0; index < leftmost; index += 1) lockedSum += px[index];
  let leftPx = newBoardWidth - lockedSum;
  if (leftPx < minPx) {
    let remaining = minPx - leftPx;
    leftPx = minPx;
    for (let index = leftmost - 1; index >= 0 && remaining > 0; index -= 1) {
      const reducible = Math.max(0, px[index] - minPx);
      const take = Math.min(reducible, remaining);
      px[index] -= take;
      remaining -= take;
    }
  }
  px[leftmost] = leftPx;
  const others = px.slice(0, leftmost).map(value => (value / newBoardWidth) * GRID_COLUMN_COUNT);
  const othersSum = others.reduce((sum, value) => sum + value, 0);
  return others.concat(GRID_COLUMN_COUNT - othersSum);
}
