import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import { displaySessionListTitle, SESSIONS_CHANGED_EVENT } from "../../lib/sessionTitle";

export type SessionHeading = { title: string; project: string; branch: string };

/** Last path part of a workspace root ("/a/b/proj" -> "proj"). */
export function projectName(repo: string): string {
  const parts = repo.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || "";
}

/**
 * Session title, project, and branch for the conversation header. Reads
 * again when the session or workspace changes, when the server names the
 * session, and after a config event (a branch checkout).
 */
export function useSessionHeading(sessionId: string | null, repo: string): SessionHeading {
  const [title, setTitle] = useState("");
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
          setTitle(row ? displaySessionListTitle(row.title, row.title_user) : "");
        }).catch(() => {});
      } else {
        setTitle("");
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
