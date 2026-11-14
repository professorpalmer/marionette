import type { NestStatus } from "../../lib/api";
import { nestStatusDot, nestStatusLabel } from "../../lib/nestedSessions";
import { requestSessionSwitch } from "../../lib/sessionNav";

/** A nested session's report in its parent transcript. */
export type SessionNoteItem = {
  kind: "session_note";
  child_id: string;
  name: string;
  status: NestStatus;
  report: string;
  driver: "parent" | "user";
  ts: number;
};

function parseStatus(raw: unknown): NestStatus {
  return raw === "working" || raw === "failed" || raw === "stopped" ? raw : "done";
}

/** Parse a ``session_note`` display row or stream event. Null when it has no child. */
export function parseSessionNote(raw: unknown): SessionNoteItem | null {
  if (!raw || typeof raw !== "object") return null;
  const row: Record<string, unknown> = { ...raw };
  const childId = typeof row.child_id === "string" ? row.child_id.trim() : "";
  if (!childId) return null;
  return {
    kind: "session_note",
    child_id: childId,
    name: typeof row.name === "string" && row.name ? row.name : childId,
    status: parseStatus(row.status),
    report: typeof row.report === "string" ? row.report : "",
    driver: row.driver === "user" ? "user" : "parent",
    ts: typeof row.ts === "number" && Number.isFinite(row.ts) ? row.ts : 0,
  };
}

/** Report text for the row: markdown emphasis and code marks removed. */
export function plainReport(report: string): string {
  return report.replace(/\*\*|__|`/g, "");
}

export function sessionNoteKey(item: SessionNoteItem): string {
  return `note-${item.child_id}-${item.ts}`;
}

export function SessionNoteRow({ item }: { item: SessionNoteItem }) {
  return (
    <div
      data-testid="session-note"
      className="my-1 w-full max-w-full rounded-lg border border-edge/30 bg-panel2/15 px-3 py-2 text-ui-11"
    >
      <button
        type="button"
        onClick={() => requestSessionSwitch(item.child_id)}
        title={`Open ${item.name}`}
        className="flex max-w-full items-center gap-1.5 rounded-md text-txt hover:text-accent"
      >
        <span aria-hidden className={`h-1.5 w-1.5 shrink-0 rounded-full ${nestStatusDot(item.status)}`} />
        <span className="min-w-0 truncate font-medium">{item.name}</span>
        <span className="shrink-0 text-faint">
          · {nestStatusLabel(item.status)}{item.driver === "user" ? " · you drove this turn" : ""}
        </span>
      </button>
      {item.report ? (
        <p className="mt-1 line-clamp-3 whitespace-pre-wrap break-words text-muted">{plainReport(item.report)}</p>
      ) : null}
    </div>
  );
}
