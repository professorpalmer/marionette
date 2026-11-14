import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";
import type { Session } from "../lib/api";
import { nestStatus, nestStatusDot, nestStatusLabel, nestedSessionsLabel, type RunnerState } from "../lib/nestedSessions";
import { displaySessionListTitle } from "../lib/sessionTitle";

/** Open/close control on a parent row. A sibling of the row button, never inside it. */
export function NestToggle({
  count,
  expanded,
  onToggle,
}: {
  count: number;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={(e) => { e.stopPropagation(); onToggle(); }}
      aria-expanded={expanded}
      aria-label={`${expanded ? "Hide" : "Show"} ${nestedSessionsLabel(count)}`}
      title={nestedSessionsLabel(count)}
      className="p-0.5 rounded text-faint hover:text-txt hover:bg-panel2 shrink-0 focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent"
    >
      {expanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
    </button>
  );
}

/** Tree line on the left of a child row: a straight line, or a corner on the last row. */
function TreeLine({ last }: { last: boolean }) {
  return last
    ? <span aria-hidden className="absolute left-6 top-0 h-1/2 w-2 border-l border-b border-edge rounded-bl-md" />
    : (
      <>
        <span aria-hidden className="absolute left-6 top-0 bottom-0 border-l border-edge" />
        <span aria-hidden className="absolute left-6 top-1/2 w-2 border-t border-edge" />
      </>
    );
}

/**
 * Children under a parent session in the rail. Open: one row per child with a
 * tree line and a status dot. Closed: one "N nested sessions" row that opens it.
 */
export default function NestedSessionRows({
  sessions,
  expanded,
  onToggle,
  runners,
  switchingSessionId,
  onOpen,
  onPrefetch,
}: {
  sessions: readonly Session[];
  expanded: boolean;
  onToggle: () => void;
  runners: Record<string, RunnerState>;
  switchingSessionId: string | null;
  onOpen: (id: string) => void;
  onPrefetch: (id: string) => void;
}) {
  if (!expanded) {
    return (
      <div className="relative" data-testid="nested-sessions-collapsed">
        <TreeLine last />
        <button
          type="button"
          onClick={onToggle}
          className="w-full min-h-6 pl-11 pr-1.5 text-left text-ui-11 text-faint hover:text-muted rounded"
        >
          {nestedSessionsLabel(sessions.length)}
        </button>
      </div>
    );
  }
  return (
    <div role="group" aria-label="Nested sessions" data-testid="nested-sessions">
      {sessions.map((child, index) => {
        const status = nestStatus(child.nest?.status, runners[child.id]);
        const title = displaySessionListTitle(child.title, child.title_user);
        const switching = switchingSessionId === child.id;
        return (
          <div key={child.id} className="relative" data-nested-session={child.id}>
            <TreeLine last={index === sessions.length - 1} />
            <button
              type="button"
              onClick={() => { if (!switchingSessionId) onOpen(child.id); }}
              onPointerEnter={() => onPrefetch(child.id)}
              aria-current={child.active ? "true" : undefined}
              aria-disabled={!!switchingSessionId || undefined}
              title={child.nest?.report ? `${title}\n${nestStatusLabel(status)}: ${child.nest.report}` : `${title}\n${nestStatusLabel(status)}`}
              className={`w-full min-h-7 pl-11 pr-1.5 flex items-center gap-1.5 text-left text-ui-12 rounded aria-disabled:opacity-60
                ${child.active ? "text-txt font-medium" : "text-muted hover:text-txt"}`}
            >
              <span className="flex-1 min-w-0 truncate">{title}</span>
              {switching
                ? <Loader2 size={10} className="shrink-0 animate-spin text-accent" />
                : status !== "done"
                  ? <span
                      aria-label={nestStatusLabel(status)}
                      className={`w-1.5 h-1.5 rounded-full shrink-0 ${nestStatusDot(status)}`}
                    />
                  : null}
            </button>
          </div>
        );
      })}
    </div>
  );
}
