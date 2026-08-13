/** Vertical split between two stacked right-pane cards (top fraction 0–1). */

export const DEFAULT_STACK_SPLIT = 0.5;
export const MIN_STACK_SPLIT = 0.1;
export const MAX_STACK_SPLIT = 0.9;
export const STACK_SPLIT_STEP = 0.05;
export const STACK_SPLIT_STORAGE_KEY = "pmharness.board.stackSplits.v1";
export const STACK_ROW_RESIZE_LABEL = "Resize stacked panel height";

export function clampStackSplit(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_STACK_SPLIT;
  return Math.min(MAX_STACK_SPLIT, Math.max(MIN_STACK_SPLIT, value));
}

export function stackPairKey(tabs: readonly string[]): string {
  return tabs.filter(Boolean).join("|");
}

/** CSS grid-template-rows for a two-card stack. */
export function stackRowTemplate(split: number): string {
  const clamped = clampStackSplit(split);
  const top = Math.max(1, Math.round(clamped * 100));
  const bottom = Math.max(1, 100 - top);
  return `minmax(0, ${top}fr) minmax(0, ${bottom}fr)`;
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
