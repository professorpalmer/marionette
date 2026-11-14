import { useEffect, useState } from "react";
import { api, type WorkspaceInfo } from "../../lib/api";
import { repoPathsEqual } from "../../lib/pathNormalize";
import { displaySessionListTitle, SESSIONS_CHANGED_EVENT } from "../../lib/sessionTitle";
import { readSWRCache, writeSWRCache } from "../../lib/useStaleWhileRevalidate";
import { cachedSessionRow, workspaceInfoCacheKey } from "../leftRailSessions";

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

/** ``root`` is the project the rail lists the session under. It differs from
 *  ``repo`` only for a nested session, whose repo is its own worktree. */
type HeadingSeed = { title: string; repo: string; root: string; branch: string } | null;

/** What the rail already knows about the session, read at switch time. */
function headingSeed(sessionId: string | null): HeadingSeed {
  const hit = sessionId ? cachedSessionRow(sessionId) : null;
  if (!hit) return null;
  const { row, root } = hit;
  const repo = row.repo || row.workspace_root || root;
  if (row.nest?.parent_id) {
    // A nested session works in its own worktree, which is locked to the
    // child's branch. The view stays on the parent project, so no workspace
    // read names that branch: the row does.
    return {
      title: displaySessionListTitle(row.title, row.title_user),
      repo,
      root: row.workspace_root || root || repo,
      branch: row.branch || "",
    };
  }
  // The row's own branch is the one the session started on; the repo may
  // be on another branch now. Show only a branch read from that repo.
  const workspace = readSWRCache<WorkspaceInfo>(workspaceInfoCacheKey(repo));
  return {
    title: displaySessionListTitle(row.title, row.title_user),
    repo,
    root: repo,
    branch: workspace?.is_git ? workspace.branch || "" : "",
  };
}

/**
 * Session title, project, and branch for the conversation header. A switch
 * shows the rail's cached row for the target at once: the config and
 * workspace reads answer for the outgoing session until the backend switch
 * ends. Reads again when the session or workspace changes, when the server
 * names the session, and after a config event (a branch checkout).
 */
export function useSessionHeading(sessionId: string | null, repo: string): SessionHeading {
  const [seedFor, setSeedFor] = useState(sessionId);
  const [seed, setSeed] = useState<HeadingSeed>(() => headingSeed(sessionId));
  if (seedFor !== sessionId) {
    setSeedFor(sessionId);
    setSeed(headingSeed(sessionId));
  }
  const [loaded, setLoaded] = useState<{ sessionId: string; title: string } | null>(null);
  const [workspace, setWorkspace] = useState<{ repo: string; branch: string } | null>(null);
  const shownRepo = seed?.repo || repo;
  const projectRoot = seed?.root || shownRepo;

  useEffect(() => {
    let gen = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const load = () => {
      const mine = ++gen;
      if (sessionId) {
        api.sessions(projectRoot || undefined).then((rows) => {
          if (mine !== gen) return;
          const row = rows.find((r) => r.id === sessionId);
          const next = row ? displaySessionListTitle(row.title, row.title_user) : "";
          setLoaded({ sessionId, title: next });
          if (next) rememberHeadingTitle(sessionId, next);
        }).catch(() => {});
      }
      api.getWorkspace().then((ws) => {
        if (ws?.repo) writeSWRCache(workspaceInfoCacheKey(ws.repo), ws);
        if (mine === gen) setWorkspace({ repo: ws?.repo || "", branch: ws?.is_git ? ws.branch || "" : "" });
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
  }, [sessionId, shownRepo, projectRoot]);

  const title = !sessionId
    ? lastHeadingTitle(null)
    : loaded?.sessionId === sessionId
      ? loaded.title
      : seed?.title || lastHeadingTitle(sessionId);
  // The workspace read names the backend's open folder; mid-switch that is
  // still the outgoing session's, so its branch counts only for this repo.
  const workspaceBranch = workspace && (!shownRepo || repoPathsEqual(workspace.repo, shownRepo))
    ? workspace.branch
    : null;
  const branch = (seed && seed.root !== seed.repo ? seed.branch : null) ?? workspaceBranch ?? seed?.branch ?? "";
  return { title, project: projectName(projectRoot), branch };
}
