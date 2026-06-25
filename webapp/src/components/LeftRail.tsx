import { useEffect, useState } from "react";
import { GitBranch, Plus, MessageSquare, Boxes, Check, Loader2, ChevronDown, ChevronRight } from "lucide-react";
import { api, type Workspace, type Session, type Job } from "../lib/api";

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

  const [collapsedWorkspaces, setCollapsedWorkspaces] = useState<Record<string, boolean>>({});
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renamingTitle, setRenamingTitle] = useState("");

  const toggleWorkspaceCollapse = (repo: string) => {
    setCollapsedWorkspaces((prev) => ({
      ...prev,
      [repo]: !prev[repo],
    }));
  };

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

  const [openPath, setOpenPath] = useState("");
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

  const handleOpenFolder = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!openPath.trim()) return;
    setOpening(true);
    try {
      const res = await api.openWorkspace(openPath);
      if (res.ok) {
        setOpenPath("");
        fetchWorkspace();
        await loadWs();
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

  // Group active sessions by workspace (repo path)
  const workspaceGroups: Record<string, { repo: string; branch: string; sessions: Session[]; lastCreated: number }> = {};
  
  activeSessions.forEach((s) => {
    const r = s.repo || "";
    const b = s.branch || "";
    if (!workspaceGroups[r]) {
      workspaceGroups[r] = {
        repo: r,
        branch: b,
        sessions: [],
        lastCreated: 0,
      };
    }
    workspaceGroups[r].sessions.push(s);
    if (s.created > workspaceGroups[r].lastCreated) {
      workspaceGroups[r].lastCreated = s.created;
    }
    // preserve branch info if active
    if (s.active && b) {
      workspaceGroups[r].branch = b;
    }
  });

  // Sort sessions within each group by created descending
  Object.values(workspaceGroups).forEach((g) => {
    g.sessions.sort((a, b) => b.created - a.created);
  });

  // Sort workspace groups by currently open repo first, then lastCreated descending
  const sortedWorkspaceGroups = Object.values(workspaceGroups).sort((a, b) => {
    const isCurrentA = workspaceInfo?.repo && a.repo === workspaceInfo.repo;
    const isCurrentB = workspaceInfo?.repo && b.repo === workspaceInfo.repo;
    if (isCurrentA && !isCurrentB) return -1;
    if (!isCurrentA && isCurrentB) return 1;
    return b.lastCreated - a.lastCreated;
  });

  return (
    <aside className="bg-panel border-r border-edge flex flex-col h-full overflow-hidden">
      <div className="flex items-center gap-2 px-4 border-b border-edge"
        style={{ paddingTop: 34, paddingBottom: 12, WebkitAppRegion: "drag" } as React.CSSProperties}>
        <span className="bg-accent/15 text-accent font-bold px-1.5 py-0.5 rounded-md text-[11px] tracking-tight">PM</span>
        <span className="font-medium text-[13px] text-txt/90">Puppetmaster</span>
      </div>

      {/* OPEN WORKSPACE FOLDER */}
      <div className="px-4 py-2.5 border-b border-edge bg-panel2/10">
        <div className="text-[10px] uppercase tracking-wider text-faint font-semibold mb-1.5">Opened Folder</div>
        {workspaceInfo?.repo ? (
          <div className="text-[11px] font-mono break-all text-muted bg-bg p-2 rounded border border-edge/40 mb-2">
            {workspaceInfo.repo}
            {workspaceInfo.codegraph_status && (
              <div className="text-[9px] mt-1 text-faint flex items-center gap-1">
                CodeGraph: <span className={workspaceInfo.codegraph_status === "ready" ? "text-good font-semibold" : workspaceInfo.codegraph_status === "indexing" ? "text-warn animate-pulse font-semibold" : "text-faint font-semibold"}>{workspaceInfo.codegraph_status}</span>
              </div>
            )}
          </div>
        ) : (
          <div className="text-[11px] text-faint italic mb-2">No folder open</div>
        )}
        <form onSubmit={handleOpenFolder} className="flex gap-1.5">
          <input
            type="text"
            placeholder="Absolute path to folder..."
            value={openPath}
            onChange={(e) => setOpenPath(e.target.value)}
            className="flex-1 min-w-0 bg-bg border border-edge rounded px-1.5 py-1 text-[11px] text-txt focus:outline-none focus:border-accent"
          />
          <button
            type="submit"
            disabled={opening}
            className="bg-accent/15 hover:bg-accent/25 text-accent text-[11px] font-semibold px-2 py-1 rounded transition disabled:opacity-50 shrink-0"
          >
            {opening ? "Opening..." : "Open"}
          </button>
        </form>
      </div>

      {/* WORKSPACES */}
      <Section title="Workspaces" action={<IconBtn onClick={newWs}><Plus size={13} /></IconBtn>}>
        {workspaces.length === 0 && <Empty>No workspaces</Empty>}
        {workspaces.map((w) => (
          <button key={w.name} onClick={() => switchWs(w.name)}
            className={`w-full text-left rounded px-2 py-1.5 mb-0.5 flex items-center gap-2 text-[13px] transition
              ${w.active ? "bg-accent2/40 text-txt" : "hover:bg-panel2 text-muted"}`}>
            {swapping === w.name ? <Loader2 size={12} className="animate-spin" /> : <GitBranch size={12} />}
            <span className="flex-1 truncate">{w.name}</span>
            {w.dirty && <span className="w-1.5 h-1.5 rounded-full bg-warn" title="uncommitted changes" />}
            {w.active && <Check size={12} className="text-accent" />}
          </button>
        ))}
      </Section>

      {/* SESSIONS */}
      <Section title="Sessions" action={<IconBtn onClick={newSession}><Plus size={13} /></IconBtn>}>
        {sessions.length === 0 && <Empty>No sessions</Empty>}
        {sortedWorkspaceGroups.map((g) => {
          const isCollapsed = !!collapsedWorkspaces[g.repo];
          const basename = getWorkspaceBasename(g.repo) || "No workspace";
          const isCurrentOpen = workspaceInfo?.repo && g.repo === workspaceInfo.repo;
          
          return (
            <div key={g.repo} className="mb-2">
              <button
                onClick={() => toggleWorkspaceCollapse(g.repo)}
                className="w-full flex items-center justify-between px-2 py-1 text-[11px] font-semibold text-muted hover:text-txt transition-colors"
              >
                <div className="flex items-center gap-1.5 truncate">
                  {isCollapsed ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
                  <span className={`truncate ${isCurrentOpen ? "text-accent font-bold" : ""}`}>
                    {basename}
                  </span>
                  {g.branch && (
                    <span className="bg-panel2 px-1 py-0.5 rounded text-[9px] font-mono font-normal text-faint max-w-[80px] truncate">
                      {g.branch}
                    </span>
                  )}
                </div>
              </button>
              
              {!isCollapsed && (
                <div className="pl-3 mt-1 border-l border-edge/30 space-y-0.5">
                  {g.sessions.map((s) => (
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
                          className="w-full bg-bg border border-accent rounded px-2 py-1 text-[13px] text-txt focus:outline-none"
                        />
                      ) : (
                        <button onClick={() => switchSession(s.id)}
                          onDoubleClick={() => {
                            setRenamingId(s.id);
                            setRenamingTitle(s.title || "Untitled");
                          }}
                          onContextMenu={(e) => handleContextMenu(e, s)}
                          className={`w-full text-left rounded px-2 py-1.5 flex items-center gap-2 text-[13px] transition
                            ${s.active ? "bg-accent2/40 text-txt font-semibold" : "hover:bg-panel2 text-muted"}`}>
                          <MessageSquare size={12} />
                          <span className="flex-1 truncate">{s.title || "Untitled"}</span>
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}

        {archivedSessions.length > 0 && (
          <div className="mt-2">
            <button
              onClick={() => setArchivedExpanded(!archivedExpanded)}
              className="w-full text-left px-2 py-1 text-[10px] uppercase tracking-wider text-faint font-medium hover:text-muted flex items-center justify-between"
            >
              <span>Archived ({archivedSessions.length})</span>
              {archivedExpanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
            </button>
            {archivedExpanded && (
              <div className="mt-1 pl-1 border-l border-edge">
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
                        className="w-full bg-bg border border-accent rounded px-2 py-1 text-[13px] text-txt focus:outline-none"
                      />
                    ) : (
                      <button onClick={() => switchSession(s.id)}
                        onDoubleClick={() => {
                          setRenamingId(s.id);
                          setRenamingTitle(s.title || "Untitled");
                        }}
                        onContextMenu={(e) => handleContextMenu(e, s)}
                        className={`w-full text-left rounded px-2 py-1.5 mb-0.5 flex items-center gap-2 text-[13px] transition opacity-60 hover:opacity-100
                          ${s.active ? "bg-accent2/40 text-txt font-semibold" : "hover:bg-panel2 text-muted"}`}>
                        <MessageSquare size={12} />
                        <span className="flex-1 truncate">{s.title || "Untitled"}</span>
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </Section>

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
    <div className={`px-2 pt-3 ${grow ? "flex-1 overflow-y-auto" : ""}`}>
      <div className="flex items-center justify-between px-2 mb-1 mt-1">
        <span className="text-[10px] uppercase tracking-wider text-faint font-medium">{title}</span>
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
