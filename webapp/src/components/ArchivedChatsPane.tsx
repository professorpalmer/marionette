import { useEffect, useState } from "react";
import { Archive } from "lucide-react";
import { api, type Session } from "../lib/api";
import { SESSION_UNARCHIVED_EVENT, requestSessionSwitch, sessionAge, workspaceName } from "../lib/sessionNav";
import { displaySessionListTitle } from "../lib/sessionTitle";

// The bank endpoint caps a listing at 500 rows.
const BANK_LIMIT = 500;

type Listing =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; rows: Session[] };

function errorText(err: unknown): string {
  if (err && typeof err === "object") {
    const record = err as Record<string, unknown>;
    for (const key of ["error", "message"]) {
      const value = record[key];
      if (typeof value === "string" && value.trim()) return value.trim();
    }
  }
  return String(err || "unknown error");
}

/** Settings Archived chats page. An archived chat lives in the vault; open or unarchive restores it. */
export default function ArchivedChatsPane({ onOpenSession }: { onOpenSession: () => void }) {
  const [listing, setListing] = useState<Listing>({ kind: "loading" });
  const [busyId, setBusyId] = useState<string | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.sessionsBank({ limit: BANK_LIMIT })
      .then((rows) => {
        if (cancelled) return;
        const archived = (Array.isArray(rows) ? rows : [])
          .filter((row) => row.archived)
          .sort((a, b) => (b.updated ?? b.created) - (a.updated ?? a.created));
        setListing({ kind: "ready", rows: archived });
      })
      .catch((err: unknown) => {
        if (!cancelled) setListing({ kind: "error", message: errorText(err) });
      });
    return () => { cancelled = true; };
  }, []);

  const open = (sessionId: string) => {
    requestSessionSwitch(sessionId);
    onOpenSession();
  };

  const unarchive = async (sessionId: string) => {
    setBusyId(sessionId);
    setRowError(null);
    try {
      await api.archiveSession(sessionId, false);
      setListing((prev) => prev.kind === "ready"
        ? { kind: "ready", rows: prev.rows.filter((row) => row.id !== sessionId) }
        : prev);
      window.dispatchEvent(new Event(SESSION_UNARCHIVED_EVENT));
    } catch (err) {
      setRowError(`Could not unarchive: ${errorText(err)}`);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="space-y-3" data-testid="archived-chats-pane">
      <div className="flex items-center gap-2 text-ui-12 text-muted">
        <Archive size={14} className="text-accent" />
        <span>Archived chats are kept in the vault, not in the session list. Open or unarchive one to restore it.</span>
      </div>
      {rowError && <div className="text-ui-11 text-risk">{rowError}</div>}
      {listing.kind === "loading" && <div className="text-ui-12 text-faint">Loading archived chats...</div>}
      {listing.kind === "error" && (
        <div className="text-ui-12 text-risk">Could not read sessions: {listing.message}</div>
      )}
      {listing.kind === "ready" && listing.rows.length === 0 && (
        <div className="text-ui-12 text-faint" data-testid="archived-chats-empty">No archived chats.</div>
      )}
      {listing.kind === "ready" && listing.rows.length > 0 && (
        <ul className="divide-y divide-edge/30 border border-edge/40 rounded-md">
          {listing.rows.map((row) => (
            <li key={row.id} className="flex items-center gap-3 px-3 py-2" data-testid="archived-chat-row">
              <div className="min-w-0 flex-1">
                <div className="text-ui-12.5 text-txt truncate">{displaySessionListTitle(row.title, row.title_user)}</div>
                <div className="text-ui-11 text-faint truncate">
                  {[workspaceName(row.workspace_root), sessionAge(row.updated ?? row.created)].filter(Boolean).join(" · ")}
                </div>
              </div>
              <button
                type="button"
                onClick={() => open(row.id)}
                disabled={busyId === row.id}
                className="shrink-0 px-2 py-1 rounded-md text-ui-11 border border-edge/50 text-txt hover:bg-panel2 disabled:opacity-50"
              >
                Open
              </button>
              <button
                type="button"
                onClick={() => { void unarchive(row.id); }}
                disabled={busyId === row.id}
                className="shrink-0 px-2 py-1 rounded-md text-ui-11 border border-edge/50 text-txt hover:bg-panel2 disabled:opacity-50"
              >
                {busyId === row.id ? "Restoring..." : "Unarchive"}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
