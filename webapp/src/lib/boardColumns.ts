/** Right-pane card columns. Index 0 is the rightmost column. */

export const BOARD_COLUMNS_STORAGE_KEY = "pmharness.board.columns.v1";
export const NEW_COLUMN_DROP_LABEL = "Drop to open a column";
export const MIN_MULTI_COLUMN_BOARD_PX = 420;

export function defaultColumns<T>(openCards: readonly T[]): T[][] {
  if (openCards.length === 0) return [];
  return [openCards.slice()];
}

export function flattenColumns<T>(columns: readonly T[][]): T[] {
  return columns.flat();
}

export function reconcileColumns<T>(openCards: readonly T[], columns: readonly T[][]): T[][] {
  const allowed = new Set(openCards);
  const seen = new Set<T>();
  const next: T[][] = [];
  for (const col of columns) {
    const kept = col.filter((tab) => allowed.has(tab) && !seen.has(tab));
    for (const tab of kept) seen.add(tab);
    if (kept.length) next.push(kept);
  }
  const missing = openCards.filter((tab) => !seen.has(tab));
  if (missing.length === 0) return next;
  if (next.length === 0) return [missing.slice()];
  next[0] = [...next[0], ...missing];
  return next;
}

export function columnIndexOf<T>(columns: readonly T[][], card: T): number {
  return columns.findIndex((col) => col.includes(card));
}

export function moveCardIntoColumn<T>(
  columns: readonly T[][],
  card: T,
  destCol: number,
  destIndex: number,
): T[][] {
  const stripped = columns
    .map((col) => col.filter((tab) => tab !== card))
    .filter((col) => col.length > 0);
  if (stripped.length === 0) return [[card]];
  const target = Math.max(0, Math.min(destCol, stripped.length));
  if (target === stripped.length) {
    return [...stripped, [card]];
  }
  const col = stripped[target].slice();
  const idx = Math.max(0, Math.min(destIndex, col.length));
  col.splice(idx, 0, card);
  const next = stripped.slice();
  next[target] = col;
  return next;
}

/** Pull a card out of its stack into a new leftmost column. */
export function extractCardToLeftColumn<T>(columns: readonly T[][], card: T): T[][] {
  const stripped = columns
    .map((col) => col.filter((tab) => tab !== card))
    .filter((col) => col.length > 0);
  if (stripped.length === 0) return [[card]];
  return [...stripped, [card]];
}

export function canOpenLeftColumn<T>(columns: readonly T[][], card: T): boolean {
  const col = columns.find((c) => c.includes(card));
  return Boolean(col && col.length > 1);
}
