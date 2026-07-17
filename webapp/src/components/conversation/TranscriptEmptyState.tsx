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
      <div className="text-muted text-[13px] mt-32 text-center leading-relaxed">
        Message the pilot. It plans, investigates via swarms, and explains.
      </div>
    );
  }
  if (transcriptStale && itemCount === 0) {
    return (
      <div className="text-muted text-[13px] mt-32 text-center leading-relaxed">
        Loading session…
      </div>
    );
  }
  return null;
}
