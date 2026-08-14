/** Vertical splits for stacked right-pane cards (per-card fractions). */

export const DEFAULT_STACK_SPLIT = 0.5;
export const MIN_STACK_SPLIT = 0.1;
export const MAX_STACK_SPLIT = 0.9;
export const MIN_STACK_FRACTION = 0.1;
export const STACK_SPLIT_STEP = 0.05;
export const STACK_SPLIT_STORAGE_KEY = "pmharness.board.stackSplits.v1";
export const STACK_FRACTIONS_STORAGE_KEY = "pmharness.board.stackFractions.v2";
export const STACK_ROW_RESIZE_LABEL = "Resize stacked panel height";

export function clampStackSplit(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_STACK_SPLIT;
  return Math.min(MAX_STACK_SPLIT, Math.max(MIN_STACK_SPLIT, value));
}

export function stackPairKey(tabs: readonly string[]): string {
  return tabs.filter(Boolean).join("|");
}

export function equalFractions(count: number): number[] {
  if (count <= 0) return [];
  if (count === 1) return [1];
  return Array.from({ length: count }, () => 1 / count);
}

export function normalizeFractions(values: readonly number[], count: number): number[] {
  if (count <= 0) return [];
  if (count === 1) return [1];
  const min = Math.min(MIN_STACK_FRACTION, 1 / count);
  const raw = values.slice(0, count);
  while (raw.length < count) raw.push(1 / count);
  const clamped = raw.map((value) => (
    Number.isFinite(value) ? Math.max(min, value) : 1 / count
  ));
  const sum = clamped.reduce((total, value) => total + value, 0) || 1;
  return clamped.map((value) => value / sum);
}

export function stackRowTemplateN(fractions: readonly number[]): string {
  const normalized = normalizeFractions(fractions, fractions.length);
  return normalized
    .map((value) => `minmax(0, ${Math.max(1, Math.round(value * 100))}fr)`)
    .join(" ");
}

/** CSS grid-template-rows for a two-card stack. */
export function stackRowTemplate(split: number): string {
  const clamped = clampStackSplit(split);
  return stackRowTemplateN([clamped, 1 - clamped]);
}

export function stackSplitFromDrag(opts: {
  startSplit: number;
  startClientY: number;
  clientY: number;
  stackHeight: number;
}): number {
  if (!Number.isFinite(opts.stackHeight) || opts.stackHeight <= 0) {
    return clampStackSplit(opts.startSplit);
  }
  const delta = (opts.clientY - opts.startClientY) / opts.stackHeight;
  return clampStackSplit(opts.startSplit + delta);
}

export function stackSplitFromKey(split: number, key: string): number {
  if (key === "ArrowUp") return clampStackSplit(split - STACK_SPLIT_STEP);
  if (key === "ArrowDown") return clampStackSplit(split + STACK_SPLIT_STEP);
  return clampStackSplit(split);
}

export function fractionsFromBoundaryDrag(opts: {
  fractions: readonly number[];
  boundaryIndex: number;
  startClientY: number;
  clientY: number;
  stackHeight: number;
}): number[] {
  const next = normalizeFractions(opts.fractions, opts.fractions.length);
  const boundary = opts.boundaryIndex;
  if (boundary < 0 || boundary >= next.length - 1) return next;
  if (!Number.isFinite(opts.stackHeight) || opts.stackHeight <= 0) return next;
  const min = Math.min(MIN_STACK_FRACTION, 1 / next.length);
  const pair = next[boundary] + next[boundary + 1];
  const delta = (opts.clientY - opts.startClientY) / opts.stackHeight;
  const top = Math.max(min, Math.min(pair - min, next[boundary] + delta));
  next[boundary] = top;
  next[boundary + 1] = pair - top;
  return normalizeFractions(next, next.length);
}

export function fractionsFromKey(
  fractions: readonly number[],
  boundaryIndex: number,
  key: string,
): number[] {
  if (key !== "ArrowUp" && key !== "ArrowDown") {
    return normalizeFractions(fractions, fractions.length);
  }
  return fractionsFromBoundaryDrag({
    fractions,
    boundaryIndex,
    startClientY: 0,
    clientY: (key === "ArrowDown" ? 1 : -1) * STACK_SPLIT_STEP * 400,
    stackHeight: 400,
  });
}
