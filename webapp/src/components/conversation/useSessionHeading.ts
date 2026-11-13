import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import { displaySessionListTitle, SESSIONS_CHANGED_EVENT } from "../../lib/sessionTitle";

export type SessionHeading = { title: string; project: string; branch: string };

/** Last path part of a workspace root ("/a/b/proj" -> "proj"). */
export function projectName(repo: string): string {
  const parts = repo.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || "";
}

const LAST_HEADING_KEY = "marionette:last-heading";

/**
 * Keep the title the header last showed. After a reload the header shows it
 * until the session list answers (about 200 ms), not "Marionette".
 */
export function rememberHeadingTitle(sessionId: string, title: string): void {
  try {
    localStorage.setItem(LAST_HEADING_KEY, JSON.stringify({ sessionId, title }));
  } catch {
    // Storage can be off. The header then waits for the read.
  }
}

/** The kept title, when it belongs to this session (or no id is known yet). */
function lastHeadingTitle(sessionId: string | null): string {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(LAST_HEADING_KEY) || "null");
    if (typeof raw !== "object" || raw === null) return "";
    const { sessionId: kept, title } = raw as { sessionId?: unknown; title?: unknown };
    if (typeof kept !== "string" || typeof title !== "string") return "";
    return !sessionId || kept === sessionId ? title : "";
  } catch {
    return "";
  }
}

/**
 * Session title, project, and branch for the conversation header. Reads
 * again when the session or workspace changes, when the server names the
 * session, and after a config event (a branch checkout).
 */
export function useSessionHeading(sessionId: string | null, repo: string): SessionHeading {
  const [title, setTitle] = useState(() => lastHeadingTitle(sessionId));
  const [branch, setBranch] = useState("");

  useEffect(() => {
    let gen = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const load = () => {
      const mine = ++gen;
      if (sessionId) {
        api.sessions(repo || undefined).then((rows) => {
          if (mine !== gen) return;
          const row = rows.find((r) => r.id === sessionId);
          const next = row ? displaySessionListTitle(row.title, row.title_user) : "";
          setTitle(next);
          if (next) rememberHeadingTitle(sessionId, next);
        }).catch(() => {});
      } else {
        // No id yet: the app is still reading its sessions after a reload.
        setTitle(lastHeadingTitle(null));
      }
      api.getWorkspace().then((ws) => {
        if (mine === gen) setBranch(ws?.is_git ? ws.branch || "" : "");
      }).catch(() => {});
    };
    const later = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(load, 300);
    };
    load();
    window.addEventListener(SESSIONS_CHANGED_EVENT, later);
    window.addEventListener("harness-config-changed", later);
    return () => {
      gen += 1;
      if (timer) clearTimeout(timer);
      window.removeEventListener(SESSIONS_CHANGED_EVENT, later);
      window.removeEventListener("harness-config-changed", later);
    };
  }, [sessionId, repo]);

  return { title, project: projectName(repo), branch };
}
