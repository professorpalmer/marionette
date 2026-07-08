import { useEffect, useState } from "react";
import { GitBranch, Plus, MessageSquare, Check, Loader2, ChevronDown, ChevronRight, SquarePen, Folder, FolderGit2, CheckCircle2, Circle, XCircle, Trash2 } from "lucide-react";
import { api, type Workspace, type WorkspaceInfo, type Session, type Job, type Artifact } from "../lib/api";
import { pickFolder } from "../lib/transport";

export default function LeftRail({ jobsRefresh, onSessionChange }: {
  jobsRefresh: number;
  onSessionChange?: (id: string) => void;
}) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [swapping, setSwapping] = useState<string | null>(null);
  const [contextMenu, setContextMenu] = useState<{
    x: number;
    y: number;
    sessionId: string;
    archived: boolean;
  } | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [confirmClearSessions, setConfirmClearSessions] = useState(false);
  const [projectContextMenu, setProjectContextMenu] = useState<{
    x: number;
    y: number;
    projectPath: string;
  } | null>(null);
  const [confirmForgetPath, setConfirmForgetPath] = useState<string | null>(null);
  const [archivedExpanded, setArchivedExpanded] = useState(false);
  const [expandedJobs, setExpandedJobs] = useState<Record<string, boolean>>({});
  // /api/jobs only carries an artifact COUNT per job; the full artifact list is
  // fetched lazily the first time a card is expanded and cached here.
  const [artifactsByJob, setArtifactsByJob] = useState<Record<string, Artifact[]>>({});

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
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renamingTitle, setRenamingTitle] = useState("");

  const getWorkspaceBasename = (repoPath: string) => {
    if (!repoPath) return "";
    const parts = repoPath.split(/[/\\]/);
    return parts[parts.length - 1] || repoPath;
  };

  const handleRenameSubmit = async (id: string) => {
    if (!renamingTitle.trim()) {
      setRenamingId(null);
      return;
    }
    try {
      await api.renameSession(id, renamingTitle.trim());
      await loadSess();
    } catch (err) {
      console.error(err);
    } finally {
      setRenamingId(null);
    }
  };

  const [opening, setOpening] = useState(false);
  const [workspaceInfo, setWorkspaceInfo] = useState<WorkspaceInfo | null>(null);

  const fetchWorkspace = () =>
    api.getWorkspace().then(setWorkspaceInfo).catch(() => {});

  const handleForgetProject = async (path: string) => {
    const previous = workspaceInfo;
    setWorkspaceInfo((prev) => {
      if (!prev) return prev;
      return { ...prev, recents: (prev.recents || []).filter((r) => r !== path) };
    });
    setExpandedProjects((prev) => {
      const next = { ...prev };
      delete next[path];
      return next;
    });
    try {
      const res = await api.forgetWorkspace(path);
      setWorkspaceInfo((prev) => (prev ? { ...prev, recents: res.recents } : prev));
    } catch (err) {
      console.error(err);
      if (previous) setWorkspaceInfo(previous);
      else await fetchWorkspace();
    }
  };

  const loadWs = () => api.workspaces().then(setWorkspaces).catch(() => {});
  const loadSess = () => api.sessions().then((sess) => {
    setSessions(sess);
    const active = sess.find((s) => s.active);
    if (active) {
      onSessionChange?.(active.id);
    } else {
      onSessionChange?.("");
    }
  }).catch(() => {});
  useEffect(() => {
    loadWs();
    loadSess();
    fetchWorkspace();
    const handleConfigChanged = () => {
      loadWs();
      loadSess();
      fetchWorkspace();
    };
    window.addEventListener("harness-config-changed", handleConfigChanged);
    return () => {
      window.removeEventListener("harness-config-changed", handleConfigChanged);
    };
  }, []);

  // Poll workspace status while CodeGraph indexes so the badge flips to READY
  // without opening a session or switching directories.
  useEffect(() => {
    if (workspaceInfo?.codegraph_status !== "indexing") return;
    const poll = () => { fetchWorkspace(); };
    poll();
    const timer = setInterval(poll, 4000);
    return () => clearInterval(timer);
  }, [workspaceInfo?.codegraph_status]);

  useEffect(() => {
    if (workspaceInfo?.codegraph_status !== "indexing") return;
    const onFocus = () => { fetchWorkspace(); };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [workspaceInfo?.codegraph_status]);

  const handleOpenProject = async (path: string) => {
    setOpening(true);
    try {
      const res = await api.openWorkspace(path);
      if (res.ok) {
        setWorkspaceInfo((prev): WorkspaceInfo => ({
          repo: res.repo,
          branch: res.branch,
          is_git: res.is_git,
          codegraph_status: res.codegraph,
          recents: prev?.recents,
        }));
        await fetchWorkspace();
        await loadWs();
        await loadSess();
        window.dispatchEvent(new Event("harness-config-changed"));
      } else {
        alert("Failed to open directory: " + (res as any).error);
      }
    } catch (err: any) {
      alert("Error opening directory: " + (err?.error || err?.message || err));
    } finally {
      setOpening(false);
    }
  };

  const handleOpenFolder = async () => {
    const picked = await pickFolder();
    if (!picked) return;
    await handleOpenProject(picked);
  };
  useEffect(() => { api.jobs().then(setJobs).catch(() => {}); }, [jobsRefresh]);

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

  const switchWs = async (name: string) => {
    setSwapping(name);
    try { await api.switchWorkspace(name); await loadWs(); } finally { setSwapping(null); }
  };
  const newWs = async () => {
    const name = prompt("New workspace name (creates a git branch):");
    if (!name) return;
    await api.createWorkspace(name); await loadWs();
  };
  const switchSession = async (id: string) => {
    await api.switchSession(id);
    await loadSess();
    // Session switch can repoint the active repo (and thus the codegraph) on the
    // backend. Fire the same event the dir-open path uses so the codegraph/state
    // panel refetches -- without this, clicking a session leaves the old graph
    // shown even though the backend already swapped repos.
    window.dispatchEvent(new Event("harness-config-changed"));
  };
  const newSession = async () => { await api.createSession(); await loadSess(); };
  useEffect(() => {
    const onNew = () => { newSession(); };
    window.addEventListener("harness-new-session", onNew);
    return () => window.removeEventListener("harness-new-session", onNew);
  }, []);
  const handleDeleteSession = async (id: string) => {
    const res = await api.deleteSession(id);
    await loadSess();
    if (res.active) {
      await switchSession(res.active);
    }
  };

  const handleClearSessions = async () => {
    const res = await api.clearSessions();
    await loadSess();
    if (res.active) {
      await switchSession(res.active);
    }
    setConfirmClearSessions(false);
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
    });
  };

  const activeSessions = sessions.filter((s) => !s.archived);
  const archivedSessions = sessions.filter((s) => s.archived);

  const currentRepo = workspaceInfo?.repo || "";
  const rawRecents = workspaceInfo?.recents || [];
  const projects = Array.from(new Set([currentRepo, ...rawRecents])).filter(Boolean);

  const handleProjectContextMenu = (e: React.MouseEvent, path: string) => {
    e.preventDefault();
    setProjectContextMenu({
      x: e.clientX,
      y: e.clientY,
      projectPath: path,
    });
  };

  const handleProjectRowClick = (projectPath: string, isActive: boolean, isExpanded: boolean) => {
    if (isActive) {
      setExpandedProjects(prev => ({
        ...prev,
        [projectPath]: !isExpanded
      }));
    } else {
      handleOpenProject(projectPath);
    }
  };

  return (
    <aside className="bg-panel border-r border-edge flex flex-col h-full overflow-hidden">
      {/* Slim draggable bar to clear the macOS traffic lights; no product label
          (the title bar already names the app, like Cursor/Hermes). */}
      <div style={{ height: 30, WebkitAppRegion: "drag" } as React.CSSProperties} />
      
      <div className="px-3 pb-2 border-b border-edge flex flex-col gap-1.5">
        <button
          onClick={newSession}
          className="w-full flex items-center gap-2 px-2.5 py-2 rounded-md text-[13px] font-medium text-txt bg-panel2/60 hover:bg-panel2 border border-edge/60 transition">
          <SquarePen size={14} className="text-accent" />
          New session
        </button>
        <button
          onClick={handleOpenFolder}
          disabled={opening}
          className="w-full text-center text-accent text-[11px] font-semibold py-1 hover:bg-accent/10 rounded transition disabled:opacity-50"
        >
          {opening ? "Opening..." : "Open Folder..."}
        </button>
      </div>

      {/* PROJECTS SECTION */}
      <Section title="Projects">
        {projects.length === 0 && <Empty>No projects</Empty>}
        <div className="space-y-1">
          {projects.map((projectPath) => {
            const basename = getWorkspaceBasename(projectPath) || "Untitled Project";
            const isCurrentActive = !!(workspaceInfo?.repo && projectPath === workspaceInfo.repo);
            const isExpanded = expandedProjects[projectPath] !== undefined 
              ? expandedProjects[projectPath] 
              : isCurrentActive;
            
            const projectSessions = activeSessions.filter((s) => s.repo === projectPath);
            projectSessions.sort((a, b) => b.created - a.created);
            const count = projectSessions.length;
            
            return (
              <div key={projectPath} className={`rounded transition ${isCurrentActive ? "bg-panel2/50 border-l-2 border-accent" : "hover:bg-panel2/20"}`}>
                {/* Project Row */}
                <div
                  onClick={() => handleProjectRowClick(projectPath, isCurrentActive, isExpanded)}
                  onContextMenu={(e) => handleProjectContextMenu(e, projectPath)}
                  className="flex items-center gap-1.5 px-2 py-1.5 cursor-pointer select-none group"
                  title={projectPath}
                >
                  {/* Expand Chevron */}
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
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
                  <span className={`text-[12.5px] truncate font-medium flex-1 ${isCurrentActive ? "text-txt font-semibold" : "text-muted hover:text-txt"}`}>
                    {basename}
                  </span>

                  {/* CodeGraph status (inline compact) */}
                  {isCurrentActive && workspaceInfo?.codegraph_status && (
                    <span className={`text-[9px] font-semibold uppercase px-1 rounded shrink-0 ${
                      workspaceInfo.codegraph_status === "ready" 
                        ? "text-good bg-good/10" 
                        : workspaceInfo.codegraph_status === "indexing" 
                          ? "text-warn bg-warn/10 animate-pulse" 
                          : "text-faint bg-panel2"
                    }`}>
                      {workspaceInfo.codegraph_status}
                    </span>
                  )}

                  {/* Session Count Badge */}
                  {count > 0 && (
                    <span className="text-[10px] text-faint px-1.5 py-0.2 rounded bg-panel2 font-mono shrink-0">
                      {count}
                    </span>
                  )}
                </div>

                {/* Sessions (Expandable inline) */}
                {isExpanded && (
                  <div className="pl-4 pr-1 pb-1.5 space-y-0.5 border-l border-edge/30 ml-3.5 mt-0.5">
                    {isCurrentActive && projectSessions.length > 0 && (
                      <div className="px-1 pb-1 flex justify-end">
                        {confirmClearSessions ? (
                          <div className="flex items-center gap-2 text-[10px]">
                            <span className="text-muted">Clear all?</span>
                            <button
                              onClick={handleClearSessions}
                              className="text-red-400 font-semibold hover:underline"
                            >
                              Yes
                            </button>
                            <button
                              onClick={() => setConfirmClearSessions(false)}
                              className="text-muted hover:underline"
                            >
                              No
                            </button>
                          </div>
                        ) : (
                          <button
                            onClick={() => setConfirmClearSessions(true)}
                            className="text-[10px] text-faint hover:text-red-400 transition-colors"
                          >
                            Clear sessions
                          </button>
                        )}
                      </div>
                    )}
                    {projectSessions.length === 0 ? (
                      <div className="text-[11px] text-faint italic px-2 py-1">No sessions</div>
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
                            <div className="flex items-center gap-0.5">
                              <button onClick={() => switchSession(s.id)}
                                onDoubleClick={() => {
                                  setRenamingId(s.id);
                                  setRenamingTitle(s.title || "Untitled");
                                }}
                                onContextMenu={(e) => handleContextMenu(e, s)}
                                className={`flex-1 text-left rounded px-1.5 py-1 flex items-center gap-1.5 text-[12.5px] transition
                                  ${s.active ? "bg-accent/10 text-accent font-semibold" : "hover:bg-panel2/60 text-muted hover:text-txt"}`}>
                                <MessageSquare size={11} className={s.active ? "text-accent" : "text-faint"} />
                                <span className="flex-1 truncate">{s.title || "Untitled"}</span>
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

      {/* BRANCH SWITCHING / WORKSPACES */}
      {workspaceInfo?.is_git && (
        <Section title="Branches" action={<IconBtn onClick={newWs}><Plus size={13} /></IconBtn>}>
          {workspaces.length === 0 && <Empty>No branches</Empty>}
          <div className="space-y-0.5 max-h-[140px] overflow-y-auto">
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
        </Section>
      )}

      {/* ARCHIVED SESSIONS */}
      {archivedSessions.length > 0 && (
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

      {/* JOBS -- clean task-list styling (mirrors the composer TaskStack): a
          slim status row per job, click to expand a card with richer detail
          (adapter/role, tokens/cost, artifact headlines) instead of a lone
          line of truncated text. */}
      <Section title="Session Jobs" grow>
        {jobs.length === 0 && <Empty>No jobs yet</Empty>}
        {jobs.slice().reverse().map((j) => {
          const st = jobStatus(j);
          const isOpen = !!expandedJobs[j.id];
          const detail = jobDetailBits(j);
          const loadedArts = artifactsByJob[j.id];
          const arts = (loadedArts || []).filter((a) => a && a.headline);
          const diff = jobDiffstat(loadedArts || []);
          return (
            <div key={j.id} className="rounded mb-0.5 bg-panel2 border border-edge overflow-hidden">
              <button
                onClick={() => toggleJobCard(j)}
                className="w-full flex items-center gap-1.5 px-2 py-1.5 text-left hover:bg-panel2/60 transition-colors focus:outline-none"
              >
                <JobStatusIcon status={st} />
                <span
                  className={`flex-1 truncate text-[12px] ${st === "completed" ? "text-muted" : st === "cancelled" ? "text-red-400/90" : "text-txt"}`}
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
                <div className="px-2 pb-1.5 pt-0.5 border-t border-edge/50 space-y-1">
                  {detail.length > 0 && (
                    <div className="flex flex-wrap gap-x-2 gap-y-0.5 text-[10px] text-faint">
                      {detail.map((d, i) => (
                        <span key={i} className="tabular-nums">{d}</span>
                      ))}
                    </div>
                  )}
                  {diff && (
                    <div className="flex items-center gap-2 text-[10px] tabular-nums text-faint">
                      <span>{diff.files} file{diff.files === 1 ? "" : "s"} changed</span>
                      {diff.insertions > 0 && <span className="text-good">+{diff.insertions}</span>}
                      {diff.deletions > 0 && <span className="text-red-400/90">-{diff.deletions}</span>}
                    </div>
                  )}
                  {arts.length > 0 ? (
                    <div className="space-y-0.5">
                      {arts.slice(0, 4).map((a, i) => (
                        <div key={a.id || i} className="text-[11px] text-txt/90 flex items-start gap-1.5 leading-snug">
                          <span className="text-good mt-[3px] shrink-0">·</span>
                          <span className="flex-1">{a.headline}</span>
                        </div>
                      ))}
                      {arts.length > 4 && (
                        <div className="text-[10px] text-faint pl-3">+{arts.length - 4} more</div>
                      )}
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
      </Section>

      {/* CONTEXT MENU */}
      {contextMenu && (
        <div
          className="fixed z-50 bg-panel border border-edge rounded shadow-lg text-[12px] py-1 min-w-[150px]"
          style={{ top: contextMenu.y, left: contextMenu.x }}
          onClick={(e) => e.stopPropagation()}
        >
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
              await loadSess();
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

function jobStatus(j: Job): JobStatus {
  const s = (j.status || "").toLowerCase();
  if (s.includes("complete") || s.includes("done")) return "completed";
  if (s.includes("fail") || s.includes("cancel") || s.includes("error")) return "cancelled";
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

function Section({ title, action, children, grow }: any) {
  return (
    <div className={`px-2 pt-4 ${grow ? "flex-1 overflow-y-auto" : ""}`}>
      <div className="flex items-center justify-between px-2 mb-2 mt-0.5">
        <span className="text-[11px] uppercase tracking-wider text-muted font-semibold">{title}</span>
        {action}
      </div>
      {children}
    </div>
  );
}
const IconBtn = ({ onClick, children }: any) => (
  <button onClick={onClick} className="text-muted hover:text-txt p-0.5 rounded hover:bg-panel2">{children}</button>
);
const Empty = ({ children }: any) => <div className="text-[11px] text-muted italic px-1 py-1">{children}</div>;
