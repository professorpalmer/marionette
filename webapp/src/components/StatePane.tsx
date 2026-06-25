import { Database } from "lucide-react";

export default function StatePane({ artifacts }: {
  artifacts: { type: string; headline: string; confidence?: number }[];
}) {
  return (
    <aside className="bg-panel border-l border-edge flex flex-col h-full overflow-hidden">
      <header className="px-3 py-3 border-b border-edge flex items-center gap-2">
        <Database size={13} className="text-muted" />
        <span className="text-[11px] uppercase tracking-wider text-muted">Durable State</span>
      </header>
      <div className="text-[10px] text-muted px-3 pt-2">Artifacts (live)</div>
      <div className="flex-1 overflow-y-auto px-2 py-2 flex flex-col gap-1.5">
        {artifacts.length === 0 && (
          <div className="text-[11px] text-muted italic px-1">Findings appear here as the pilot investigates.</div>
        )}
        {artifacts.map((a, i) => (
          <div key={i} className="bg-panel2 border border-edge rounded-lg p-2">
            <div className="text-[9px] uppercase tracking-wide text-accent">{a.type}</div>
            <div className="text-[12px] mt-0.5">{a.headline}</div>
            {a.confidence != null && <div className="text-[10px] text-muted mt-0.5">confidence {a.confidence}</div>}
          </div>
        ))}
      </div>
    </aside>
  );
}
