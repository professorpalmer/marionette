import { useEffect, useState } from "react";
import { BookOpen, Bug, GitCompare, MessageSquare } from "lucide-react";
import { api, type Session } from "../../lib/api";
import { displaySessionListTitle, isDefaultSessionTitle } from "../../lib/sessionTitle";
import { prefillComposer, requestSessionSwitch, sessionAge, workspaceName } from "../../lib/sessionNav";

/** Starter prompts fill the composer. They never send, so the user can edit. */
export const STARTER_PROMPTS = [
  {
    id: "tour",
    label: "Explain this codebase",
    icon: BookOpen,
    prompt: "Give me a tour of this repository: the layout, the main entry points, and how the parts connect.",
  },
  {
    id: "bug",
    label: "Find and fix a bug",
    icon: Bug,
    prompt: "Find the most likely bug in this repository. Explain it, then fix it and add a test that fails without the fix.",
  },
  {
    id: "review",
    label: "Review my changes",
    icon: GitCompare,
    prompt: "Review my uncommitted changes for correctness, risky edge cases and missing tests.",
  },
] as const;

const RECENT_LIMIT = 4;

/** Rows worth reopening: not this session, not archived, not an untouched "New session". */
export function recentSessionRows(rows: Session[], currentId: string | undefined): Session[] {
  return rows
    .filter((row) => row.id !== currentId && !row.archived && !isDefaultSessionTitle(row.title))
    .sort((a, b) => (b.created || 0) - (a.created || 0))
    .slice(0, RECENT_LIMIT);
}

/** Below the centered composer in an empty session: starters, then recent sessions. */
export default function EmptySessionStarters({
  sessionId,
  showStarters,
}: {
  sessionId?: string;
  showStarters: boolean;
}) {
  const [recent, setRecent] = useState<Session[]>([]);
  useEffect(() => {
    let cancelled = false;
    api.sessionsBank({ limit: 24 })
      .then((rows) => {
        if (!cancelled) setRecent(recentSessionRows(Array.isArray(rows) ? rows : [], sessionId));
      })
      .catch(() => {
        if (!cancelled) setRecent([]);
      });
    return () => { cancelled = true; };
  }, [sessionId]);

  return (
    <div data-testid="empty-session-starters" className="max-w-3xl mx-auto px-6 pt-4 pb-6 flex flex-col gap-6">
      {showStarters ? (
        <div className="flex flex-wrap justify-center gap-2">
          {STARTER_PROMPTS.map(({ id, label, icon: Icon, prompt }) => (
            <button
              key={id}
              type="button"
              onClick={() => prefillComposer(prompt)}
              title={prompt}
              className="inline-flex items-center gap-1.5 h-8 px-3 rounded-full border border-edge bg-panel/60 text-ui-12 text-txt hover:bg-panel2 hover:border-edge2 transition"
            >
              <Icon size={13} className="text-muted" />
              {label}
            </button>
          ))}
        </div>
      ) : null}
      {recent.length > 0 ? (
        <section aria-label="Recent sessions" className="mx-auto w-full max-w-md">
          <h3 className="px-2 mb-1 text-ui-10.5 font-semibold uppercase tracking-[0.12em] text-faint">Recent sessions</h3>
          <ul className="flex flex-col">
            {recent.map((row) => (
              <li key={row.id}>
                <button
                  type="button"
                  onClick={() => requestSessionSwitch(row.id)}
                  className="w-full h-8 grid grid-cols-[14px_minmax(0,1fr)_auto] items-center gap-x-2 px-2 rounded text-left hover:bg-panel2/70 transition"
                >
                  <MessageSquare size={12} className="text-faint" />
                  <span className="truncate text-ui-12.5 text-txt">
                    {displaySessionListTitle(row.title, row.title_user)}
                    <span className="text-faint"> · {workspaceName(row.workspace_root || row.repo)}</span>
                  </span>
                  <span className="text-ui-11 text-faint tabular-nums">{sessionAge(row.created)}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
