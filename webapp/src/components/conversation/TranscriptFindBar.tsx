import { useEffect, useRef } from "react";
import { ChevronDown, ChevronUp, X } from "lucide-react";

/** Cmd/Ctrl+F bar over the chat feed. State lives in the chat column. */
export function TranscriptFindBar({
  query,
  onQuery,
  active,
  total,
  onStep,
  onClose,
  focusNonce,
  rightInset,
}: {
  query: string;
  onQuery: (q: string) => void;
  /** Zero-based active match, -1 when none. */
  active: number;
  total: number;
  onStep: (delta: 1 | -1) => void;
  onClose: () => void;
  /** Bumped on every Cmd/Ctrl+F so a repeat press reselects the query. */
  focusNonce: number;
  /** Feed's right clearance (dock rail), so the bar never covers its icons. */
  rightInset: number;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [focusNonce]);
  const status = !query.trim() ? "" : total === 0 ? "No results" : `${active + 1} of ${total}`;
  const stepButton = "p-1 rounded text-muted hover:text-txt hover:bg-panel2 disabled:opacity-40 disabled:pointer-events-none";
  return (
    <div
      role="search"
      data-testid="transcript-find"
      style={{ right: rightInset + 16, maxWidth: `calc(100% - ${rightInset + 32}px)` }}
      className="transcript-fold-chrome select-none absolute top-2 z-20 flex items-center gap-1 rounded-lg border border-edge2 bg-panel px-2 py-1 shadow-lg"
    >
      <input
        ref={inputRef}
        value={query}
        onChange={(e) => onQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            onStep(e.shiftKey ? -1 : 1);
          } else if (e.key === "Escape") {
            e.preventDefault();
            onClose();
          }
        }}
        placeholder="Find in chat"
        aria-label="Find in chat"
        spellCheck={false}
        className="w-48 min-w-0 flex-1 bg-transparent text-ui-12 text-txt placeholder:text-faint outline-none"
      />
      <span data-testid="transcript-find-status" aria-live="polite" className="shrink-0 min-w-[4.5rem] text-right text-ui-11 tabular-nums text-faint">
        {status}
      </span>
      <button type="button" aria-label="Previous match" title="Previous (Shift+Enter)" disabled={total === 0} onClick={() => onStep(-1)} className={stepButton}>
        <ChevronUp size={14} />
      </button>
      <button type="button" aria-label="Next match" title="Next (Enter)" disabled={total === 0} onClick={() => onStep(1)} className={stepButton}>
        <ChevronDown size={14} />
      </button>
      <button type="button" aria-label="Close find" title="Close (Esc)" onClick={onClose} className="p-1 rounded text-muted hover:text-txt hover:bg-panel2">
        <X size={14} />
      </button>
    </div>
  );
}
