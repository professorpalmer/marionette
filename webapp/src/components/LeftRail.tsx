import { useCallback, useEffect, useRef, useState } from "react";
import { GitBranch, Plus, MessageSquare, Check, Loader2, ChevronDown, ChevronRight, SquarePen, Folder, FolderGit2, CheckCircle2, Circle, XCircle, Trash2, Brush, Search, X } from "lucide-react";
import { api, type Workspace, type WorkspaceInfo, type Session, type Job, type Artifact } from "../lib/api";
import { pickFolder } from "../lib/transport";
import { dispatchProjectSelected, dispatchProjectSwitching, panelOpacityClass } from "../lib/panelTransition";
import { repoPathsEqual } from "../lib/pathNormalize";
import { mapSessionSearchHits, type SessionSearchRow } from "../lib/sessionSearch";
import { usePolling } from "../lib/usePolling";
import { readSWRCache, writeSWRCache, useStaleWhileRevalidate } from "../lib/useStaleWhileRevalidate";
import { writeTranscriptCache } from "./Conversation";

/** User-facing copy when concurrent session runner leases are full. */
export const SESSION_LEASE_EXHAUSTED_MESSAGE =
  "This session could not start — too many sessions are busy right now. Wait a moment or stop another turn, then try again.";

export type LeaseExhaustedPayload = {
  code?: string;
  error?: string;
  message?: string;
  status?: number;
  max_concurrent?: number;
  active_count?: number;
  busy_session_ids?: string[];
  busy_session_titles?: string[];
};

/** True when switch/open/create failed because all session runner leases are busy. */
export function isLeaseExhaustedError(err: unknown): boolean {
  if (!err) return false;
  const e = err as LeaseExhaustedPayload;
  if (e.code === "lease_exhausted") return true;
  const msg = String(e.message || e.error || err || "");
  // Message-only fallbacks (older servers / bridge quirks). Do NOT treat a bare
  // "... -> 409" as lease exhaustion — other conflicts share that status.
  if (/lease_exhausted/i.test(msg)) return true;
  if (/session runner lease exhausted/i.test(msg)) return true;
  return false;
}

/** Hermes-style toast: name busy sessions and show capacity when the 409 body has them. */
export function formatLeaseExhaustedMessage(err: unknown): string {
  const e = (err || {}) as LeaseExhaustedPayload;
  const max = typeof e.max_concurrent === "number" ? e.max_concurrent : null;
  const active = typeof e.active_count === "number" ? e.active_count : null;
  const titles = (e.busy_session_titles || []).map((t) => String(t).trim()).filter(Boolean);
  const capacity =
    max != null
      ? active != null
        ? `${active}/${max}`
        : `${max}`
      : null;
  if (titles.length && capacity) {
    return `Too many sessions are busy (${capacity}). Stop one of: ${titles.map((t) => `"${t}"`).join(", ")} — then try again.`;
  }
  if (titles.length) {
    return `Too many sessions are busy. Stop one of: ${titles.map((t) => `"${t}"`).join(", ")} — then try again.`;
  }
  if (capacity) {
    return `This session could not start — session capacity is full (${capacity}). Wait a moment or stop another turn, then try again.`;
  }
  return SESSION_LEASE_EXHAUSTED_MESSAGE;
}

/**
 * Stable PROJECTS rail order: keep recents as-is, append currentRepo only when
 * it is not already present (slash/case-insensitive). Never force the active
 * path to index 0.
 */
export function buildProjectsList(currentRepo: string, rawRecents: string[]): string[] {
  const seen: string[] = [];
  const out: string[] = [];
  for (const p of rawRecents) {
    if (!p || seen.some((s) => repoPathsEqual(s, p))) continue;
    seen.push(p);
    out.push(p);
  }
  if (currentRepo && !seen.some((s) => repoPathsEqual(s, currentRepo))) {
    out.push(currentRepo);
  }
  return out;
}

/** Drop a path (and slash/case siblings) from a recents list. */
export function filterForgottenRecent(recents: string[], path: string): string[] {
  return (recents || []).filter((r) => !repoPathsEqual(r, path));
}

/** Drop a session id from every per-root sessions SWR cache. Returns how many
 *  caches were rewritten. Used on delete so inactive projects do not keep
 *  phantom titles (the "merged dir" ghost). */
export function purgeSessionFromRootCaches(
  roots: string[],
  sessionId: string,
  read: (key: string) => Session[] | undefined = readSWRCache,
  write: (key: string, data: Session[]) => void = writeSWRCache,
): number {
  let touched = 0;
  for (const root of roots) {
    if (!root) continue;
    const key = `sessions:${root}`;
    const cached = read(key);
    if (!cached) continue;
    const next = cached.filter((s) => s.id !== sessionId);
    if (next.length === cached.length) continue;
    write(key, next);
    touched += 1;
  }
  return touched;
}

/** SWR cache key for the Branches list -- keyed by repo so project switches
 *  do not flash another project's branches, and revisits stay warm. */
export function workspacesCacheKey(repo: string): string {
  return `workspaces:${repo || "__none__"}`;
}

/** True when LeftRail should offer Stop without forcing a view attach. */
export function shouldOfferBackgroundStop(
  status: "running" | "idle" | "attaching" | "missing" | undefined,
  isActive: boolean,
): boolean {
  return status === "running" && !isActive;
}

/**
 * Rail-wide dim / project-switching signal. Browse-selecting an already-listed
 * project must not trip this — jobs SWR key changes on select and used to flash
 * the whole PROJECTS tree. Only real open/switch/session activation dims the rail.
 */
export function isRailWideSwitching(flags: {
  opening: boolean;
  switchingSessionId: string | null;
  workspaceTransitioning: boolean;
  sessionsTransitioning: boolean;
}): boolean {
  return (
    flags.opening
    || !!flags.switchingSessionId
    || flags.workspaceTransitioning
    || flags.sessionsTransitioning
  );
}

/**
 * Per-project empty-state: spinner only until that root's sessions resolve.
 * Never gate on rail-wide / jobs transitioning (that hid the New session CTA
 * and blinked "Loading sessions..." on first expand of a listed project).
 */
export function projectSessionsEmptyState(
  sessionsReady: boolean,
  showRowLoading: boolean,
): "loading" | "pending" | "empty" {
  if (sessionsReady) return "empty";
  return showRowLoading ? "loading" : "pending";
}

export default function LeftRail({ jobsRefresh, onSessionChange }: {
  jobsRefresh: number;
  onSessionChange?: (id: string) => void;
}) {
  const [swapping, setSwapping] = useState<string | null>(null);
  const [contextMenu, setContextMenu] = useState<{
    x: number;
    y: number;
    sessionId: string;
    archived: boolean;
    running: boolean;
  } | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [projectContextMenu, setProjectContextMenu] = useState<{
    x: number;
    y: number;
    projectPath: string;
  } | null>(null);
  const [confirmForgetPath, setConfirmForgetPath] = useState<string | null>(null);
  const [archivedExpanded, setArchivedExpanded] = useState(false);
  const [sessionJobsCollapsed, setSessionJobsCollapsed] = useState(
    () => localStorage.getItem(SESSION_JOBS_COLLAPSED_KEY) === "1",
  );
  const [hiddenJobIds, setHiddenJobIds] = useState<Set<string>>(loadHiddenSessionJobs);
  const [confirmClearJobs, setConfirmClearJobs] = useState(false);
  const [showAllJobs, setShowAllJobs] = useState(false);
  const [expandedJobs, setExpandedJobs] = useState<Record<string, boolean>>({});
  const [sessionJobsHeight, setSessionJobsHeight] = useState(loadSessionJobsHeight);
  const [branchesHeight, setBranchesHeight] = useState(loadBranchesHeight);
  const [pruningBranches, setPruningBranches] = useState(false);
  // /api/jobs only carries an artifact COUNT per job; the full artifact list is
  // fetched lazily the first time a card is expanded and cached here.
  const [artifactsByJob, setArtifactsByJob] = useState<Record<string, Artifact[]>>({});

  const railRef = useRef<HTMLElement>(null);
  const topChromeRef = useRef<HTMLDivElement>(null);
  const upperSectionsRef = useRef<HTMLDivElement>(null);
  const projectsSectionRef = useRef<HTMLDivElement>(null);
  const sessionJobsHeightRef = useRef(sessionJobsHeight);
  const branchesHeightRef = useRef(branchesHeight);
  const resizeDragRef = useRef<{ startY: number; startH: number } | null>(null);
  const branchesResizeDragRef = useRef<{ startY: number; startH: number } | null>(null);

  sessionJobsHeightRef.current = sessionJobsHeight;
  branchesHeightRef.current = branchesHeight;

  const getMaxSessionJobsHeight = () => {
    const rail = railRef.current;
    const top = topChromeRef.current;
    const upper = upperSectionsRef.current;
    if (!rail || !top || !upper) return sessionJobsMinHeight();
    // Measure the upper content's NATURAL height by summing its children --
    // not upper.scrollHeight. The upper div is a flex-1 scroll container, and
    // a scroll container's scrollHeight is never less than its rendered
    // height, so with a short projects list the computed max collapsed to
    // "whatever the jobs panel already has": dragging up crawled at the ~1px
    // of layout rounding slack per event while dragging down ran free.
    // Children inside an overflow container keep their natural height, so
    // their sum is the true content bound in both the short and overflowing
    // cases.
    const upperContent = Array.from(upper.children).reduce(
      (sum, el) => sum + (el as HTMLElement).offsetHeight,
      0,
    );
    const available = rail.clientHeight - top.offsetHeight;
    return Math.max(sessionJobsMinHeight(), available - upperContent);
  };

  const clampSessionJobsHeight = (height: number) =>
    Math.min(getMaxSessionJobsHeight(), Math.max(sessionJobsMinHeight(), height));

  const getMaxBranchesHeight = () => {
    const rail = railRef.current;
    const top = topChromeRef.current;
    if (!rail || !top) return BRANCHES_DEFAULT_HEIGHT;
    const jobsOccupied = sessionJobsCollapsed
      ? 48
      : sessionJobsHeightRef.current;
    // Keep Projects (and a little archived chrome) from being crushed out
    // of the upper rail when Branches is dragged tall.
    const projectsOccupied = projectsSectionRef.current?.offsetHeight
      ?? BRANCHES_PROJECTS_RESERVE;
    const reserved = Math.max(BRANCHES_PROJECTS_RESERVE, projectsOccupied);
    const available =
      rail.clientHeight - top.offsetHeight - jobsOccupied - reserved;
    return Math.max(BRANCHES_MIN_HEIGHT, available);
  };

  const clampBranchesHeight = (height: number) =>
    Math.min(getMaxBranchesHeight(), Math.max(BRANCHES_MIN_HEIGHT, height));

  const onSessionJobsResizePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (sessionJobsCollapsed) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    resizeDragRef.current = { startY: e.clientY, startH: sessionJobsHeightRef.current };
  };

  const onSessionJobsResizePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!resizeDragRef.current) return;
    const delta = resizeDragRef.current.startY - e.clientY;
    setSessionJobsHeight(clampSessionJobsHeight(resizeDragRef.current.startH + delta));
  };

  const finishSessionJobsResize = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!resizeDragRef.current) return;
    resizeDragRef.current = null;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    saveSessionJobsHeight(sessionJobsHeightRef.current);
  };

  const onBranchesResizePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    branchesResizeDragRef.current = { startY: e.clientY, startH: branchesHeightRef.current };
  };

  const onBranchesResizePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!branchesResizeDragRef.current) return;
    const delta = e.clientY - branchesResizeDragRef.current.startY;
    setBranchesHeight(clampBranchesHeight(branchesResizeDragRef.current.startH + delta));
  };

  const finishBranchesResize = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!branchesResizeDragRef.current) return;
    branchesResizeDragRef.current = null;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    saveBranchesHeight(branchesHeightRef.current);
  };

  const toggleJobCard = (j: Job) => {
    const opening = !expandedJobs[j.id];
    setExpandedJobs((p) => ({ ...p, [j.id]: opening }));
    if (opening && artifactsByJob[j.id] === undefined) {
      api.artifacts(j.id)
        .then((arts) => setArtifactsByJob((p) => ({ ...p, [j.id]: Array.isArray(arts) ? arts : [] })))
        .catch(() => setArtifactsByJob((p) => ({ ...p, [j.id]: [] })));
    }
  };

  const [expandedProjects, setExpandedProjects] = useState<Record<string, boolean>>({});
  const [selectedProjectPath, setSelectedProjectPath] = useState("");
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renamingTitle, setRenamingTitle] = useState("");
  // Per-session runner liveness from /api/session/state (multi-session Phase D).
  const [runners, setRunners] = useState<Record<string, "running" | "idle" | "attaching" | "missing">>({});

  const getWorkspaceBasename = (repoPath: string) => {
    if (!repoPath) return "";
    const homePath = workspaceInfo?.home || "";
    if (homePath && repoPathsEqual(repoPath, homePath)) return "Home";
    const parts = repoPath.split(/[/\\]/);
    const base = parts[parts.length - 1] || repoPath;
    if (base.toLowerCase() === "home" && homePath && repoPathsEqual(repoPath, homePath)) {
      return "Home";
    }
    // Durable Home workspace even before /api/workspace returns home=
    if (base.toLowerCase() === "home" && /[/\\]\.pmharness[/\\]home$/i.test(repoPath.replace(/\\/g, "/"))) {
      return "Home";
    }
    return base;
  };

  const codegraphBadgeLabel = (cgStatus: string) => {
    if (cgStatus === "needs_scope") return "scope";
    if (cgStatus === "pending") return "indexing";
    // Never paint the word UNSUPPORTED for transient/empty-index flash;
    // confirmed failures surface as "failed".
    if (cgStatus === "unsupported") return "failed";
    if (cgStatus === "none") return "";
    return cgStatus;
  };

  const handleRenameSubmit = async (id: string) => {
    if (!renamingTitle.trim()) {
      setRenamingId(null);
      return;
    }
    try {
      await api.renameSession(id, renamingTitle.trim());
      await refreshSessionsRef.current();
    } catch (err) {
      console.error(err);
    } finally {
      setRenamingId(null);
    }
  };

  const [opening, setOpening] = useState(false);
  const [switchingSessionId, setSwitchingSessionId] = useState<string | null>(null);
  const [sessionActivationNotice, setSessionActivationNotice] = useState<string | null>(null);
  const codegraphByRepoRef = useRef<Record<string, string>>({});
  const [railTab, setRailTab] = useState<"projects" | "sessions">(() => {
    try {
      return localStorage.getItem("pmharness.leftRail.tab") === "sessions" ? "sessions" : "projects";
    } catch {
      return "projects";
    }
  });
  const [bankSessions, setBankSessions] = useState<Session[]>([]);
  const [bankLoading, setBankLoading] = useState(false);
  const [sessionSearchQuery, setSessionSearchQuery] = useState("");
  const [sessionSearchRows, setSessionSearchRows] = useState<SessionSearchRow[]>([]);
  const [sessionSearchLoading, setSessionSearchLoading] = useState(false);
  const sessionSearchReqId = useRef(0);

  const currentRepoRef = useRef("");
  // Assigned after projects + SWR hooks exist; early handlers (rename) call through this.
  const refreshSessionsRef = useRef<() => Promise<void>>(async () => {});
  // Kept current each render so delete can optimistically purge every root's cache.
  const projectsRef = useRef<string[]>([]);
  // Roots whose per-repo sessions fetch has resolved at least once this boot
  // (or was seeded from cache). Used so we never flash "No sessions" for a
  // row whose list has not arrived yet.
  const [sessionsResolvedRoots, setSessionsResolvedRoots] = useState<Record<string, true>>({});
  // Bumped whenever per-root session caches are rewritten outside the active
  // SWR hook so projectSessionsFor re-reads (writeSWRCache alone does not
  // re-render). Without this, deleting a session under an inactive project
  // left phantom titles until a full reload.
  const [sessionsCacheEpoch, setSessionsCacheEpoch] = useState(0);

  const onSessionsLoaded = useCallback((sess: Session[], forRepo?: string) => {
    // Stale-response guard: a late payload for a different root must not
    // promote that root's active id into the conversation pane.
    if (forRepo && currentRepoRef.current && !repoPathsEqual(forRepo, currentRepoRef.current)) {
      return;
    }
    const active = sess.find((s) => s.active);
    // Only push a real id. Passing "" during project open briefly clears the
    // conversation to the empty placeholder before the next root's active
    // session arrives -- keep the prior id until we know the next one.
    if (active?.id) {
      onSessionChange?.(active.id);
    }
  }, [onSessionChange]);

  const {
    data: workspaceInfo,
    isValidating: workspaceValidating,
    isTransitioning: workspaceTransitioning,
    isShowingStale: workspaceStale,
    revalidate: revalidateWorkspace,
    mutate: mutateWorkspace,
  } = useStaleWhileRevalidate<WorkspaceInfo>(
    "workspace",
    () => api.getWorkspace(),
    {
      onSuccess: (info) => {
        if (info.repo && info.codegraph_status) {
          codegraphByRepoRef.current[info.repo] = info.codegraph_status;
        }
      },
    },
  );

  const currentRepo = workspaceInfo?.repo || "";
  currentRepoRef.current = currentRepo;

  // Branches list: SWR keyed by repo so the first fetch stays warm across
  // session switches / config-changed events (no blank-then-refill lag).
  const {
    data: workspaces = [],
    revalidate: revalidateWorkspaces,
  } = useStaleWhileRevalidate<Workspace[]>(
    workspacesCacheKey(currentRepo),
    () => api.workspaces(),
    { enabled: !!currentRepo && !!workspaceInfo?.is_git },
  );

  const {
    data: sessions = [],
    isValidating: sessionsValidating,
    isTransitioning: sessionsTransitioning,
    isShowingStale: sessionsStale,
    revalidate: revalidateSessions,
  } = useStaleWhileRevalidate<Session[]>(
    `sessions:${currentRepo || "__none__"}`,
    () => api.sessions(currentRepo || undefined),
    {
      enabled: !!currentRepo,
      onSuccess: (sess) => {
        if (currentRepo) {
          setSessionsResolvedRoots((prev) => ({ ...prev, [currentRepo]: true }));
        }
        onSessionsLoaded(sess, currentRepo);
      },
    },
  );

  const {
    data: jobs = [],
    isValidating: jobsValidating,
    isTransitioning: jobsTransitioning,
    isShowingStale: jobsStale,
    revalidate: revalidateJobs,
  } = useStaleWhileRevalidate<Job[]>(
    `jobs:${selectedProjectPath || "__none__"}`,
    () => api.jobs(selectedProjectPath || undefined),
    { enabled: !!selectedProjectPath },
  );

  // Dim only on real workspace/session activation — never on jobs fetch that
  // follows browse-select of an already-listed project (that was the PROJECTS blink).
  const panelSwitching = isRailWideSwitching({
    opening,
    switchingSessionId,
    workspaceTransitioning,
    sessionsTransitioning,
  });

  useEffect(() => {
    dispatchProjectSwitching(panelSwitching);
  }, [panelSwitching]);

  const handleForgetProject = async (path: string) => {
    const previous = workspaceInfo;
    const forgettingActive = !!(previous?.repo && repoPathsEqual(previous.repo, path));
    mutateWorkspace(previous
      ? {
          ...previous,
          recents: filterForgottenRecent(previous.recents || [], path),
          // Drop active repo immediately so buildProjectsList cannot re-append
          // the forgotten path as a phantom row.
          ...(forgettingActive ? { repo: "", branch: "", is_git: false, codegraph_status: "none" } : {}),
        }
      : undefined);
    setExpandedProjects((prev) => {
      const next = { ...prev };
      for (const key of Object.keys(next)) {
        if (repoPathsEqual(key, path)) delete next[key];
      }
      return next;
    });
    // Drop per-root session cache so orphan titles cannot linger under the
    // forgotten path (or a slash/case sibling key).
    try {
      writeSWRCache(`sessions:${path}`, []);
      setSessionsCacheEpoch((n) => n + 1);
    } catch { /* best-effort */ }
    if (forgettingActive) {
      setSelectedProjectPath("");
    }
    try {
      const res = await api.forgetWorkspace(path);
      mutateWorkspace(previous
        ? {
            ...previous,
            recents: res.recents,
            repo: res.cleared_active ? (res.repo || "") : previous.repo,
            ...(res.cleared_active
              ? { branch: "", is_git: false, codegraph_status: "none" }
              : {}),
          }
        : undefined);
      if (res.cleared_active) {
        window.dispatchEvent(new Event("harness-config-changed"));
      }
    } catch (err) {
      console.error(err);
      if (previous) mutateWorkspace(previous);
      else await revalidateWorkspace();
    }
  };

  useEffect(() => {
    const handleConfigChanged = () => {
      // Background revalidate only -- SWR keeps the last branch list visible
      // so Branches does not blank for a second on every session switch.
      void revalidateWorkspaces();
      void refreshSessionsRef.current();
      void revalidateWorkspace();
      void revalidateJobs();
    };
    window.addEventListener("harness-config-changed", handleConfigChanged);
    return () => {
      window.removeEventListener("harness-config-changed", handleConfigChanged);
    };
  }, [revalidateWorkspace, revalidateJobs, revalidateWorkspaces]);

  // Poll workspace status while CodeGraph indexes (or waits on scope) so the
  // badge flips without opening a session or switching directories.
  useEffect(() => {
    const st = workspaceInfo?.codegraph_status;
    if (st !== "indexing" && st !== "needs_scope") return;
    const poll = () => { void revalidateWorkspace(); };
    poll();
    const timer = setInterval(poll, 4000);
    return () => clearInterval(timer);
  }, [workspaceInfo?.codegraph_status, revalidateWorkspace]);

  useEffect(() => {
    const st = workspaceInfo?.codegraph_status;
    if (st !== "indexing" && st !== "needs_scope") return;
    const onFocus = () => { void revalidateWorkspace(); };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [workspaceInfo?.codegraph_status, revalidateWorkspace]);

  const handleOpenProject = async (path: string): Promise<boolean> => {
    setOpening(true);
    try {
      const res = await api.openWorkspace(path);
      if (res.ok) {
        if (res.codegraph) codegraphByRepoRef.current[res.repo] = res.codegraph;
        mutateWorkspace({
          repo: res.repo,
          branch: res.branch,
          is_git: res.is_git,
          codegraph_status: res.codegraph,
          recents: workspaceInfo?.recents,
        });
        // Hermes-style: land inside the opened project — expand + select so
        // sessions are visible without an extra click.
        setExpandedProjects((prev) => ({ ...prev, [res.repo]: true }));
        setSelectedProjectPath(res.repo);
        dispatchProjectSelected(res.repo);
        await Promise.all([revalidateWorkspace(), revalidateWorkspaces(), revalidateSessions()]);
        window.dispatchEvent(new Event("harness-config-changed"));
        return true;
      }
      if ((res as { code?: string }).code === "lease_exhausted") {
        notifySessionActivationBlocked(res);
      } else {
        alert("Failed to open directory: " + (res as any).error);
      }
      return false;
    } catch (err: any) {
      if (!notifySessionActivationBlocked(err)) {
        alert("Error opening directory: " + (err?.error || err?.message || err));
      }
      return false;
    } finally {
      setOpening(false);
    }
  };

  const handleOpenFolder = async () => {
    const picked = await pickFolder();
    if (!picked) return;
    await handleOpenProject(picked);
  };

  useEffect(() => {
    if (!contextMenu) return;
    const handleClose = () => {
      setContextMenu(null);
      setConfirmDeleteId(null);
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setContextMenu(null);
        setConfirmDeleteId(null);
      }
    };
    window.addEventListener("click", handleClose);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("click", handleClose);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [contextMenu]);

  useEffect(() => {
    if (!projectContextMenu) return;
    const handleClose = () => {
      setProjectContextMenu(null);
      setConfirmForgetPath(null);
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setProjectContextMenu(null);
        setConfirmForgetPath(null);
      }
    };
    window.addEventListener("click", handleClose);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("click", handleClose);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [projectContextMenu]);

  const toast = (msg: string) => {
    window.dispatchEvent(new CustomEvent("harness-toast", { detail: msg }));
  };

  const notifySessionActivationBlocked = (err: unknown): boolean => {
    if (!isLeaseExhaustedError(err)) return false;
    const msg = formatLeaseExhaustedMessage(err);
    setSessionActivationNotice(msg);
    toast(msg);
    return true;
  };

  const switchWs = async (name: string) => {
    if (workspaces.some((w) => w.name === name && w.active)) return;
    setSwapping(name);
    try {
      let res = await api.switchWorkspace(name);
      if (!res.ok && res.dirty) {
        const proceed = window.confirm(
          "Uncommitted changes in this repo. Switch branch anyway? (may fail if checkout would overwrite files)",
        );
        if (!proceed) return;
        res = await api.switchWorkspace(name, { allow_dirty: true });
      }
      if (!res.ok) {
        toast(res.error || `Could not switch to ${name}`);
        return;
      }
      await Promise.all([revalidateWorkspaces(), revalidateWorkspace()]);
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch (err: any) {
      toast(err?.error || err?.message || `Could not switch to ${name}`);
    } finally {
      setSwapping(null);
    }
  };
  const newWs = async () => {
    const name = prompt("New workspace name (creates a git branch):");
    if (!name) return;
    try {
      const res = await api.createWorkspace(name);
      if (!res.ok) {
        toast(res.error || `Could not create branch ${name}`);
        return;
      }
      await Promise.all([revalidateWorkspaces(), revalidateWorkspace()]);
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch (err: any) {
      toast(err?.error || err?.message || `Could not create branch ${name}`);
    }
  };

  const pruneEditBranches = async () => {
    if (pruningBranches) return;
    const proceed = window.confirm(
      "Delete unused local edit/worker branches (pmedit-*, pmworker-*)? Active checkout and worktree-attached branches are kept.",
    );
    if (!proceed) return;
    setPruningBranches(true);
    try {
      const res = await api.pruneEditBranches();
      await revalidateWorkspaces();
      const count = typeof res.count === "number" ? res.count : (res.deleted?.length ?? 0);
      toast(count > 0
        ? `Pruned ${count} unused edit branch${count === 1 ? "" : "es"}`
        : "No unused edit branches to prune");
    } catch (err: any) {
      toast(err?.error || err?.message || "Could not prune edit branches");
    } finally {
      setPruningBranches(false);
    }
  };
  const switchSession = async (id: string) => {
    if (switchingSessionId || opening) return;
    setSwitchingSessionId(id);
    try {
      const res: any = await api.switchSession(id);
      await refreshSessionsRef.current();
      // Session switch can repoint the active repo (and thus the codegraph) on the
      // backend. Fire the same event the dir-open path uses so the codegraph/state
      // panel refetches -- without this, clicking a session leaves the old graph
      // shown even though the backend already swapped repos.
      window.dispatchEvent(new Event("harness-config-changed"));
      const repo = (res?.repo || "").trim();
      if (repo) {
        setExpandedProjects((prev) => ({ ...prev, [repo]: true }));
        setSelectedProjectPath(repo);
      }
      if (railTab === "sessions") {
        void refreshBankSessions();
      }
    } catch (err) {
      notifySessionActivationBlocked(err);
    } finally {
      setSwitchingSessionId(null);
    }
  };

  const refreshBankSessions = useCallback(async () => {
    setBankLoading(true);
    try {
      const rows = await api.sessionsBank({ limit: 80 });
      setBankSessions(Array.isArray(rows) ? rows.filter((s) => !s.archived) : []);
    } catch {
      setBankSessions([]);
    } finally {
      setBankLoading(false);
    }
  }, []);

  useEffect(() => {
    if (railTab !== "sessions") return;
    void refreshBankSessions();
  }, [railTab, refreshBankSessions, jobsRefresh]);

  // Debounced FTS search on the Sessions tab. Empty query restores the bank list.
  // Soft-fail: errors clear results without a sticky error banner.
  useEffect(() => {
    if (railTab !== "sessions") return;
    const trimmed = sessionSearchQuery.trim();
    if (!trimmed) {
      sessionSearchReqId.current += 1;
      setSessionSearchRows([]);
      setSessionSearchLoading(false);
      return;
    }
    setSessionSearchLoading(true);
    const reqId = ++sessionSearchReqId.current;
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const hits = await api.searchSessions(trimmed, 20);
          if (reqId !== sessionSearchReqId.current) return;
          const titleById: Record<string, string> = {};
          for (const s of bankSessions) {
            if (s?.id) titleById[s.id] = s.title || "";
          }
          for (const s of sessions) {
            if (s?.id && titleById[s.id] == null) titleById[s.id] = s.title || "";
          }
          setSessionSearchRows(mapSessionSearchHits(hits, titleById));
        } catch {
          if (reqId !== sessionSearchReqId.current) return;
          setSessionSearchRows([]);
        } finally {
          if (reqId === sessionSearchReqId.current) setSessionSearchLoading(false);
        }
      })();
    }, 250);
    return () => window.clearTimeout(timer);
    // Titles resolve from latest bank/local sessions at fire time; omit them as
    // deps so SWR/bank refresh does not re-hit FTS for the same query.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [railTab, sessionSearchQuery]);

  useEffect(() => {
    const onRelocated = (e: Event) => {
      const root = String((e as CustomEvent).detail?.workspace_root || "").trim();
      if (!root) return;
      setRailTab("projects");
      try { localStorage.setItem("pmharness.leftRail.tab", "projects"); } catch { /* ignore */ }
      setExpandedProjects((prev) => ({ ...prev, [root]: true }));
      setSelectedProjectPath(root);
      // Await workspace refresh first so buildProjectsList includes the new
      // root, then seed that root's sessions cache even if projectsRef lagged.
      void (async () => {
        try {
          await revalidateWorkspace();
        } catch { /* ignore */ }
        try {
          const rows = await api.sessions(root);
          writeSWRCache(`sessions:${root}`, Array.isArray(rows) ? rows : []);
          setSessionsResolvedRoots((prev) => ({ ...prev, [root]: true }));
          setSessionsCacheEpoch((n) => n + 1);
        } catch {
          setSessionsResolvedRoots((prev) => ({ ...prev, [root]: true }));
          setSessionsCacheEpoch((n) => n + 1);
        }
        try {
          await refreshSessionsRef.current();
        } catch { /* ignore */ }
        try {
          await revalidateWorkspaces();
        } catch { /* ignore */ }
      })();
    };
    window.addEventListener("harness-session-relocated", onRelocated);
    return () => window.removeEventListener("harness-session-relocated", onRelocated);
  }, [revalidateWorkspace, revalidateWorkspaces]);

  const newSession = async (inProjectPath?: string) => {
    try {
      // createSession always uses the active _cfg.repo. When the user has
      // selected a different (often empty) project, open that workspace first
      // so the new session lands there instead of the current active root.
      const target = (inProjectPath || selectedProjectPath || "").trim();
      const current = (workspaceInfo?.repo || "").trim();
      if (target && (!current || !repoPathsEqual(target, current))) {
        const opened = await handleOpenProject(target);
        if (!opened) return;
      } else if (target) {
        setExpandedProjects((prev) => ({ ...prev, [target]: true }));
      }
      const created = await api.createSession();
      // Seed an empty warm-cache entry before Conversation's switch effect runs
      // so it never paints the previous session's transcript under this id.
      if (created?.id) {
        writeTranscriptCache(created.id, []);
      }
      await refreshSessionsRef.current();
    } catch (err) {
      notifySessionActivationBlocked(err);
    }
  };
  useEffect(() => {
    const onNew = () => { void newSession(); };
    window.addEventListener("harness-new-session", onNew);
    return () => window.removeEventListener("harness-new-session", onNew);
  }, []);
  const handleDeleteSession = async (id: string) => {
    // Optimistic: drop the id from every per-root cache immediately so phantom
    // titles cannot linger under a non-active project while the network round
    // trip completes (the bug that produced "merged dir" ghosts).
    purgeSessionFromRootCaches(projectsRef.current, id);
    setSessionsCacheEpoch((n) => n + 1);

    try {
      const res = await api.deleteSession(id);
      await refreshSessionsRef.current();
      if (res.active) {
        await switchSession(res.active);
      }
    } catch (err) {
      // Restore caches from the server if delete failed after the optimistic purge.
      await refreshSessionsRef.current();
      console.error(err);
    }
  };

  const handleExport = (sid: string, format: "md" | "json") => {
    const url = api.exportUrl(sid, format);
    const a = document.createElement("a");
    a.href = url;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  };

  const handleContextMenu = (e: React.MouseEvent, s: Session) => {
    e.preventDefault();
    setContextMenu({
      x: e.clientX,
      y: e.clientY,
      sessionId: s.id,
      archived: !!s.archived,
      running: runners[s.id] === "running",
    });
  };

  const stopBackgroundSession = async (sessionId: string) => {
    try {
      await api.interruptSession(sessionId);
      setRunners((prev) => ({ ...prev, [sessionId]: "idle" }));
    } catch (err) {
      console.error("Failed to interrupt background session:", err);
    }
  };

  const activeSessions = sessions.filter((s) => !s.archived);
  const archivedSessions = sessions.filter((s) => s.archived);

  const rawRecents = workspaceInfo?.recents || [];
  // Stable PROJECTS order: recents as-is, append current only if missing.
  // Do NOT put currentRepo first -- that snapped the opened dir to the top
  // on every workspace open and blinked the rail.
  const projects = buildProjectsList(currentRepo, rawRecents);
  projectsRef.current = projects;

  // Refresh EVERY project's sessions:${root} cache (not just the active SWR
  // key). Delete/create/rename under an inactive root otherwise left phantom
  // titles that, when clicked, looked like a "merged" project tree.
  const refreshAllProjectSessions = useCallback(async () => {
    const roots = projectsRef.current.filter(Boolean);
    await Promise.all(
      roots.map(async (root) => {
        try {
          const rows = await api.sessions(root);
          writeSWRCache(`sessions:${root}`, rows);
          setSessionsResolvedRoots((prev) => ({ ...prev, [root]: true }));
        } catch {
          setSessionsResolvedRoots((prev) => ({ ...prev, [root]: true }));
        }
      }),
    );
    setSessionsCacheEpoch((n) => n + 1);
    // Keep the active-repo SWR hook in sync (promotes active id, etc.).
    await revalidateSessions();
  }, [revalidateSessions]);
  refreshSessionsRef.current = refreshAllProjectSessions;

  // Eager per-root lists: prefetch sessions for EVERY project in the rail so
  // non-active dirs show their rows without waiting for a click. Seeds the
  // SWR cache under sessions:${path}; projectSessionsFor always reads that.
  useEffect(() => {
    let cancelled = false;
    const roots = projects.filter(Boolean);
    if (roots.length === 0) return;
    void Promise.all(
      roots.map(async (root) => {
        try {
          const rows = await api.sessions(root);
          if (cancelled) return;
          writeSWRCache(`sessions:${root}`, rows);
          setSessionsResolvedRoots((prev) => ({ ...prev, [root]: true }));
          // Active-repo hook already owns promotion; only seed cache here.
        } catch {
          if (!cancelled) {
            setSessionsResolvedRoots((prev) => ({ ...prev, [root]: true }));
          }
        }
      }),
    ).then(() => {
      if (!cancelled) setSessionsCacheEpoch((n) => n + 1);
    });
    return () => { cancelled = true; };
    // projects is rebuilt each render from workspaceInfo; join for stable dep.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projects.join("\0")]);

  const projectSessionsFor = (projectPath: string): Session[] => {
    // sessionsCacheEpoch: force re-read after writeSWRCache from delete/refresh.
    void sessionsCacheEpoch;
    // Always prefer the per-root cache -- never derive other rows from the
    // active-repo list (that caused empty/wrong lists under slash/case drift).
    // Trust cache contents: they were fetched with ?repo= for this root, so
    // the backend already applied visibility (including legacy orphans).
    const cached = readSWRCache<Session[]>(`sessions:${projectPath}`);
    const rows = cached
      ? cached.filter((s) => !s.archived)
      : (repoPathsEqual(projectPath, currentRepo)
        ? activeSessions.filter((s) => {
            const root = s.workspace_root || s.repo || "";
            // Empty root = legacy orphan visible everywhere (backend contract).
            return !root || repoPathsEqual(root, projectPath);
          })
        : []);
    // Client guard: rootless orphans must only paint under the *active*
    // workspace row. Prefetch into sessions:${otherRoot} otherwise shows
    // foreign titles under huge dirs (Ashita); clicking them switches away.
    const isActiveRow = repoPathsEqual(projectPath, currentRepo);
    return rows.filter((s) => {
      const root = (s.workspace_root || s.repo || "").trim();
      if (!root) return isActiveRow;
      return repoPathsEqual(root, projectPath);
    });
  };

  const sessionsResolvedFor = (projectPath: string): boolean =>
    !!sessionsResolvedRoots[projectPath] || readSWRCache<Session[]>(`sessions:${projectPath}`) !== undefined;

  const codegraphStatusFor = (projectPath: string, isCurrentActive: boolean) => {
    if (isCurrentActive && workspaceInfo?.codegraph_status) return workspaceInfo.codegraph_status;
    return codegraphByRepoRef.current[projectPath];
  };

  // Keep the highlighted project aligned with the backend workspace when it changes
  // (open folder, session switch, etc.).
  useEffect(() => {
    if (currentRepo) {
      setSelectedProjectPath(currentRepo);
      dispatchProjectSelected(currentRepo);
    }
  }, [currentRepo]);

  const selectProject = (projectPath: string) => {
    setSelectedProjectPath(projectPath);
    dispatchProjectSelected(projectPath);
  };

  useEffect(() => { void revalidateJobs(); }, [jobsRefresh, revalidateJobs]);

  // Poll runner statuses so session rows can show running/idle without opening
  // a conversation. Same endpoint Conversation already uses for resume/swarm.
  usePolling(() => api.getSessionState().then((res) => {
    if (res?.runners) setRunners(res.runners);
  }), 4000);

  useEffect(() => { saveHiddenSessionJobs(hiddenJobIds); }, [hiddenJobIds]);

  useEffect(() => {
    const clampToViewport = () => {
      setSessionJobsHeight((h) => clampSessionJobsHeight(h));
      setBranchesHeight((h) => clampBranchesHeight(h));
    };
    clampToViewport();
    window.addEventListener("resize", clampToViewport);
    return () => window.removeEventListener("resize", clampToViewport);
  }, [archivedExpanded, archivedSessions.length, workspaceInfo?.is_git, projects.length, sessionJobsCollapsed, sessionJobsHeight, workspaces.length]);

  const toggleSessionJobsCollapsed = () => {
    setSessionJobsCollapsed((v) => {
      const next = !v;
      localStorage.setItem(SESSION_JOBS_COLLAPSED_KEY, next ? "1" : "0");
      return next;
    });
  };

  const sortedJobs = jobs.slice().reverse();
  const visibleJobs = sortedJobs.filter(
    (j) => !hiddenJobIds.has(j.id) || !isTerminalJob(j),
  );
  const hiddenJobCount = sortedJobs.filter(
    (j) => hiddenJobIds.has(j.id) && isTerminalJob(j),
  ).length;
  const terminalVisibleJobs = visibleJobs.filter((j) => isTerminalJob(j));

  const clearFinishedJobs = () => {
    setHiddenJobIds((prev) => {
      const next = new Set(prev);
      for (const j of terminalVisibleJobs) next.add(j.id);
      return next;
    });
    setConfirmClearJobs(false);
  };

  const restoreHiddenJobs = () => setHiddenJobIds(new Set());

  const displayedJobs = showAllJobs
    ? visibleJobs
    : visibleJobs.slice(0, SESSION_JOBS_DISPLAY_CAP);
  const hasMoreJobs = visibleJobs.length > SESSION_JOBS_DISPLAY_CAP;

  const handleProjectContextMenu = (e: React.MouseEvent, path: string) => {
    e.preventDefault();
    setProjectContextMenu({
      x: e.clientX,
      y: e.clientY,
      projectPath: path,
    });
  };

  const handleProjectRowClick = (projectPath: string, isExpanded: boolean) => {
    // Expand/collapse + highlight only. Never open the workspace from a row
    // click -- that used to yank the active dir just to peek at another root's
    // sessions. Activation is session click (switchSession may cross-repo) or
    // the explicit Open folder control.
    selectProject(projectPath);
    setExpandedProjects((prev) => ({
      ...prev,
      [projectPath]: !isExpanded,
    }));
  };

  return (
    <aside ref={railRef} className="bg-panel border-r border-edge flex flex-col h-full overflow-hidden">
      <div ref={topChromeRef}>
      {/* Slim draggable bar to clear the macOS traffic lights; no product label
          (the title bar already names the app, like Cursor/Hermes). */}
      <div style={{ height: 30, WebkitAppRegion: "drag" } as React.CSSProperties} />
      
      <div className="px-3 pb-2 border-b border-edge flex flex-col gap-1.5">
        <button
          onClick={() => { void newSession(); }}
          className="w-full flex items-center gap-2 px-2.5 py-2 rounded-md text-[13px] font-medium text-txt bg-panel2/60 hover:bg-panel2 border border-edge/60 transition">
          <SquarePen size={14} className="text-accent" />
          New session
        </button>
        <button
          onClick={handleOpenFolder}
          disabled={opening}
          className="w-full text-center text-accent text-[11px] font-semibold py-1 hover:bg-accent/10 rounded transition disabled:opacity-50"
        >
          {opening ? "Opening…" : "Open Folder..."}
        </button>
        {sessionActivationNotice && (
          <div
            role="status"
            className="rounded-md border border-warn/40 bg-warn/10 px-2.5 py-2 text-[11px] leading-snug text-txt"
          >
            <div className="flex items-start gap-2">
              <p className="flex-1 min-w-0">{sessionActivationNotice}</p>
              <button
                type="button"
                onClick={() => setSessionActivationNotice(null)}
                className="shrink-0 text-[10px] text-muted hover:text-txt font-semibold"
                aria-label="Dismiss"
              >
                Dismiss
              </button>
            </div>
          </div>
        )}
      </div>
      </div>

      <div ref={upperSectionsRef} className={`flex-1 min-h-0 overflow-y-auto overflow-x-hidden min-w-0 ${panelOpacityClass(panelSwitching, sessionsStale || workspaceStale)}`}>
      {/* Projects | Sessions toggle */}
      <div className="px-3 pt-3 flex items-center gap-1">
        <button
          type="button"
          onClick={() => {
            setRailTab("projects");
            try { localStorage.setItem("pmharness.leftRail.tab", "projects"); } catch { /* ignore */ }
          }}
          className={`flex-1 text-[11px] font-semibold uppercase tracking-wider py-1 rounded transition ${
            railTab === "projects" ? "bg-panel2 text-txt" : "text-muted hover:text-txt hover:bg-panel2/40"
          }`}
        >
          Projects
        </button>
        <button
          type="button"
          onClick={() => {
            setRailTab("sessions");
            try { localStorage.setItem("pmharness.leftRail.tab", "sessions"); } catch { /* ignore */ }
          }}
          className={`flex-1 text-[11px] font-semibold uppercase tracking-wider py-1 rounded transition ${
            railTab === "sessions" ? "bg-panel2 text-txt" : "text-muted hover:text-txt hover:bg-panel2/40"
          }`}
        >
          Sessions
        </button>
      </div>

      {/* GLOBAL SESSIONS BANK */}
      {railTab === "sessions" && (
        <>
          <div className="px-3 pt-2 pb-1">
            <div className="relative">
              <Search className="absolute left-2 top-1/2 -translate-y-1/2 text-faint" size={12} />
              <input
                type="search"
                value={sessionSearchQuery}
                onChange={(e) => setSessionSearchQuery(e.target.value)}
                placeholder="Search sessions..."
                aria-label="Search sessions"
                className="w-full bg-panel border border-edge rounded text-[11px] text-txt
                           pl-7 pr-7 py-1.5 outline-none focus:border-accent placeholder:text-faint"
              />
              {sessionSearchQuery.trim() ? (
                <button
                  type="button"
                  onClick={() => setSessionSearchQuery("")}
                  aria-label="Clear session search"
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-faint hover:text-txt"
                >
                  <X size={12} />
                </button>
              ) : null}
            </div>
          </div>
          {sessionSearchQuery.trim() ? (
            <Section title="Results" headerSpinner={sessionSearchLoading}>
              {!sessionSearchLoading && sessionSearchRows.length === 0 && (
                <Empty>No matches</Empty>
              )}
              <div className="space-y-0.5 pb-2">
                {sessionSearchRows.map((row) => (
                  <button
                    key={row.id}
                    type="button"
                    disabled={!!switchingSessionId || opening}
                    onClick={() => { if (!switchingSessionId) void switchSession(row.id); }}
                    className={`w-full text-left px-2 py-1.5 rounded transition min-w-0 disabled:opacity-60 ${
                      switchingSessionId === row.id ? "bg-panel2/60 border-l-2 border-accent" : "hover:bg-panel2/30"
                    }`}
                    title={row.snippet ? `${row.title}\n${row.snippet}` : row.title}
                  >
                    <div className="flex items-center gap-1.5 min-w-0">
                      {switchingSessionId === row.id
                        ? <Loader2 size={11} className="shrink-0 animate-spin text-accent" />
                        : null}
                      <div className="text-[12.5px] truncate flex-1 text-muted">
                        {row.title}
                      </div>
                    </div>
                    {row.snippet ? (
                      <div className="text-[10px] text-faint truncate">{row.snippet}</div>
                    ) : null}
                  </button>
                ))}
              </div>
            </Section>
          ) : (
            <Section title="Recent" headerSpinner={bankLoading}>
              {bankSessions.length === 0 && !bankLoading && <Empty>No sessions</Empty>}
              <div className="space-y-0.5 pb-2">
                {bankSessions.map((s) => {
                  const root = s.workspace_root || s.repo || "";
                  const label = getWorkspaceBasename(root) || "Home";
                  const isActive = !!s.active;
                  return (
                    <button
                      key={s.id}
                      type="button"
                      disabled={!!switchingSessionId || opening}
                      onClick={() => { if (!switchingSessionId) void switchSession(s.id); }}
                      className={`w-full text-left px-2 py-1.5 rounded transition min-w-0 disabled:opacity-60 ${
                        isActive ? "bg-panel2/60 border-l-2 border-accent" : "hover:bg-panel2/30"
                      }`}
                      title={`${s.title}${s.preview ? `\n${s.preview}` : ""}\n${root}`}
                    >
                      <div className="flex items-center gap-1.5 min-w-0">
                        {switchingSessionId === s.id
                          ? <Loader2 size={11} className="shrink-0 animate-spin text-accent" />
                          : null}
                        <div className={`text-[12.5px] truncate flex-1 ${isActive ? "text-txt font-semibold" : "text-muted"}`}>
                          {s.title || "Untitled"}
                        </div>
                      </div>
                      {s.preview ? (
                        <div className="text-[10px] text-faint truncate pl-0.5">{s.preview}</div>
                      ) : null}
                      <div className="text-[10px] text-faint truncate font-mono">{label}</div>
                    </button>
                  );
                })}
              </div>
            </Section>
          )}
        </>
      )}

      {/* PROJECTS SECTION */}
      {railTab === "projects" && (
      <div ref={projectsSectionRef}>
      <Section title="Projects" headerSpinner={panelSwitching && (sessionsValidating || workspaceValidating)}>
        {projects.length === 0 && !panelSwitching && <Empty>No projects</Empty>}
        <div className="space-y-1">
          {projects.map((projectPath) => {
            const basename = getWorkspaceBasename(projectPath) || "Untitled Project";
            const isCurrentActive = !!(workspaceInfo?.repo && repoPathsEqual(projectPath, workspaceInfo.repo));
            const isSelected = repoPathsEqual(projectPath, selectedProjectPath);
            // Expand is mostly user-driven; open/switch/newSession also expand
            // the landing root so sessions appear without an extra click.
            const isExpanded = !!expandedProjects[projectPath];
            const browsingOther =
              isSelected && !!workspaceInfo?.repo && !repoPathsEqual(projectPath, workspaceInfo.repo);
            const projectSessions = projectSessionsFor(projectPath);
            projectSessions.sort((a, b) => b.created - a.created);
            const count = projectSessions.length;
            const cgStatus = codegraphStatusFor(projectPath, isCurrentActive);
            const cgLabel = cgStatus ? codegraphBadgeLabel(cgStatus) : "";
            const showCgBadge = !!cgLabel && (isCurrentActive || isSelected);
            const sessionsReady = sessionsResolvedFor(projectPath);
            const sessionsEmptyState = projectSessionsEmptyState(sessionsReady, isSelected);

            return (
              <div
                key={projectPath}
                className={`rounded transition min-w-0 overflow-hidden ${
                  browsingOther
                    ? "bg-panel2/50 border-l-2 border-warn/70"
                    : isSelected
                      ? "bg-panel2/50 border-l-2 border-accent"
                      : "hover:bg-panel2/20"
                }`}
              >
                {/* Project Row */}
                <div
                  onClick={() => handleProjectRowClick(projectPath, isExpanded)}
                  onContextMenu={(e) => handleProjectContextMenu(e, projectPath)}
                  className="flex items-center gap-1.5 px-2 py-1.5 cursor-pointer select-none group"
                  title={browsingOther
                    ? `${projectPath}\nBrowsing sessions — click a session to open this project`
                    : projectPath}
                >
                  {/* Expand Chevron */}
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      selectProject(projectPath);
                      setExpandedProjects(prev => ({ ...prev, [projectPath]: !isExpanded }));
                    }}
                    className="p-0.5 hover:bg-panel2 rounded text-muted hover:text-txt transition-colors flex items-center justify-center"
                  >
                    {isExpanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                  </button>

                  {/* Folder Icon */}
                  {isCurrentActive && workspaceInfo?.is_git ? (
                    <FolderGit2 size={13} className="text-accent shrink-0" />
                  ) : (
                    <Folder size={13} className="text-muted shrink-0" />
                  )}

                  {/* Basename */}
                  <span className={`text-[12.5px] truncate font-medium flex-1 ${isSelected ? "text-txt font-semibold" : "text-muted hover:text-txt"}`}>
                    {basename}
                  </span>

                  {/* CodeGraph status (inline compact) */}
                  {showCgBadge && (
                    <span className={`text-[9px] font-semibold uppercase px-1 rounded shrink-0 ${
                      cgStatus === "ready"
                        ? "text-good bg-good/10"
                        : cgStatus === "indexing" || cgStatus === "pending"
                          ? "text-warn bg-warn/10 animate-pulse"
                          : cgStatus === "needs_scope"
                            ? "text-warn bg-warn/10"
                            : "text-faint bg-panel2"
                    }`}>
                      {cgLabel}
                    </span>
                  )}

                  {/* Session Count Badge */}
                  {count > 0 && (
                    <span className="text-[10px] text-faint px-1.5 py-0.2 rounded bg-panel2 font-mono shrink-0">
                      {count}
                    </span>
                  )}
                </div>

                {/* Sessions (Expandable inline) — stale-while-revalidate: keep
                    gold selection + expansion; fill rows when cache arrives.
                    Scoped loading on this row only (not rail-wide dim). */}
                {isExpanded && (
                  <div className={`pl-4 pr-1 pb-1.5 space-y-0.5 border-l border-edge/30 ml-3.5 mt-0.5 min-w-0 overflow-hidden ${panelOpacityClass(!sessionsReady && isSelected)}`}>
                    {projectSessions.length === 0 ? (
                      sessionsEmptyState === "loading" ? (
                        <div className="text-[11px] text-faint italic px-2 py-1 flex items-center gap-1.5">
                          <Loader2 size={10} className="animate-spin shrink-0" />
                          Loading sessions...
                        </div>
                      ) : sessionsEmptyState === "pending" ? null : (
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            void newSession(projectPath);
                          }}
                          className="w-full text-left text-[11px] text-accent hover:text-accent/80 px-2 py-1 rounded hover:bg-accent/10 transition"
                          title={`Open ${basename} and start a session`}
                        >
                          New session
                        </button>
                      )
                    ) : (
                      projectSessions.map((s) => (
                        <div key={s.id} className="group relative">
                          {renamingId === s.id ? (
                            <input
                              type="text"
                              value={renamingTitle}
                              onChange={(e) => setRenamingTitle(e.target.value)}
                              onBlur={() => handleRenameSubmit(s.id)}
                              onKeyDown={(e) => {
                                if (e.key === "Enter") {
                                  handleRenameSubmit(s.id);
                                } else if (e.key === "Escape") {
                                  setRenamingId(null);
                                }
                              }}
                              autoFocus
                              className="w-full bg-bg border border-accent rounded px-2 py-1 text-[12px] text-txt focus:outline-none"
                            />
                          ) : (
                            <div className="flex items-center gap-0.5 min-w-0">
                              <button
                                onClick={() => { if (!switchingSessionId) void switchSession(s.id); }}
                                disabled={!!switchingSessionId || opening}
                                onDoubleClick={() => {
                                  setRenamingId(s.id);
                                  setRenamingTitle(s.title || "Untitled");
                                }}
                                onContextMenu={(e) => handleContextMenu(e, s)}
                                className={`flex-1 min-w-0 text-left rounded px-1.5 py-1 flex items-start gap-1.5 text-[12.5px] transition disabled:opacity-60
                                  ${s.active ? "bg-accent/10 text-accent font-semibold" : "hover:bg-panel2/60 text-muted hover:text-txt"}
                                  ${switchingSessionId === s.id ? "opacity-70" : ""}`}>
                                {switchingSessionId === s.id
                                  ? <Loader2 size={11} className="shrink-0 mt-0.5 animate-spin text-accent" />
                                  : <MessageSquare size={11} className={`shrink-0 mt-0.5 ${s.active ? "text-accent" : "text-faint"}`} />}
                                <span className="flex-1 min-w-0">
                                  <span className="block truncate">{s.title || "Untitled"}</span>
                                  {s.preview ? (
                                    <span className="block truncate text-[10px] font-normal text-faint">{s.preview}</span>
                                  ) : null}
                                </span>
                                <RunnerStatusDot
                                  status={runners[s.id]}
                                  stoppable={shouldOfferBackgroundStop(runners[s.id], !!s.active)}
                                  onStop={() => { void stopBackgroundSession(s.id); }}
                                />
                              </button>
                              {confirmDeleteId === s.id ? (
                                <div className="flex items-center gap-1 shrink-0 pr-0.5">
                                  <button
                                    onClick={async () => {
                                      await handleDeleteSession(s.id);
                                      setConfirmDeleteId(null);
                                    }}
                                    className="text-[10px] text-red-400 font-semibold hover:underline"
                                  >
                                    Yes
                                  </button>
                                  <button
                                    onClick={() => setConfirmDeleteId(null)}
                                    className="text-[10px] text-muted hover:underline"
                                  >
                                    No
                                  </button>
                                </div>
                              ) : (
                                <button
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setConfirmDeleteId(s.id);
                                  }}
                                  title="Delete session"
                                  className="opacity-0 group-hover:opacity-100 p-0.5 rounded text-faint hover:text-red-400 hover:bg-panel2 transition-all shrink-0"
                                >
                                  <Trash2 size={11} />
                                </button>
                              )}
                            </div>
                          )}
                        </div>
                      ))
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </Section>
      </div>
      )}

      {/* BRANCH SWITCHING / WORKSPACES */}
      {railTab === "projects" && workspaceInfo?.is_git && (
        <Section
          title="Branches"
          action={
            <div className="flex items-center gap-0.5">
              <IconBtn
                onClick={() => { void pruneEditBranches(); }}
                title="Prune unused edit/worker branches"
                disabled={pruningBranches}
              >
                {pruningBranches ? <Loader2 size={13} className="animate-spin" /> : <Brush size={13} />}
              </IconBtn>
              <IconBtn onClick={newWs} title="New branch"><Plus size={13} /></IconBtn>
            </div>
          }
        >
          {workspaces.length === 0 && <Empty>No branches</Empty>}
          <div className="space-y-0.5 overflow-y-auto" style={{ height: branchesHeight }}>
            {workspaces.map((w) => (
              <button key={w.name} onClick={() => switchWs(w.name)}
                className={`w-full text-left rounded px-2 py-1 mb-0.5 flex items-center gap-2 text-[12px] transition
                  ${w.active ? "bg-accent2/40 text-txt font-semibold" : "hover:bg-panel2/60 text-muted"}`}>
                {swapping === w.name ? <Loader2 size={11} className="animate-spin" /> : <GitBranch size={11} />}
                <span className="flex-1 truncate">{w.name}</span>
                {w.dirty && <span className="w-1.5 h-1.5 rounded-full bg-warn" title="uncommitted changes" />}
                {w.active && <Check size={11} className="text-accent" />}
              </button>
            ))}
          </div>
          <div
            role="separator"
            aria-orientation="horizontal"
            aria-label="Resize branches list"
            onPointerDown={onBranchesResizePointerDown}
            onPointerMove={onBranchesResizePointerMove}
            onPointerUp={finishBranchesResize}
            onPointerCancel={finishBranchesResize}
            className="h-1.5 mt-0.5 cursor-row-resize touch-none flex items-center justify-center group shrink-0"
          >
            <div className="w-8 h-0.5 rounded-full bg-edge/80 group-hover:bg-muted/80 transition-colors" />
          </div>
        </Section>
      )}

      {/* ARCHIVED SESSIONS */}
      {railTab === "projects" && archivedSessions.length > 0 && (
        <Section title="Archived">
          <button
            onClick={() => setArchivedExpanded(!archivedExpanded)}
            className="w-full text-left px-2 py-1 text-[10px] uppercase tracking-wider text-faint font-medium hover:text-muted flex items-center justify-between"
          >
            <span>Sessions ({archivedSessions.length})</span>
            {archivedExpanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
          </button>
          {archivedExpanded && (
            <div className="mt-1 pl-1 border-l border-edge space-y-0.5">
              {archivedSessions.map((s) => (
                <div key={s.id} className="group relative">
                  {renamingId === s.id ? (
                    <input
                      type="text"
                      value={renamingTitle}
                      onChange={(e) => setRenamingTitle(e.target.value)}
                      onBlur={() => handleRenameSubmit(s.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          handleRenameSubmit(s.id);
                        } else if (e.key === "Escape") {
                          setRenamingId(null);
                        }
                      }}
                      autoFocus
                      className="w-full bg-bg border border-accent rounded px-2 py-1 text-[12px] text-txt focus:outline-none"
                    />
                  ) : (
                    <button onClick={() => switchSession(s.id)}
                      onDoubleClick={() => {
                        setRenamingId(s.id);
                        setRenamingTitle(s.title || "Untitled");
                      }}
                      onContextMenu={(e) => handleContextMenu(e, s)}
                      className={`w-full text-left rounded px-2 py-1 flex items-center gap-1.5 text-[12.5px] transition opacity-60 hover:opacity-100
                        ${s.active ? "bg-accent/10 text-accent font-semibold" : "hover:bg-panel2/60 text-muted"}`}>
                      <MessageSquare size={11} />
                      <span className="flex-1 truncate">{s.title || "Untitled"}</span>
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </Section>
      )}

      </div>

      {/* JOBS -- clean task-list styling: a
          slim status row per job, click to expand a card with richer detail
          (adapter/role, tokens/cost, artifact headlines) instead of a lone
          line of truncated text. Bounded height + collapsible header so a long
          session doesn't swallow the left rail. Vertically resizable via the
          grab handle above the header. */}
      <div
        className={`px-2 shrink-0 border-t border-edge/40 min-w-0 flex flex-col ${panelOpacityClass(panelSwitching || jobsTransitioning, jobsStale)}`}
        style={sessionJobsCollapsed ? undefined : { height: sessionJobsHeight }}
      >
        {!sessionJobsCollapsed && (
          <div
            role="separator"
            aria-orientation="horizontal"
            aria-label="Resize session jobs panel"
            onPointerDown={onSessionJobsResizePointerDown}
            onPointerMove={onSessionJobsResizePointerMove}
            onPointerUp={finishSessionJobsResize}
            onPointerCancel={finishSessionJobsResize}
            className="h-1.5 -mt-1.5 mb-0.5 cursor-row-resize touch-none flex items-center justify-center group shrink-0"
          >
            <div className="w-8 h-0.5 rounded-full bg-edge/80 group-hover:bg-muted/80 transition-colors" />
          </div>
        )}
        <div className={`flex items-center justify-between px-2 mb-1.5 gap-2 min-w-0 shrink-0 ${sessionJobsCollapsed ? "pt-4 mt-0.5" : "pt-1 mt-0"}`}>
          <button
            onClick={toggleSessionJobsCollapsed}
            className="flex items-center gap-1 min-w-0 text-[11px] uppercase tracking-wider text-muted font-semibold hover:text-txt focus:outline-none"
          >
            {sessionJobsCollapsed ? <ChevronRight size={11} className="shrink-0" /> : <ChevronDown size={11} className="shrink-0" />}
            <span className="truncate">Session Jobs</span>
            {jobsValidating && !sessionJobsCollapsed && (
              <Loader2 size={10} className="animate-spin text-muted shrink-0" />
            )}
            {visibleJobs.length > 0 && (
              <span className="text-faint/70 normal-case tracking-normal shrink-0">({visibleJobs.length})</span>
            )}
          </button>
          {!sessionJobsCollapsed && terminalVisibleJobs.length > 0 && (
            confirmClearJobs ? (
              <div className="flex items-center gap-2 text-[10px] shrink-0">
                <span className="text-muted">Clear all?</span>
                <button
                  onClick={clearFinishedJobs}
                  className="text-red-400 font-semibold hover:underline"
                >
                  Yes
                </button>
                <button
                  onClick={() => setConfirmClearJobs(false)}
                  className="text-muted hover:underline"
                >
                  No
                </button>
              </div>
            ) : (
              <button
                onClick={() => setConfirmClearJobs(true)}
                className="text-[10px] text-faint hover:text-red-400 transition-colors shrink-0"
              >
                Clear jobs
              </button>
            )
          )}
        </div>
        {!sessionJobsCollapsed && (
          <div className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden min-w-0 pb-1">
            {visibleJobs.length === 0 ? (
              <div className="px-1 py-1">
                {jobsValidating ? (
                  <div className="text-[11px] text-faint italic px-1 py-1 flex items-center gap-1.5">
                    <Loader2 size={10} className="animate-spin shrink-0" />
                    Loading jobs...
                  </div>
                ) : (
                <Empty>
                  {hiddenJobCount > 0 ? "All session jobs cleared" : "No jobs yet"}
                </Empty>
                )}
                {hiddenJobCount > 0 && (
                  <button
                    onClick={restoreHiddenJobs}
                    className="mt-1 px-1 text-[10px] text-accent hover:underline focus:outline-none"
                  >
                    Show {hiddenJobCount} hidden job{hiddenJobCount === 1 ? "" : "s"}
                  </button>
                )}
              </div>
            ) : (
              <>
                {displayedJobs.map((j) => {
                  const st = jobStatus(j);
                  const isOpen = !!expandedJobs[j.id];
                  const detail = jobDetailBits(j);
                  const loadedArts = artifactsByJob[j.id];
                  const arts = (loadedArts || []).filter((a) => a && a.headline);
                  const diff = jobDiffstat(loadedArts || []);
                  return (
                    <div key={j.id} className="rounded mb-0.5 bg-panel2 border border-edge overflow-hidden min-w-0">
                      <button
                        onClick={() => toggleJobCard(j)}
                        className="w-full min-w-0 flex items-center gap-1.5 px-2 py-1.5 text-left hover:bg-panel2/60 transition-colors focus:outline-none"
                      >
                        <JobStatusIcon status={st} />
                        <span
                          className={`flex-1 min-w-0 truncate text-[12px] ${st === "completed" ? "text-muted" : st === "cancelled" ? "text-red-400/90" : "text-txt"}`}
                          title={j.goal}
                        >
                          {j.goal}
                        </span>
                        {diff && (
                          <span
                            className="shrink-0 flex items-center gap-1 text-[10px] tabular-nums font-medium"
                            title={`${diff.files} file${diff.files === 1 ? "" : "s"} changed, ${diff.insertions} insertion${diff.insertions === 1 ? "" : "s"}, ${diff.deletions} deletion${diff.deletions === 1 ? "" : "s"}`}
                          >
                            {diff.insertions > 0 && <span className="text-good">+{diff.insertions}</span>}
                            {diff.deletions > 0 && <span className="text-red-400/90">-{diff.deletions}</span>}
                          </span>
                        )}
                        <ChevronDown size={11} className={`text-faint shrink-0 transition-transform ${isOpen ? "rotate-180" : ""}`} />
                      </button>
                      {isOpen && (
                        <div className="px-2 pb-1.5 pt-1 border-t border-edge/50 space-y-1.5 min-w-0 max-h-48 overflow-y-auto overflow-x-hidden">
                          <p className={`text-[12px] leading-snug break-words whitespace-normal ${st === "completed" ? "text-muted" : st === "cancelled" ? "text-red-400/90" : "text-txt"}`}>
                            {j.goal}
                          </p>
                          {detail.length > 0 && (
                            <div className="flex flex-wrap gap-x-2 gap-y-0.5 text-[10px] text-faint">
                              {detail.map((d, i) => (
                                <span key={i} className="tabular-nums">{d}</span>
                              ))}
                            </div>
                          )}
                          {diff && (
                            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] tabular-nums text-faint">
                              <span>{diff.files} file{diff.files === 1 ? "" : "s"} changed</span>
                              {diff.insertions > 0 && <span className="text-good">+{diff.insertions}</span>}
                              {diff.deletions > 0 && <span className="text-red-400/90">-{diff.deletions}</span>}
                            </div>
                          )}
                          {arts.length > 0 ? (
                            <div className="space-y-0.5">
                              {arts.map((a, i) => (
                                <div key={a.id || i} className="text-[11px] text-txt/90 flex items-start gap-1.5 leading-snug min-w-0">
                                  <span className="text-good mt-[3px] shrink-0">·</span>
                                  <span className="flex-1 min-w-0 break-words whitespace-normal">{a.headline}</span>
                                </div>
                              ))}
                            </div>
                          ) : loadedArts === undefined ? (
                            <div className="text-[10px] text-faint italic">Loading artifacts...</div>
                          ) : (
                            <div className="text-[10px] text-faint italic">No artifacts recorded</div>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
                {hasMoreJobs && !showAllJobs && (
                  <button
                    onClick={() => setShowAllJobs(true)}
                    className="w-full px-2 py-1 text-[10px] text-accent hover:underline focus:outline-none"
                  >
                    Show all ({visibleJobs.length})
                  </button>
                )}
                {hiddenJobCount > 0 && (
                  <button
                    onClick={restoreHiddenJobs}
                    className="w-full px-2 py-1 text-[10px] text-faint hover:text-accent hover:underline focus:outline-none"
                  >
                    Show {hiddenJobCount} hidden job{hiddenJobCount === 1 ? "" : "s"}
                  </button>
                )}
              </>
            )}
          </div>
        )}
      </div>

      {/* CONTEXT MENU */}
      {contextMenu && (
        <div
          className="fixed z-50 bg-panel border border-edge rounded shadow-lg text-[12px] py-1 min-w-[150px]"
          style={{ top: contextMenu.y, left: contextMenu.x }}
          onClick={(e) => e.stopPropagation()}
        >
          {contextMenu.running && (
            <>
              <button
                onClick={async () => {
                  await stopBackgroundSession(contextMenu.sessionId);
                  setContextMenu(null);
                }}
                className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
              >
                Stop
              </button>
              <div className="border-t border-edge my-1" />
            </>
          )}
          <button
            onClick={() => {
              handleExport(contextMenu.sessionId, "md");
              setContextMenu(null);
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            Export as Markdown
          </button>
          <button
            onClick={() => {
              handleExport(contextMenu.sessionId, "json");
              setContextMenu(null);
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            Export as JSON
          </button>
          <div className="border-t border-edge my-1" />
          <button
            onClick={async () => {
              await api.archiveSession(contextMenu.sessionId, !contextMenu.archived);
              await refreshSessionsRef.current();
              setContextMenu(null);
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            {contextMenu.archived ? "Unarchive" : "Archive"}
          </button>
          <div className="border-t border-edge my-1" />
          {confirmDeleteId === contextMenu.sessionId ? (
            <div className="px-3 py-1.5 flex items-center justify-between gap-2 bg-panel2/50">
              <span className="text-muted font-medium">Delete?</span>
              <div className="flex gap-2">
                <button
                  onClick={async () => {
                    await handleDeleteSession(contextMenu.sessionId);
                    setContextMenu(null);
                    setConfirmDeleteId(null);
                  }}
                  className="text-red-400 font-bold hover:underline"
                >
                  Yes
                </button>
                <button
                  onClick={() => setConfirmDeleteId(null)}
                  className="text-muted hover:underline"
                >
                  No
                </button>
              </div>
            </div>
          ) : (
            <button
              onClick={() => {
                setConfirmDeleteId(contextMenu.sessionId);
              }}
              className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-red-400 font-medium transition-colors"
            >
              Delete
            </button>
          )}
        </div>
      )}

      {/* PROJECT CONTEXT MENU */}
      {projectContextMenu && (
        <div
          className="fixed z-50 bg-panel border border-edge rounded shadow-lg text-[12px] py-1 min-w-[150px]"
          style={{ top: projectContextMenu.y, left: projectContextMenu.x }}
          onClick={(e) => e.stopPropagation()}
        >
          {confirmForgetPath === projectContextMenu.projectPath ? (
            <div className="px-3 py-1.5 flex items-center justify-between gap-2 bg-panel2/50">
              <span className="text-muted font-medium">Remove?</span>
              <div className="flex gap-2">
                <button
                  onClick={async () => {
                    await handleForgetProject(projectContextMenu.projectPath);
                    setProjectContextMenu(null);
                    setConfirmForgetPath(null);
                  }}
                  className="text-accent font-bold hover:underline"
                >
                  Yes
                </button>
                <button
                  onClick={() => setConfirmForgetPath(null)}
                  className="text-muted hover:underline"
                >
                  No
                </button>
              </div>
            </div>
          ) : (
            <button
              onClick={() => {
                setConfirmForgetPath(projectContextMenu.projectPath);
              }}
              className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
            >
              Remove from list
            </button>
          )}
        </div>
      )}
    </aside>
  );
}

type JobStatus = "pending" | "in_progress" | "completed" | "cancelled";

const SESSION_JOBS_COLLAPSED_KEY = "pmharness.leftRail.sessionJobsCollapsed";
const SESSION_JOBS_HEIGHT_KEY = "pmharness.leftRail.sessionJobsHeight.v1";
const SESSION_JOBS_HIDDEN_KEY = "pmharness.leftRail.hiddenSessionJobs.v1";
const SESSION_JOBS_DISPLAY_CAP = 20;

const BRANCHES_HEIGHT_KEY = "pmharness.leftRail.branchesHeight.v1";
const BRANCHES_MIN_HEIGHT = 90;
const BRANCHES_DEFAULT_HEIGHT = 140;
const BRANCHES_PROJECTS_RESERVE = 160;

function sessionJobsMinHeight(): number {
  if (typeof window === "undefined") return 280;
  return Math.min(280, Math.round(window.innerHeight * 0.35));
}

function loadSessionJobsHeight(): number {
  const fallback = sessionJobsMinHeight();
  try {
    const raw = localStorage.getItem(SESSION_JOBS_HEIGHT_KEY);
    if (!raw) return fallback;
    const n = Number.parseInt(raw, 10);
    if (!Number.isFinite(n) || n <= 0) return fallback;
    // Upper-bound against the window BEFORE first paint. The layout-aware
    // clamp (getMaxSessionJobsHeight) needs refs that only exist after mount,
    // so a tall height saved from a big window would otherwise flash a jobs
    // panel that swallows the whole rail for one frame in a small window.
    const conservativeMax = typeof window === "undefined"
      ? n
      : Math.max(sessionJobsMinHeight(), Math.round(window.innerHeight * 0.6));
    return Math.min(conservativeMax, Math.max(sessionJobsMinHeight(), n));
  } catch {
    return fallback;
  }
}

function saveSessionJobsHeight(height: number): void {
  try {
    localStorage.setItem(SESSION_JOBS_HEIGHT_KEY, String(Math.round(height)));
  } catch {
    // localStorage full/unavailable -- height still works for this session.
  }
}

function loadBranchesHeight(): number {
  try {
    const raw = localStorage.getItem(BRANCHES_HEIGHT_KEY);
    if (!raw) return BRANCHES_DEFAULT_HEIGHT;
    const n = Number.parseInt(raw, 10);
    if (!Number.isFinite(n) || n <= 0) return BRANCHES_DEFAULT_HEIGHT;
    const conservativeMax = typeof window === "undefined"
      ? n
      : Math.max(BRANCHES_MIN_HEIGHT, Math.round(window.innerHeight * 0.4));
    return Math.min(conservativeMax, Math.max(BRANCHES_MIN_HEIGHT, n));
  } catch {
    return BRANCHES_DEFAULT_HEIGHT;
  }
}

function saveBranchesHeight(height: number): void {
  try {
    localStorage.setItem(BRANCHES_HEIGHT_KEY, String(Math.round(height)));
  } catch {
    // localStorage full/unavailable -- height still works for this session.
  }
}

function loadHiddenSessionJobs(): Set<string> {
  try {
    const raw = localStorage.getItem(SESSION_JOBS_HIDDEN_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(arr) ? arr : []);
  } catch {
    return new Set();
  }
}

function saveHiddenSessionJobs(ids: Set<string>): void {
  try {
    localStorage.setItem(SESSION_JOBS_HIDDEN_KEY, JSON.stringify([...ids].slice(-2000)));
  } catch {
    // localStorage full/unavailable -- hide state still works for this session.
  }
}

function isTerminalJob(j: Job): boolean {
  const st = jobStatus(j);
  return st === "completed" || st === "cancelled";
}

function jobStatus(j: Job): JobStatus {
  const s = (j.status || "").toLowerCase();
  if (s.includes("complete") || s.includes("done")) return "completed";
  if (s.includes("fail") || s.includes("cancel") || s.includes("error") || s.includes("stall")) return "cancelled";
  if (s.includes("run") || s.includes("progress") || s.includes("active")) return "in_progress";
  return "pending";
}

// Compact metadata chips shown when a job card is expanded -- role/adapter and
// usage so the card carries real signal instead of a truncated goal line.
function jobDetailBits(j: Job): string[] {
  const bits: string[] = [];
  const status = (j.status || "").split(".").pop();
  if (status) bits.push(status);
  if (j.role) bits.push(j.role);
  if (j.adapter) bits.push(j.adapter);
  if (typeof j.task_count === "number" && j.task_count > 0) bits.push(`${j.task_count} task${j.task_count === 1 ? "" : "s"}`);
  if (typeof j.tokens === "number" && j.tokens > 0) bits.push(`${j.tokens.toLocaleString()} tok`);
  if (typeof j.est_cost_usd === "number" && j.est_cost_usd > 0) bits.push(`$${j.est_cost_usd.toFixed(3)}`);
  return bits;
}

// Aggregate diffstat across a job's patch artifacts so a card can show a
// git-style "+40 -12" summary at a glance. Returns null when the job produced
// no patch (audits, reviews) so the caller can skip the row entirely.
function jobDiffstat(artifacts: Artifact[]): { files: number; insertions: number; deletions: number } | null {
  const patches = artifacts.filter((a) => a && a.diffstat);
  if (patches.length === 0) return null;
  let files = 0;
  let insertions = 0;
  let deletions = 0;
  for (const a of patches) {
    const d = a.diffstat!;
    files += d.files || 0;
    insertions += d.insertions || 0;
    deletions += d.deletions || 0;
  }
  if (!(files || insertions || deletions)) return null;
  return { files, insertions, deletions };
}

function JobStatusIcon({ status }: { status: JobStatus }) {
  if (status === "completed") return <CheckCircle2 size={12} className="text-good shrink-0" />;
  if (status === "in_progress") return <Loader2 size={12} className="animate-spin text-accent shrink-0" />;
  if (status === "cancelled") return <XCircle size={12} className="text-red-400 shrink-0" />;
  return <Circle size={12} className="text-muted shrink-0" />;
}

/** Compact running/idle indicator for a session row. Hidden when status unknown.
 *  When stoppable (running + non-active), click Stops that runner without view attach. */
function RunnerStatusDot({
  status,
  stoppable,
  onStop,
}: {
  status?: "running" | "idle";
  stoppable?: boolean;
  onStop?: () => void;
}) {
  if (!status) return null;
  const running = status === "running";
  if (stoppable && running && onStop) {
    return (
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          e.preventDefault();
          onStop();
        }}
        className="w-1.5 h-1.5 rounded-full shrink-0 bg-accent hover:ring-2 hover:ring-accent/40 transition"
        title="Stop (free lease slot)"
        aria-label="Stop background session"
      />
    );
  }
  return (
    <span
      className={`w-1.5 h-1.5 rounded-full shrink-0 ${running ? "bg-accent" : "bg-muted/50"}`}
      title={running ? "Running" : "Idle"}
    />
  );
}

function Section({ title, action, headerSpinner, children }: {
  title: string;
  action?: React.ReactNode;
  headerSpinner?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="px-2 pt-4 shrink-0 min-w-0">
      <div className="flex items-center justify-between px-2 mb-2 mt-0.5">
        <span className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted font-semibold">
          {title}
          {headerSpinner && <Loader2 size={10} className="animate-spin text-muted shrink-0" />}
        </span>
        {action}
      </div>
      {children}
    </div>
  );
}
const IconBtn = ({ onClick, children, title, disabled }: {
  onClick?: () => void;
  children?: React.ReactNode;
  title?: string;
  disabled?: boolean;
}) => (
  <button
    onClick={onClick}
    title={title}
    disabled={disabled}
    className="text-muted hover:text-txt p-0.5 rounded hover:bg-panel2 disabled:opacity-50 disabled:pointer-events-none"
  >
    {children}
  </button>
);
const Empty = ({ children }: any) => <div className="text-[11px] text-muted italic px-1 py-1">{children}</div>;
