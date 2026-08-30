import { useCallback, useEffect, useRef, useState } from "react";
import { GitBranch, Plus, MessageSquare, Check, Loader2, ChevronDown, ChevronRight, SquarePen, Folder, FolderGit2, CheckCircle2, Circle, Trash2, Brush, Search, X, Square } from "lucide-react";
import { api, type Workspace, type WorkspaceInfo, type Session, type Job, type Artifact } from "../lib/api";
import { pickFolder } from "../lib/transport";
import { dispatchProjectSelected, dispatchProjectSwitching, panelOpacityClass } from "../lib/panelTransition";
import { repoPathsEqual } from "../lib/pathNormalize";
import { mapSessionSearchHits, type SessionSearchRow } from "../lib/sessionSearch";
import { displaySessionListTitle } from "../lib/sessionTitle";
import { usePolling } from "../lib/usePolling";
import { readSWRCache, writeSWRCache, useStaleWhileRevalidate } from "../lib/useStaleWhileRevalidate";
import {
  copyTranscriptId,
  downloadTextFile,
  formatSessionExportMarkdown,
  sessionExportFilename,
  transcriptIdOf,
} from "../lib/sessionExport";
import { writeTranscriptCache } from "./Conversation";
import { sharedReadinessNotice } from "../lib/operationalDiagnostic";
import { useOperationalDiagnostic } from "../lib/useOperationalDiagnostic";
import { filterJobsByScope, loadJobScope, saveJobScope, type JobScope } from "../lib/jobScope";
import { filterBranchWorkspaces } from "./leftRailBranches";

export {
  SESSION_LEASE_EXHAUSTED_MESSAGE,
  type LeaseExhaustedPayload,
  isLeaseExhaustedError,
  formatLeaseExhaustedMessage,
  buildProjectsList,
  filterForgottenRecent,
  pickFallbackProjectAfterForget,
  canSettleSessionsForProject,
  partitionProjectSessions,
  readSessionSettledFromCaches,
  patchSessionSettledInCaches,
  patchSessionTitleInCaches,
  patchSessionArchivedInCaches,
  purgeSessionFromRootCaches,
  workspacesCacheKey,
  jobsCacheKey,
  shouldOfferBackgroundStop,
  collectUnreadFinishedSessionIds,
  isRailWideSwitching,
  projectSessionsEmptyState,
} from "./leftRailSessions";

import {
  isLeaseExhaustedError,
  formatLeaseExhaustedMessage,
  buildProjectsList,
  filterForgottenRecent,
  pickFallbackProjectAfterForget,
  canSettleSessionsForProject,
  partitionProjectSessions,
  readSessionSettledFromCaches,
  patchSessionSettledInCaches,
  patchSessionTitleInCaches,
  patchSessionArchivedInCaches,
  purgeSessionFromRootCaches,
  workspacesCacheKey,
  jobsCacheKey,
  shouldOfferBackgroundStop,
  collectUnreadFinishedSessionIds,
  isRailWideSwitching,
  projectSessionsEmptyState,
  type RunnerStatus,
} from "./leftRailSessions";
import { Section, IconBtn, Empty, JobStatusIcon, RunnerStatusDot, type JobStatus } from "./leftRailPrimitives";

export default function LeftRail({ jobsRefresh, onSessionChange }: {
  jobsRefresh: number;
  onSessionChange?: (id: string) => void;
}) {
  const [swapping, setSwapping] = useState<string | null>(null);
  const operationalDiagnostic = useOperationalDiagnostic();
  const [contextMenu, setContextMenu] = useState<{
    x: number;
    y: number;
    sessionId: string;
    title: string;
    settled: boolean;
    archived: boolean;
    running: boolean;
    /** False when browsing a non-active project (API would 403). */
    canSettle: boolean;
  } | null>(null);
  /** Per-project Settled section expand; collapsed by default. */
  const [expandedSettled, setExpandedSettled] = useState<Record<string, boolean>>(() => {
    try {
      const raw = localStorage.getItem(SETTLED_EXPANDED_KEY);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === "object" ? parsed as Record<string, boolean> : {};
    } catch {
      return {};
    }
  });
  const [archivedExpanded, setArchivedExpanded] = useState(false);
  const settleUndoRef = useRef<{ sid: string; priorSettled: boolean } | null>(null);
  const bankAllRef = useRef<Session[]>([]);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [projectContextMenu, setProjectContextMenu] = useState<{
    x: number;
    y: number;
    projectPath: string;
  } | null>(null);
  const [confirmForgetPath, setConfirmForgetPath] = useState<string | null>(null);
  const [sessionJobsCollapsed, setSessionJobsCollapsed] = useState(
    () => localStorage.getItem(SESSION_JOBS_COLLAPSED_KEY) === "1",
  );
  const [hiddenJobIds, setHiddenJobIds] = useState<Set<string>>(loadHiddenSessionJobs);
  const [jobScope, setJobScope] = useState<JobScope>(() => loadJobScope());
  useEffect(() => {
    const onScope = () => setJobScope(loadJobScope());
    window.addEventListener("harness-job-scope-changed", onScope);
    return () => window.removeEventListener("harness-job-scope-changed", onScope);
  }, []);
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
    // Keep Projects (and a little settled chrome) from being crushed out
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
  const [runners, setRunners] = useState<Record<string, RunnerStatus>>({});
  // Hermes-style: green unread cue when a non-active session finishes a turn.
  const [unreadFinishedIds, setUnreadFinishedIds] = useState<Record<string, true>>({});
  const runnersPrevRef = useRef<Record<string, RunnerStatus>>({});

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

  const codegraphAttentionLabel = (cgStatus?: string) => {
    if (!cgStatus || cgStatus === "ready" || cgStatus === "none") return "";
    if (cgStatus === "needs_scope") return "scope";
    if (cgStatus === "pending") return "indexing";
    if (cgStatus === "unsupported") return "failed";
    return cgStatus;
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
  // One-shot: expand the already-open workspace on first boot so sessions under
  // the active project are visible without an extra click. Subsequent
  // currentRepo flips stay user-driven (handleOpenProject / row click).
  const bootExpandedRef = useRef(false);
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

  const beginSessionRename = (id: string, title: string) => {
    setRenamingId(id);
    setRenamingTitle(title || "Untitled");
  };

  const handleRenameSubmit = async (id: string) => {
    const title = renamingTitle.trim();
    if (!title) {
      setRenamingId(null);
      return;
    }
    const roots = projectsRef.current.filter(Boolean);
    patchSessionTitleInCaches(roots, id, title);
    setSessionsCacheEpoch((n) => n + 1);
    setBankSessions((prev) => prev.map((row) => (row.id === id ? { ...row, title } : row)));
    setRenamingId(null);
    try {
      await api.renameSession(id, title);
      await refreshSessionsRef.current();
    } catch (err) {
      console.error(err);
      await refreshSessionsRef.current();
    }
  };

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
    revalidate: revalidateJobs,
  } = useStaleWhileRevalidate<Job[]>(
    jobsCacheKey(selectedProjectPath, sessions.find((session) => session.active)?.id),
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

  const openProjectWorkspace = useCallback(async (
    path: string,
    options?: { quiet?: boolean },
  ): Promise<{ ok: boolean; created_session?: boolean; active_session?: string }> => {
    if (!options?.quiet) setOpening(true);
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
        return {
          ok: true,
          created_session: res.created_session,
          active_session: res.active_session,
        };
      }
      if ((res as { code?: string }).code === "lease_exhausted") {
        notifySessionActivationBlocked(res);
      } else if (!options?.quiet) {
        alert("Failed to open directory: " + (res as any).error);
      }
      return { ok: false };
    } catch (err: any) {
      if (!notifySessionActivationBlocked(err) && !options?.quiet) {
        alert("Error opening directory: " + (err?.error || err?.message || err));
      }
      return { ok: false };
    } finally {
      if (!options?.quiet) setOpening(false);
    }
  }, [
    mutateWorkspace,
    notifySessionActivationBlocked,
    revalidateSessions,
    revalidateWorkspace,
    revalidateWorkspaces,
    workspaceInfo?.recents,
  ]);

  const handleForgetProject = async (path: string) => {
    const previous = workspaceInfo;
    const forgettingActive = !!(previous?.repo && repoPathsEqual(previous.repo, path));
    const nextRecents = previous ? filterForgottenRecent(previous.recents || [], path) : [];
    const fallback = forgettingActive
      ? pickFallbackProjectAfterForget(nextRecents, path, previous?.home)
      : "";
    mutateWorkspace(previous
      ? {
          ...previous,
          recents: nextRecents,
          // Drop active repo immediately so buildProjectsList cannot re-append
          // the forgotten path as a phantom row. When another project remains,
          // land there optimistically so the rail never greys out on a stale cwd.
          ...(forgettingActive
            ? {
                repo: fallback || "",
                branch: "",
                is_git: false,
                codegraph_status: "none",
              }
            : {}),
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
      if (fallback) {
        setExpandedProjects((prev) => ({ ...prev, [fallback]: true }));
        setSelectedProjectPath(fallback);
        dispatchProjectSelected(fallback);
      } else {
        setSelectedProjectPath("");
        dispatchProjectSelected("");
      }
    }
    try {
      const res = await api.forgetWorkspace(path);
      mutateWorkspace(previous
        ? {
            ...previous,
            recents: res.recents,
            ...(forgettingActive
              ? {
                  repo: fallback || (res.cleared_active ? (res.repo || "") : previous.repo),
                  branch: "",
                  is_git: false,
                  codegraph_status: "none",
                }
              : {
                  repo: res.cleared_active ? (res.repo || "") : previous.repo,
                  ...(res.cleared_active
                    ? { branch: "", is_git: false, codegraph_status: "none" }
                    : {}),
                }),
          }
        : undefined);
      if (forgettingActive && fallback) {
        await openProjectWorkspace(fallback, { quiet: true });
      } else if (res.cleared_active) {
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

  const handleOpenProject = async (
    path: string,
  ): Promise<{ ok: boolean; created_session?: boolean; active_session?: string }> =>
    openProjectWorkspace(path);

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

  const openWorktreeBranch = async (name: string, worktreePath: string) => {
    setSwapping(name);
    try {
      const opened = await handleOpenProject(worktreePath);
      if (opened.ok) {
        const kind = name.startsWith("pmworker-")
          ? "worker"
          : name.startsWith("pmedit-")
            ? "edit"
            : "linked";
        toast(`Opened ${kind} worktree · ${name}`);
      }
    } finally {
      setSwapping(null);
    }
  };

  const switchWs = async (name: string) => {
    const row = workspaces.find((w) => w.name === name);
    if (row?.active) return;
    // Linked edit/worker branches live in a sibling worktree — open that folder
    // directly (no confirm, no scary git fatal).
    if (row?.worktree_path) {
      await openWorktreeBranch(name, row.worktree_path);
      return;
    }
    setSwapping(name);
    try {
      let res = await api.switchWorkspace(name);
      if (!res.ok && res.dirty) {
        const paths = Array.isArray((res as { dirty_paths?: string[] }).dirty_paths)
          ? (res as { dirty_paths?: string[] }).dirty_paths!
          : [];
        const pathHint = paths.length
          ? `\n\nTracked changes:\n${paths.slice(0, 8).join("\n")}${paths.length > 8 ? `\n(+${paths.length - 8} more)` : ""}`
          : "";
        const proceed = window.confirm(
          `Uncommitted tracked changes in this repo. Switch branch anyway? (may fail if checkout would overwrite files)${pathHint}`,
        );
        if (!proceed) return;
        res = await api.switchWorkspace(name, { allow_dirty: true });
      }
      if (!res.ok) {
        const worktreePath = res.worktree_path;
        if (res.worktree_busy && worktreePath) {
          await openWorktreeBranch(name, worktreePath);
          return;
        }
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
      "Delete unused local edit/worker branches (pmedit-*, pmworker-*) and leftover release/v0.9.* branches that origin already dropped? Active checkout and live worktree-attached branches are kept.",
    );
    if (!proceed) return;
    setPruningBranches(true);
    try {
      const res = await api.pruneEditBranches();
      await revalidateWorkspaces();
      const count = typeof res.count === "number" ? res.count : (res.deleted?.length ?? 0);
      toast(count > 0
        ? `Pruned ${count} unused branch${count === 1 ? "" : "es"}`
        : "No unused edit or leftover release branches to prune");
    } catch (err: any) {
      toast(err?.error || err?.message || "Could not prune edit branches");
    } finally {
      setPruningBranches(false);
    }
  };
  const switchSession = async (id: string) => {
    if (switchingSessionId || opening) return;
    setSwitchingSessionId(id);
    setUnreadFinishedIds((prev) => {
      if (!prev[id]) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
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
      const all = Array.isArray(rows) ? rows : [];
      bankAllRef.current = all;
      // Recent stays active-primary; settled/archived remain available for search labels.
      setBankSessions(all.filter((s) => !s.settled && !s.archived));
    } catch {
      bankAllRef.current = [];
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
          const settledById: Record<string, boolean> = {};
          for (const s of bankAllRef.current) {
            if (!s?.id) continue;
            titleById[s.id] = s.title || "";
            settledById[s.id] = !!s.settled;
          }
          for (const s of sessions) {
            if (!s?.id) continue;
            if (titleById[s.id] == null) titleById[s.id] = s.title || "";
            if (settledById[s.id] == null) settledById[s.id] = !!s.settled;
          }
          for (const root of projectsRef.current.filter(Boolean)) {
            const cached = readSWRCache<Session[]>(`sessions:${root}`);
            for (const s of cached || []) {
              if (!s?.id) continue;
              if (titleById[s.id] == null) titleById[s.id] = s.title || "";
              if (settledById[s.id] == null) settledById[s.id] = !!s.settled;
            }
          }
          setSessionSearchRows(mapSessionSearchHits(hits, titleById, settledById));
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
      let workspaceCreatedSession = false;
      let newSessionId = "";
      if (target && (!current || !repoPathsEqual(target, current))) {
        const opened = await handleOpenProject(target);
        if (!opened.ok) return;
        workspaceCreatedSession = !!opened.created_session;
        newSessionId = (opened.active_session || "").trim();
      } else if (target) {
        setExpandedProjects((prev) => ({ ...prev, [target]: true }));
      }
      if (!workspaceCreatedSession) {
        const created = await api.createSession();
        newSessionId = (created?.id || "").trim();
      }
      // Seed an empty warm-cache entry before Conversation's switch effect runs
      // so it never paints the previous session's transcript under this id.
      // seededEmpty marks this as New Session (not an ambiguous zero-row cache).
      if (newSessionId) {
        writeTranscriptCache(newSessionId, [], { seededEmpty: true });
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

  const handleExport = async (sid: string, format: "md" | "json") => {
    try {
      const payload = await api.exportSession(sid);
      const id = transcriptIdOf(payload, sid);
      const title = payload.title || "session";
      if (format === "json") {
        downloadTextFile(
          sessionExportFilename(title, id, "json"),
          JSON.stringify(payload, null, 2),
          "application/json",
        );
      } else {
        downloadTextFile(
          sessionExportFilename(title, id, "md"),
          formatSessionExportMarkdown(payload, sid),
          "text/markdown",
        );
      }
      toast(`Exported ${format.toUpperCase()} · ${id}`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Export failed";
      toast(msg);
    }
  };

  const handleCopyTranscriptId = async (sid: string) => {
    const ok = await copyTranscriptId(sid);
    toast(ok ? `Copied transcript ID ${sid}` : "Could not copy transcript ID");
  };

  const handleContextMenu = (e: React.MouseEvent, s: Session, allowSettle: boolean) => {
    e.preventDefault();
    setContextMenu({
      x: e.clientX,
      y: e.clientY,
      sessionId: s.id,
      title: displaySessionListTitle(s.title),
      settled: !!s.settled,
      archived: !!s.archived,
      running: runners[s.id] === "running",
      canSettle: allowSettle,
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

  const settledSessions = sessions.filter((s) => s.settled && !s.archived);
  const archivedSessions = sessions.filter((s) => s.archived);

  const settleSession = async (sid: string, settled: boolean) => {
    const roots = projectsRef.current.filter(Boolean);
    const priorSettled = readSessionSettledFromCaches(roots, sid) ?? !settled;
    patchSessionSettledInCaches(roots, sid, settled);
    setSessionsCacheEpoch((n) => n + 1);
    try {
      await api.settleSession(sid, settled);
      settleUndoRef.current = { sid, priorSettled };
      window.dispatchEvent(new CustomEvent("harness-toast", {
        detail: {
          message: settled ? "Settled" : "Unsettled",
          actionLabel: "Undo",
          actionEvent: "harness-settle-undo",
        },
      }));
      await refreshSessionsRef.current();
      if (railTab === "sessions") void refreshBankSessions();
    } catch (err) {
      console.error(err);
      patchSessionSettledInCaches(roots, sid, priorSettled);
      setSessionsCacheEpoch((n) => n + 1);
      const e = err as { message?: string; error?: string } | undefined;
      const detail = String(e?.error || e?.message || err || "").trim();
      toast(
        detail
          ? `Could not ${settled ? "settle" : "unsettle"} session: ${detail}`
          : `Could not ${settled ? "settle" : "unsettle"} session`,
      );
      await refreshSessionsRef.current();
    }
  };

  const archiveSession = async (sid: string, archived: boolean) => {
    const roots = projectsRef.current.filter(Boolean);
    const prior = sessions.find((s) => s.id === sid)?.archived
      ?? readSWRCache<Session[]>(`sessions:${currentRepo}`)?.find((s) => s.id === sid)?.archived
      ?? !archived;
    patchSessionArchivedInCaches(roots, sid, archived);
    setSessionsCacheEpoch((n) => n + 1);
    try {
      await api.archiveSession(sid, archived);
      await refreshSessionsRef.current();
      if (railTab === "sessions") void refreshBankSessions();
    } catch (err) {
      console.error(err);
      patchSessionArchivedInCaches(roots, sid, !!prior);
      setSessionsCacheEpoch((n) => n + 1);
      const e = err as { message?: string; error?: string } | undefined;
      const detail = String(e?.error || e?.message || err || "").trim();
      toast(
        detail
          ? `Could not ${archived ? "archive" : "unarchive"} session: ${detail}`
          : `Could not ${archived ? "archive" : "unarchive"} session`,
      );
      await refreshSessionsRef.current();
    }
  };

  useEffect(() => {
    const onUndo = () => {
      const pending = settleUndoRef.current;
      if (!pending) return;
      settleUndoRef.current = null;
      void settleSession(pending.sid, pending.priorSettled);
    };
    window.addEventListener("harness-settle-undo", onUndo);
    return () => window.removeEventListener("harness-settle-undo", onUndo);
    // settleSession closes over latest caches/refs; rebind when rail tab changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [railTab]);

  const rawRecents = workspaceInfo?.recents || [];
  // Stable PROJECTS order: pin Home first, then recents as-is, append current
  // only if missing. Do NOT put currentRepo first -- that snapped the opened
  // dir to the top on every workspace open and blinked the rail.
  const projects = buildProjectsList(currentRepo, rawRecents, workspaceInfo?.home);
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

  const projectSessionBuckets = (projectPath: string): { open: Session[]; settled: Session[] } => {
    // sessionsCacheEpoch: force re-read after writeSWRCache from delete/refresh.
    void sessionsCacheEpoch;
    // Always prefer the per-root cache -- never derive other rows from the
    // active-repo list (that caused empty/wrong lists under slash/case drift).
    // Trust cache contents: they were fetched with ?repo= for this root, so
    // the backend already applied visibility (including legacy orphans).
    const isActiveRow = repoPathsEqual(projectPath, currentRepo);
    const cached = readSWRCache<Session[]>(`sessions:${projectPath}`);
    const rows = cached
      ? cached
      : (isActiveRow
        ? sessions.filter((s) => {
            const root = s.workspace_root || s.repo || "";
            // Empty root = legacy orphan visible everywhere (backend contract).
            return !root || repoPathsEqual(root, projectPath);
          })
        : []);
    return partitionProjectSessions(rows, projectPath, isActiveRow);
  };

  const projectSessionsFor = (projectPath: string): Session[] =>
    projectSessionBuckets(projectPath).open;

  const projectSettledFor = (projectPath: string): Session[] =>
    projectSessionBuckets(projectPath).settled;

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

  // Boot expand once when workspaceInfo.repo is already set (or first becomes
  // truthy). Does not re-expand/collapse on later currentRepo changes.
  useEffect(() => {
    if (bootExpandedRef.current || !currentRepo) return;
    bootExpandedRef.current = true;
    setExpandedProjects((prev) => ({ ...prev, [currentRepo]: true }));
  }, [currentRepo]);

  useEffect(() => { void revalidateJobs(); }, [jobsRefresh, revalidateJobs]);

  // Poll runner statuses so session rows can show running/idle without opening
  // a conversation. Same endpoint Conversation already uses for resume/swarm.
  usePolling(() => api.getSessionState().then((res) => {
    if (!res?.runners) return;
    const next = res.runners as Record<string, RunnerStatus>;
    const finished = collectUnreadFinishedSessionIds(
      runnersPrevRef.current,
      next,
      sessions.find((s) => s.active)?.id,
    );
    runnersPrevRef.current = next;
    setRunners(next);
    if (finished.length) {
      setUnreadFinishedIds((prev) => {
        const merged = { ...prev };
        for (const id of finished) merged[id] = true;
        return merged;
      });
    }
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
  }, [archivedExpanded, archivedSessions.length, settledSessions.length, workspaceInfo?.is_git, projects.length, sessionJobsCollapsed, sessionJobsHeight, workspaces.length]);

  const toggleSessionJobsCollapsed = () => {
    setSessionJobsCollapsed((v) => {
      const next = !v;
      localStorage.setItem(SESSION_JOBS_COLLAPSED_KEY, next ? "1" : "0");
      return next;
    });
  };

  const activeSessionId = sessions.find((session) => session.active)?.id || "";
  const sortedJobs = filterJobsByScope(jobs.slice().reverse(), jobScope, activeSessionId);
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
    // Home is a UI pin, not a forgettable recent — skip remove chrome.
    const homePath = workspaceInfo?.home || "";
    if (homePath && repoPathsEqual(path, homePath)) return;
    e.preventDefault();
    setProjectContextMenu({
      x: e.clientX,
      y: e.clientY,
      projectPath: path,
    });
  };

  const handleProjectRowClick = (projectPath: string, isExpanded: boolean) => {
    // Browsing a project only changes its expansion. Activation is a separate
    // path: a session click, explicit Open folder, or New session.
    setExpandedProjects((prev) => ({
      ...prev,
      [projectPath]: !isExpanded,
    }));
  };

  return (
    <aside ref={railRef} className="bg-transparent flex flex-col h-full overflow-hidden text-[0.8125rem]">
      <div ref={topChromeRef}>
      {/* Keep the native traffic-light/titlebar area separate from the actions.
          The same draggable region also preserves window movement on other
          desktop platforms. */}
      <div
        aria-hidden="true"
        className="h-12 shrink-0"
        style={{ WebkitAppRegion: "drag" } as React.CSSProperties}
      />

      <div className="px-2.5 pb-2 border-b border-edge/50">
        <button
          type="button"
          onClick={() => { void newSession(); }}
          className="w-full h-8 grid grid-cols-[14px_minmax(0,1fr)] items-center gap-x-2 px-2 rounded text-left text-[12.5px] font-medium text-txt hover:bg-panel2/60 transition">
          <SquarePen size={14} className="text-muted" />
          <span>New session</span>
        </button>
        <button
          type="button"
          onClick={handleOpenFolder}
          disabled={opening}
          className="w-full h-8 grid grid-cols-[14px_minmax(0,1fr)] items-center gap-x-2 px-2 text-left text-accent text-[11px] font-medium hover:bg-accent/10 rounded transition disabled:opacity-50"
        >
          <span aria-hidden="true" />
          <span>{opening ? "Opening…" : "Open Folder..."}</span>
        </button>
        {sessionActivationNotice && (
          <div
            role="status"
            className="rounded border border-warn/30 bg-warn/5 px-2 py-1.5 text-[11px] leading-snug text-txt"
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

      <div ref={upperSectionsRef} className={`min-h-0 overflow-y-auto overflow-x-hidden min-w-0 ${panelOpacityClass(panelSwitching, sessionsStale || workspaceStale)}`}>
      {/* Projects | Sessions toggle */}
      <div className="px-2.5 pt-2 flex items-center gap-0 border-b border-edge/35">
        <button
          type="button"
          onClick={() => {
            setRailTab("projects");
            try { localStorage.setItem("pmharness.leftRail.tab", "projects"); } catch { /* ignore */ }
          }}
          className={`flex-1 h-8 flex items-center justify-center text-center px-2 text-[10px] font-semibold uppercase tracking-[0.12em] border-b transition ${
            railTab === "projects" ? "border-accent text-txt" : "border-transparent text-muted hover:text-txt"
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
          className={`flex-1 h-8 flex items-center justify-center text-center px-2 text-[10px] font-semibold uppercase tracking-[0.12em] border-b transition ${
            railTab === "sessions" ? "border-accent text-txt" : "border-transparent text-muted hover:text-txt"
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
                className="w-full bg-panel2/40 border border-edge/60 rounded text-[11px] text-txt
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
                    className={`w-full min-h-8 flex flex-col justify-center text-left px-2 rounded transition min-w-0 disabled:opacity-60 ${
                      switchingSessionId === row.id ? "bg-panel2/60 border-l-2 border-accent" : "hover:bg-panel2/30"
                    }`}
                    title={row.snippet ? `${displaySessionListTitle(row.title)}\n${row.snippet}` : displaySessionListTitle(row.title)}
                  >
                    <div className="flex items-center gap-1.5 min-w-0">
                      {switchingSessionId === row.id
                        ? <Loader2 size={11} className="shrink-0 animate-spin text-accent" />
                        : null}
                      <div className="text-[12.5px] truncate flex-1 text-muted">
                        {displaySessionListTitle(row.title)}
                      </div>
                      {row.settled ? (
                        <span className="shrink-0 text-[9px] uppercase tracking-wider text-faint font-medium">
                          Settled
                        </span>
                      ) : null}
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
                  if (renamingId === s.id) {
                    return (
                      <input
                        key={s.id}
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
                    );
                  }
                  return (
                    <button
                      key={s.id}
                      type="button"
                      disabled={!!switchingSessionId || opening}
                      onClick={() => { if (!switchingSessionId) void switchSession(s.id); }}
                      onDoubleClick={() => beginSessionRename(s.id, displaySessionListTitle(s.title))}
                      onContextMenu={(e) => handleContextMenu(e, s, canSettleSessionsForProject(root, workspaceInfo?.repo))}
                      className={`w-full min-h-8 flex flex-col justify-center text-left px-2 rounded transition min-w-0 disabled:opacity-60 ${
                        isActive ? "bg-panel2/60 border-l-2 border-accent" : "hover:bg-panel2/30"
                      }`}
                      title={`${displaySessionListTitle(s.title)}${s.preview ? `\n${s.preview}` : ""}\n${root}`}
                    >
                      <div className="flex items-center gap-1.5 min-w-0">
                        {switchingSessionId === s.id
                          ? <Loader2 size={11} className="shrink-0 animate-spin text-accent" />
                          : null}
                        <div className={`text-[12.5px] truncate flex-1 ${isActive ? "text-txt font-semibold" : "text-muted"}`}>
                          {displaySessionListTitle(s.title)}
                        </div>
                      </div>
                      <div className="text-[10px] text-faint truncate font-mono">{label}</div>
                    </button>
                  );
                })}
              </div>
            </Section>
          )}
        </>
      )}

      {/* PROJECTS SECTION — tabs already label the pane; no redundant heading. */}
      {railTab === "projects" && (
      <div ref={projectsSectionRef} className="pb-1">
      <div className="px-2 pt-3 pb-2 shrink-0 min-w-0">
        {projects.length === 0 && !panelSwitching && (
          <Empty>{sharedReadinessNotice("No projects", operationalDiagnostic)}</Empty>
        )}
      <div className="space-y-0.5 -mx-2">
          {projects.map((projectPath) => {
            const basename = getWorkspaceBasename(projectPath) || "Untitled Project";
            const isCurrentActive = canSettleSessionsForProject(projectPath, workspaceInfo?.repo);
            const isSelected = repoPathsEqual(projectPath, selectedProjectPath);
            // Expansion is browsing state; activation paths also expand their
            // landing root so sessions appear without an extra click.
            const isExpanded = !!expandedProjects[projectPath];
            const projectSessions = projectSessionsFor(projectPath);
            projectSessions.sort((a, b) => b.created - a.created);
            const projectSettled = projectSettledFor(projectPath);
            projectSettled.sort((a, b) => b.created - a.created);
            const cgStatus = codegraphStatusFor(projectPath, isCurrentActive);
            const cgLabel = codegraphAttentionLabel(cgStatus);
            const sessionsReady = sessionsResolvedFor(projectPath);
            const sessionsEmptyState = projectSessionsEmptyState(sessionsReady, isSelected);

            return (
              <div
                key={projectPath}
                className="min-w-0 overflow-hidden px-2"
              >
                {/* Project Row */}
                <div
                  onClick={() => handleProjectRowClick(projectPath, isExpanded)}
                  onContextMenu={(e) => handleProjectContextMenu(e, projectPath)}
                  className="h-8 flex items-center gap-1.5 px-2 cursor-pointer select-none group hover:bg-panel2/30 rounded-md"
                  title={projectPath}
                >
                  {/* Expand Chevron */}
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      setExpandedProjects(prev => ({ ...prev, [projectPath]: !isExpanded }));
                    }}
                    className="w-5 h-5 p-0 hover:bg-panel2/70 rounded text-faint hover:text-txt transition-colors flex items-center justify-center shrink-0"
                  >
                    {isExpanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                  </button>

                  {/* Folder Icon */}
                  {isCurrentActive && workspaceInfo?.is_git ? (
                    <FolderGit2 size={13} className="text-muted shrink-0" />
                  ) : (
                    <Folder size={13} className="text-faint shrink-0" />
                  )}

                  {/* Basename */}
                  <span className={`text-[12px] truncate flex-1 ${isSelected ? "text-txt font-medium" : "text-muted group-hover:text-txt"}`}>
                    {basename}
                  </span>

                  {/* CodeGraph attention state; ready is intentionally silent. */}
                  {cgLabel && (
                    <span
                      className={`flex items-center gap-1 text-[9px] font-medium uppercase tracking-wide shrink-0 ${
                        cgStatus === "indexing" || cgStatus === "pending"
                          ? "text-warn"
                          : cgStatus === "unsupported" || cgStatus === "error" || cgStatus === "failed"
                            ? "text-faint"
                            : "text-warn"
                      }`}
                      title={`CodeGraph ${cgLabel}`}
                      aria-label={`CodeGraph ${cgLabel}`}
                    >
                      {cgStatus === "indexing" || cgStatus === "pending"
                        ? <Loader2 size={10} className="animate-spin" />
                        : <Circle size={6} fill="currentColor" />}
                      {cgLabel}
                    </span>
                  )}
                </div>

                {/* Sessions (Expandable inline) — stale-while-revalidate: keep
                    active selection + expansion; fill rows when cache arrives.
                    Scoped loading on this row only (not rail-wide dim). */}
                {isExpanded && (
                  <div className={`pl-3 pr-1 pb-1 space-y-0.5 mt-0.5 min-w-0 overflow-hidden ${panelOpacityClass(!sessionsReady && isSelected)}`}>
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
                          className="w-full h-7 flex items-center text-left text-[11px] text-accent hover:text-accent/80 px-2 rounded hover:bg-accent/10 transition"
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
                            <div className={`group relative flex items-center gap-0.5 min-w-0 min-h-7 rounded-md px-0.5 focus-within:bg-panel2/50 ${
                              s.active ? "bg-panel2/70" : "hover:bg-panel2/50"
                            }`}>
                              <div
                                className="w-3 shrink-0 flex items-center justify-center self-center"
                                aria-hidden={!(unreadFinishedIds[s.id] && !s.active && runners[s.id] !== "running") && (!runners[s.id] || runners[s.id] === "missing")}
                              >
                                {unreadFinishedIds[s.id] && !s.active && runners[s.id] !== "running" ? (
                                  <span
                                    className="w-1.5 h-1.5 rounded-full shrink-0 bg-good"
                                    title="Finished while you were away"
                                    aria-label="Unread finished session"
                                  />
                                ) : (
                                  <RunnerStatusDot
                                    status={
                                      runners[s.id] && runners[s.id] !== "missing"
                                        ? runners[s.id]
                                        : "idle"
                                    }
                                    stoppable={shouldOfferBackgroundStop(runners[s.id], !!s.active)}
                                    onStop={() => { void stopBackgroundSession(s.id); }}
                                  />
                                )}
                              </div>
                              <button
                                onClick={() => { if (!switchingSessionId) void switchSession(s.id); }}
                                disabled={!!switchingSessionId || opening}
                                title={s.preview ? `${displaySessionListTitle(s.title)}\n${s.preview}` : displaySessionListTitle(s.title)}
                                onDoubleClick={() => beginSessionRename(s.id, displaySessionListTitle(s.title))}
                                onContextMenu={(e) => handleContextMenu(e, s, isCurrentActive)}
                                className={`flex-1 min-w-0 h-7 text-left rounded pl-2.5 pr-1.5 flex items-center gap-1.5 text-[12px] transition disabled:opacity-60
                                  ${s.active ? "text-txt font-medium" : "text-muted group-hover:text-txt"}
                                  ${switchingSessionId === s.id ? "opacity-70" : ""}`}>
                                {switchingSessionId === s.id
                                  ? <Loader2 size={11} className="shrink-0 animate-spin text-accent" />
                                  : <MessageSquare size={11} className={`shrink-0 ${s.active ? "text-accent" : "text-faint"}`} />}
                                <span className="flex-1 min-w-0 truncate">{displaySessionListTitle(s.title)}</span>
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
                                <>
                                  {isCurrentActive ? (
                                    <button
                                      onClick={(e) => {
                                        e.stopPropagation();
                                        void settleSession(s.id, true);
                                      }}
                                      title="Settle — move to Settled"
                                      aria-label="Settle session"
                                      className="opacity-0 group-hover:opacity-100 focus-visible:opacity-100 p-0.5 rounded text-faint hover:text-good hover:bg-panel2 motion-safe:transition-all shrink-0 focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent"
                                    >
                                      <CheckCircle2 size={11} />
                                    </button>
                                  ) : null}
                                  <button
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      setConfirmDeleteId(s.id);
                                    }}
                                    title="Delete session"
                                    className="opacity-0 group-hover:opacity-100 focus-visible:opacity-100 p-0.5 rounded text-faint hover:text-red-400 hover:bg-panel2 motion-safe:transition-all shrink-0 focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent"
                                  >
                                    <Trash2 size={11} />
                                  </button>
                                </>
                              )}
                            </div>
                          )}
                        </div>
                      ))
                    )}
                    {projectSettled.length > 0 && (
                      <div className={`${projectSessions.length > 0 ? "mt-1.5 pt-1 border-t border-edge/30" : ""}`}>
                        <button
                          type="button"
                          onClick={() => {
                            setExpandedSettled((prev) => {
                              const next = { ...prev, [projectPath]: !prev[projectPath] };
                              try { localStorage.setItem(SETTLED_EXPANDED_KEY, JSON.stringify(next)); } catch { /* ignore */ }
                              return next;
                            });
                          }}
                          className="w-full h-6 flex items-center gap-1 px-1.5 text-[10px] uppercase tracking-wider text-faint font-medium hover:text-muted focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent rounded"
                          aria-expanded={!!expandedSettled[projectPath]}
                        >
                          {expandedSettled[projectPath]
                            ? <ChevronDown size={10} className="shrink-0" />
                            : <ChevronRight size={10} className="shrink-0" />}
                          <span>Settled · {projectSettled.length}</span>
                        </button>
                        {expandedSettled[projectPath] ? (
                          <div className="space-y-0 motion-safe:transition-opacity">
                            {projectSettled.map((s) => (
                              <div key={s.id} className="group relative flex items-center gap-0.5 min-w-0">
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
                                <button
                                  onClick={() => { if (!switchingSessionId) void switchSession(s.id); }}
                                  disabled={!!switchingSessionId || opening}
                                  onDoubleClick={() => beginSessionRename(s.id, displaySessionListTitle(s.title))}
                                  onContextMenu={(e) => handleContextMenu(e, s, isCurrentActive)}
                                  className={`flex-1 min-w-0 h-6 text-left rounded px-1.5 flex items-center gap-1.5 text-[11px] motion-safe:transition opacity-45 hover:opacity-90 disabled:opacity-40
                                    ${s.active ? "bg-accent/10 text-accent" : "text-faint hover:bg-panel2/50 hover:text-muted"}
                                    ${switchingSessionId === s.id ? "opacity-70" : ""}`}
                                  title={displaySessionListTitle(s.title)}
                                >
                                  <Square size={10} className="shrink-0" />
                                  <span className="truncate">{displaySessionListTitle(s.title)}</span>
                                </button>
                                )}
                                {isCurrentActive ? (
                                  <button
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      void settleSession(s.id, false);
                                    }}
                                    title="Unsettle — return to open list"
                                    aria-label="Unsettle session"
                                    className="opacity-0 group-hover:opacity-100 focus-visible:opacity-100 p-0.5 rounded text-faint hover:text-accent hover:bg-panel2 motion-safe:transition-all shrink-0 focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent"
                                  >
                                    <MessageSquare size={11} />
                                  </button>
                                ) : null}
                              </div>
                            ))}
                          </div>
                        ) : null}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
      </div>
      )}

      {/* ARCHIVED SESSIONS — deeper shelf than Settled; independent of settle. */}
      {railTab === "projects" && archivedSessions.length > 0 && (
        <Section title="Archived" className="mt-2 border-t border-edge/25 pt-5">
          <button
            type="button"
            onClick={() => setArchivedExpanded(!archivedExpanded)}
            className="w-full text-left px-2 py-1 text-[10px] uppercase tracking-wider text-faint font-medium hover:text-muted flex items-center justify-between focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent rounded"
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
                    <button
                      type="button"
                      onClick={() => { if (!switchingSessionId) void switchSession(s.id); }}
                      disabled={!!switchingSessionId || opening}
                      onDoubleClick={() => beginSessionRename(s.id, displaySessionListTitle(s.title))}
                      onContextMenu={(e) => handleContextMenu(e, s, true)}
                      className={`w-full h-7 text-left rounded px-2 flex items-center gap-1.5 text-[12.5px] transition opacity-60 hover:opacity-100 disabled:opacity-40
                        ${s.active ? "bg-accent/10 text-accent font-semibold" : "hover:bg-panel2/60 text-muted"}
                        ${switchingSessionId === s.id ? "opacity-70" : ""}`}
                    >
                      {switchingSessionId === s.id
                        ? <Loader2 size={11} className="shrink-0 animate-spin text-accent" />
                        : <MessageSquare size={11} />}
                      <span className="flex-1 truncate">{displaySessionListTitle(s.title)}</span>
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </Section>
      )}

      {/* BRANCH SWITCHING / WORKSPACES */}
      {railTab === "projects" && workspaceInfo?.is_git && (
        <Section
          title="Branches"
          className="mt-2 border-t border-edge/25 pt-5"
          action={
            <div className="flex items-center gap-0.5">
              <IconBtn
                onClick={() => { void pruneEditBranches(); }}
                title="Prune unused edit/worker and leftover release branches"
                disabled={pruningBranches}
              >
                {pruningBranches ? <Loader2 size={13} className="animate-spin" /> : <Brush size={13} />}
              </IconBtn>
              <IconBtn onClick={newWs} title="New branch"><Plus size={13} /></IconBtn>
            </div>
          }
        >
          {filterBranchWorkspaces(workspaces).length === 0 && (
            <Empty>{workspaceInfo?.head_unborn ? "No commits yet" : "No branches"}</Empty>
          )}
          <div className="space-y-0.5 overflow-y-auto" style={{ maxHeight: branchesHeight }}>
            {filterBranchWorkspaces(workspaces).map((w) => {
              const linked = !!w.worktree_path;
              const linkKind = w.name.startsWith("pmworker-")
                ? "worker"
                : w.name.startsWith("pmedit-")
                  ? "edit"
                  : linked
                    ? "worktree"
                    : null;
              return (
              <button
                key={w.name}
                onClick={() => switchWs(w.name)}
                title={linked
                  ? `Open ${linkKind || "linked"} worktree (separate folder)`
                  : undefined}
                className={`w-full h-7 text-left rounded px-2 mb-0.5 flex items-center gap-2 text-[12px] transition
                  ${w.active ? "bg-accent2/40 text-txt font-semibold" : "hover:bg-panel2/60 text-muted"}`}
              >
                {swapping === w.name
                  ? <Loader2 size={11} className="animate-spin" />
                  : linked
                    ? <FolderGit2 size={11} className="shrink-0 opacity-80" />
                    : <GitBranch size={11} />}
                <span className="flex-1 truncate">{w.name}</span>
                {linkKind && (
                  <span className="text-[9px] uppercase tracking-wider text-muted/80 shrink-0">
                    {linkKind}
                  </span>
                )}
                {w.dirty && <span className="w-1.5 h-1.5 rounded-full bg-warn" title="uncommitted changes" />}
                {w.active && <Check size={11} className="text-accent" />}
              </button>
              );
            })}
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

      </div>

      {/* JOBS -- clean task-list styling: a
          slim status row per job, click to expand a card with richer detail
          (adapter/role, tokens/cost, artifact headlines) instead of a lone
          line of truncated text. Bounded height + collapsible header so a long
          session doesn't swallow the left rail. Vertically resizable via the
          grab handle above the header. */}
      <div
        className="px-2 shrink-0 border-t border-edge/40 min-w-0 flex flex-col"
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
        <div className={`h-7 flex items-center justify-between px-1.5 mb-1 gap-2 min-w-0 shrink-0 ${sessionJobsCollapsed ? "mt-2" : ""}`}>
          <button
            onClick={toggleSessionJobsCollapsed}
            className="h-6 flex items-center gap-1 min-w-0 text-[11px] uppercase tracking-wider text-muted font-semibold hover:text-txt focus:outline-none"
          >
            {sessionJobsCollapsed ? <ChevronRight size={11} className="shrink-0" /> : <ChevronDown size={11} className="shrink-0" />}
            <span className="truncate">Jobs</span>
            {jobsValidating && !sessionJobsCollapsed && (
              <Loader2 size={10} className="animate-spin text-muted shrink-0" />
            )}
            {visibleJobs.length > 0 && (
              <span className="text-faint/70 normal-case tracking-normal shrink-0">({visibleJobs.length})</span>
            )}
          </button>
          {!sessionJobsCollapsed && (
            <div className="flex h-5 overflow-hidden rounded border border-edge/70 shrink-0">
              {(["session", "repo", "all"] as const).map((scope) => (
                <button
                  key={scope}
                  type="button"
                  aria-pressed={jobScope === scope}
                  aria-label={scope === "session" ? "This session" : scope === "repo" ? "This repo" : "All projects"}
                  onClick={(e) => { e.stopPropagation(); setJobScope(scope); saveJobScope(scope); }}
                  className={`px-1.5 text-[9px] uppercase tracking-wider ${jobScope === scope ? "bg-accent/15 text-txt" : "text-muted hover:text-txt"}`}
                >
                  {scope === "session" ? "Session" : scope === "repo" ? "Repo" : "All"}
                </button>
              ))}
            </div>
          )}
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
                  {hiddenJobCount > 0 ? "All jobs in this view cleared" : "No jobs in this view"}
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
                    <div key={j.id} className="border-b border-edge/35 overflow-hidden min-w-0">
                      <button
                        onClick={() => toggleJobCard(j)}
                        className="w-full min-w-0 h-7 flex items-center gap-1.5 px-1.5 text-left hover:bg-panel2/50 transition-colors focus:outline-none"
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
                        <div className="px-1.5 pb-1.5 pt-1 border-t border-edge/35 space-y-1.5 min-w-0 max-h-48 overflow-y-auto overflow-x-hidden">
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
              beginSessionRename(contextMenu.sessionId, contextMenu.title);
              setContextMenu(null);
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            Rename
          </button>
          <div className="border-t border-edge my-1" />
          <button
            onClick={() => {
              void handleCopyTranscriptId(contextMenu.sessionId);
              setContextMenu(null);
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            Copy transcript ID
          </button>
          <button
            onClick={() => {
              void handleExport(contextMenu.sessionId, "md");
              setContextMenu(null);
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            Export as Markdown
          </button>
          <button
            onClick={() => {
              void handleExport(contextMenu.sessionId, "json");
              setContextMenu(null);
            }}
            className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
          >
            Export as JSON
          </button>
          {contextMenu.canSettle ? (
            <>
              <div className="border-t border-edge my-1" />
              <button
                onClick={async () => {
                  await settleSession(contextMenu.sessionId, !contextMenu.settled);
                  setContextMenu(null);
                }}
                className="w-full text-left px-3 py-1.5 hover:bg-panel2 text-txt transition-colors"
              >
                {contextMenu.settled ? "Unsettle" : "Settle"}
              </button>
            </>
          ) : null}
          <div className="border-t border-edge my-1" />
          <button
            onClick={async () => {
              await archiveSession(contextMenu.sessionId, !contextMenu.archived);
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

const SESSION_JOBS_COLLAPSED_KEY = "pmharness.leftRail.sessionJobsCollapsed";
const SETTLED_EXPANDED_KEY = "pmharness.leftRail.settledExpanded";
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
  // Full resolved model id when present (pinned Muse runs, etc.).
  if (j.model) bits.push(j.model);
  if (j.adapter && j.adapter !== j.model) bits.push(j.adapter);
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
