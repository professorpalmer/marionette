/** Composer context meter: a ring for window fill. The tooltip names the auto-compaction point. */
import type { ContextUsageResponse } from "../../lib/api";

export type ContextRingTone = "normal" | "warn" | "risk";

export type ContextRingFacts = {
  /** Fill of the context window, 0..1. */
  fraction: number;
  /** Short text beside the ring, for example "12%" or "<1%". */
  label: string;
  tone: ContextRingTone;
  /** Native tooltip text (one fact per line). */
  tooltip: string;
};

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);

/** "950", "6.6k", "1.05M": compact token counts for the tooltip. */
export function formatTokenCount(tokens: number): string {
  if (!finite(tokens) || tokens <= 0) return "0";
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(2).replace(/\.?0+$/, "")}M`;
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return String(Math.round(tokens));
}

export function contextRingFacts(usage: ContextUsageResponse | null): ContextRingFacts | null {
  if (!usage || !finite(usage.total) || !finite(usage.limit) || usage.limit <= 0) return null;
  const total = Math.max(0, usage.total);
  const fraction = Math.min(1, total / usage.limit);
  const percent = Math.round(fraction * 100);
  const label = total > 0 && percent === 0 ? "<1%" : `${percent}%`;
  const trigger = finite(usage.compaction_trigger_tokens) && usage.compaction_trigger_tokens > 0
    ? usage.compaction_trigger_tokens
    : null;
  const advice = usage.compaction_advice;
  const pressured = Boolean(advice && !advice.acked_manual_compact && (advice.needs_intervention || advice.level === "now"));
  const nearTrigger = trigger !== null && total >= trigger * 0.9;
  const tone: ContextRingTone = pressured || (trigger !== null && total >= trigger)
    ? "risk"
    : nearTrigger || advice?.level === "soon"
      ? "warn"
      : "normal";
  const lines = [`Context: ${formatTokenCount(total)} of ${formatTokenCount(usage.limit)} tokens (${label})`];
  lines.push(trigger === null
    ? "Auto-compaction point: unknown"
    : `Auto-compaction starts near ${formatTokenCount(trigger)} tokens (${Math.round(Math.min(1, trigger / usage.limit) * 100)}%)`);
  lines.push("Click for the breakdown");
  return { fraction, label, tone, tooltip: lines.join("\n") };
}

const TONE_CLASS: Record<ContextRingTone, string> = {
  normal: "text-accent",
  warn: "text-warn",
  risk: "text-risk",
};

const CENTER = 7;
const RADIUS = 5;

/**
 * Part of the full turn that the arc covers for a window fill.
 *
 * At 20% and above the arc is the fill. Below 20% the arc is longer than the
 * fill, so that 1%, 4% and 5% stay visible and different on a 14px ring
 * (1% = 0.044, 4% = 0.086, 5% = 0.1, 20% = 0.2). The label shows the exact
 * number. The curve is continuous and always increases.
 */
export function ringSweep(fraction: number): number {
  if (!finite(fraction) || fraction <= 0) return 0;
  if (fraction >= 1) return 1;
  if (fraction <= 0.05) return 0.03 + 1.4 * fraction;
  if (fraction < 0.2) return 0.1 + (fraction - 0.05) * (0.1 / 0.15);
  return fraction;
}

const round3 = (value: number) => Math.round(value * 1000) / 1000;

/** Point on the ring at a part of the full turn: 0 is 12 o'clock, clockwise. */
export function ringPoint(sweep: number, center = CENTER, radius = RADIUS): { x: number; y: number } {
  const angle = sweep * 2 * Math.PI;
  return { x: round3(center + radius * Math.sin(angle)), y: round3(center - radius * Math.cos(angle)) };
}

/**
 * SVG path for an arc from 12 o'clock, clockwise, over `sweep` of the turn.
 * Null for an empty arc. A full turn is a circle, which one arc command cannot draw.
 */
export function ringArcPath(sweep: number, center = CENTER, radius = RADIUS): string | null {
  if (!(sweep > 0) || sweep >= 1) return null;
  const start = ringPoint(0, center, radius);
  const end = ringPoint(sweep, center, radius);
  const largeArc = sweep > 0.5 ? 1 : 0;
  return `M ${start.x} ${start.y} A ${radius} ${radius} 0 ${largeArc} 1 ${end.x} ${end.y}`;
}

/** 14px ring. The track and arc take the tone color; the track is dimmed. */
export function ContextRing({ facts }: { facts: ContextRingFacts | null }) {
  const sweep = ringSweep(facts?.fraction ?? 0);
  const arc = ringArcPath(sweep);
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 14 14"
      aria-hidden="true"
      data-testid="context-ring"
      className={`shrink-0 ${facts ? TONE_CLASS[facts.tone] : "text-faint"}`}
    >
      <circle cx={CENTER} cy={CENTER} r={RADIUS} fill="none" stroke="currentColor" strokeOpacity="0.3" strokeWidth="2" />
      {sweep >= 1 ? (
        <circle
          cx={CENTER}
          cy={CENTER}
          r={RADIUS}
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          data-testid="context-ring-arc"
        />
      ) : arc ? (
        <path d={arc} fill="none" stroke="currentColor" strokeWidth="2" data-testid="context-ring-arc" />
      ) : null}
    </svg>
  );
}
