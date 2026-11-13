/** Composer context meter: a ring for window fill, a tick at the auto-compaction point. */
import type { ContextUsageResponse } from "../../lib/api";

export type ContextRingTone = "normal" | "warn" | "risk";

export type ContextRingFacts = {
  /** Fill of the context window, 0..1. */
  fraction: number;
  /** Where automatic compaction starts, 0..1, or null when the server did not say. */
  triggerFraction: number | null;
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
  const triggerFraction = trigger === null ? null : Math.min(1, trigger / usage.limit);
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
    : `Auto-compaction starts near ${formatTokenCount(trigger)} tokens (${Math.round((triggerFraction ?? 0) * 100)}%)`);
  lines.push("Click for the breakdown");
  return { fraction, triggerFraction, label, tone, tooltip: lines.join("\n") };
}

const TONE_CLASS: Record<ContextRingTone, string> = {
  normal: "text-accent",
  warn: "text-warn",
  risk: "text-risk",
};

const RADIUS = 5;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

/** 14px ring. Track uses the edge token; the arc and tick take the tone color. */
export function ContextRing({ facts }: { facts: ContextRingFacts | null }) {
  const fraction = facts?.fraction ?? 0;
  const tick = facts?.triggerFraction;
  // Angle 0 is 12 o'clock; the arc runs clockwise.
  const tickAngle = tick == null ? null : tick * 2 * Math.PI - Math.PI / 2;
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 14 14"
      aria-hidden="true"
      data-testid="context-ring"
      className={`shrink-0 ${facts ? TONE_CLASS[facts.tone] : "text-faint"}`}
    >
      <circle cx="7" cy="7" r={RADIUS} fill="none" className="stroke-edge2" strokeWidth="2" />
      {fraction > 0 && (
        <circle
          cx="7"
          cy="7"
          r={RADIUS}
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeDasharray={`${Math.max(0.6, fraction * CIRCUMFERENCE)} ${CIRCUMFERENCE}`}
          transform="rotate(-90 7 7)"
          data-testid="context-ring-arc"
        />
      )}
      {tickAngle !== null && (
        <line
          x1={7 + (RADIUS - 2) * Math.cos(tickAngle)}
          y1={7 + (RADIUS - 2) * Math.sin(tickAngle)}
          x2={7 + (RADIUS + 2) * Math.cos(tickAngle)}
          y2={7 + (RADIUS + 2) * Math.sin(tickAngle)}
          stroke="currentColor"
          strokeWidth="1"
          opacity="0.7"
          data-testid="context-ring-tick"
        />
      )}
    </svg>
  );
}
