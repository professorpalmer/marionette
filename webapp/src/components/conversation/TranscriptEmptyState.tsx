/** Empty / loading placeholders for the transcript feed. */
export default function TranscriptEmptyState({
  transcriptStale,
  itemCount,
}: {
  transcriptStale: boolean;
  itemCount: number;
}) {
  if (itemCount === 0 && !transcriptStale) {
    return (
      <div className="text-muted text-ui-13 mt-32 text-center leading-relaxed">
        Message the pilot. It plans, investigates via swarms, and explains.
      </div>
    );
  }
  // Cold cache miss: keep the seating shell quiet. A "Loading session…" label
  // plus opacity dim was the visible swap blink (worse cold→cold). Prefetch +
  // warm cache still hydrate instantly; this only covers the miss paint.
  if (transcriptStale && itemCount === 0) {
    return (
      <div
        className="sr-only"
        role="status"
        aria-live="polite"
        aria-busy="true"
        aria-label="Loading session"
      />
    );
  }
  return null;
}
