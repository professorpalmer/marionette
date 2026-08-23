/** Reasoning-effort picker visibility and labels. */

export type ReasoningEffortLevel = {
  value: "none" | "low" | "medium" | "high" | "xhigh" | "max";
  label: string;
};

/** Canonical ladder. Ultra is the label for wire value xhigh. */
export const REASONING_LEVELS: ReasoningEffortLevel[] = [
  { value: "none", label: "None" },
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "xhigh", label: "Ultra" },
  { value: "max", label: "Max" },
];

export function labelForEffort(value: string): string {
  return REASONING_LEVELS.find((l) => l.value === value)?.label || "Low";
}

/**
 * Show the effort knob when:
 * - the backend has not shipped a map yet (old payload / empty) -- fail open
 * - the map has an explicit true for this spec
 *
 * A present map with a missing or false key hides the knob. That stops a
 * catalogued false (haiku, Go with no dialect) from being overridden by
 * `?? true` after a spec-key miss.
 */
export function showReasoningEffort(
  support: Record<string, boolean> | null | undefined,
  spec: string,
): boolean {
  if (support == null) return true;
  const keys = Object.keys(support);
  if (keys.length === 0) return true;
  return support[spec] === true;
}
