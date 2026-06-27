import { useEffect, useState } from "react";
import { GitBranch, Plus, MessageSquare, Boxes, Check, Loader2, ChevronDown, ChevronRight, SquarePen, Folder, FolderGit2 } from "lucide-react";
import { api, type Workspace, type Session, type Job } from "../lib/api";
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
  const [archivedExpanded, setArchivedExpanded] = useState(false);

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
  const [workspaceInfo, setWorkspaceInfo] = useState<any>(null);

  const fetchWorkspace = () => {
    api.getWorkspace().then(setWorkspaceInfo).catch(() => {});
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
  useEffect(() => { loadWs(); loadSess(); fetchWorkspace(); }, []);

  const handleOpenProject = async (path: string) => {
    setOpening(true);
    try {
      const res = await api.openWorkspace(path);
      if (res.ok) {
        fetchWorkspace();
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
  };
  const newSession = async () => { await api.createSession(); await loadSess(); };
  useEffect(() => {
    const onNew = () => { newSession(); };
    window.addEventListener("harness-new-session", onNew);
    return () => window.removeEventListener("harness-new-session", onNew);
  }, []);
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
            const isCurrentActive = workspaceInfo?.repo && projectPath === workspaceInfo.repo;
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
                            <button onClick={() => switchSession(s.id)}
                              onDoubleClick={() => {
                                setRenamingId(s.id);
                                setRenamingTitle(s.title || "Untitled");
                              }}
                              onContextMenu={(e) => handleContextMenu(e, s)}
                              className={`w-full text-left rounded px-1.5 py-1 flex items-center gap-1.5 text-[12.5px] transition
                                ${s.active ? "bg-accent/10 text-accent font-semibold" : "hover:bg-panel2/60 text-muted hover:text-txt"}`}>
                              <MessageSquare size={11} className={s.active ? "text-accent" : "text-faint"} />
                              <span className="flex-1 truncate">{s.title || "Untitled"}</span>
                            </button>
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

      {/* JOBS */}
      <Section title="Session Jobs" grow>
        {jobs.length === 0 && <Empty>No jobs yet</Empty>}
        {jobs.slice().reverse().map((j) => (
          <div key={j.id} className="rounded px-2 py-1.5 mb-0.5 bg-panel2 border border-edge">
            <div className="text-[12px] truncate flex items-center gap-1.5"><Boxes size={11} className="text-muted" />{j.goal}</div>
            <div className="text-[10px] text-muted mt-0.5">{(j.status || "").split(".").pop()}</div>
          </div>
        ))}
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
                    const res = await api.deleteSession(contextMenu.sessionId);
                    await loadSess();
                    if (res.active) {
                      await switchSession(res.active);
                    }
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
    </aside>
  );
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
