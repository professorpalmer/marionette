import { useEffect, useState } from "react";
import { api, type Session } from "../lib/api";
import { SESSION_UNARCHIVED_EVENT, requestSessionSwitch, sessionAge, workspaceName } from "../lib/sessionNav";
import { displaySessionListTitle } from "../lib/sessionTitle";
import { SETTINGS_PRIMARY_BUTTON, SettingsGroup, SettingsPage, SettingsRow } from "./SettingsLayout";

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

/** Settings Archived chats page. An archived chat lives in the vault. Open or unarchive restores it. */
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
    <SettingsPage
      title="Archived chats"
      description="An archived chat stays in the vault and not in the session list. Click a chat to open it, or unarchive it to put it back in the list."
      testId="archived-chats-pane"
    >
      {rowError ? <p className="text-ui-11 text-risk" role="alert">{rowError}</p> : null}
      <SettingsGroup>
        {listing.kind === "loading" ? <SettingsRow label="Reading archived chats..." /> : null}
        {listing.kind === "error" ? (
          <SettingsRow label={<span className="text-risk">Could not read sessions: {listing.message}</span>} />
        ) : null}
        {listing.kind === "ready" && listing.rows.length === 0 ? (
          <SettingsRow label="No archived chats." testId="archived-chats-empty" />
        ) : null}
        {listing.kind === "ready" ? listing.rows.map((row) => (
          <SettingsRow
            key={row.id}
            testId="archived-chat-row"
            label={(
              <button
                type="button"
                onClick={() => open(row.id)}
                disabled={busyId === row.id}
                title="Open this chat"
                className="block max-w-full truncate text-left hover:text-accent disabled:opacity-50"
              >
                {displaySessionListTitle(row.title, row.title_user)}
              </button>
            )}
            description={(
              <span className="block truncate">
                {[workspaceName(row.workspace_root), sessionAge(row.updated ?? row.created)].filter(Boolean).join(" · ")}
              </span>
            )}
            control={(
              <button
                type="button"
                className={SETTINGS_PRIMARY_BUTTON}
                onClick={() => { void unarchive(row.id); }}
                disabled={busyId === row.id}
              >
                {busyId === row.id ? "Restoring..." : "Unarchive"}
              </button>
            )}
          />
        )) : null}
      </SettingsGroup>
    </SettingsPage>
  );
}
