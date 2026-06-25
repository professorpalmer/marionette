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
  useEffect(() => { loadWs(); loadSess(); }, []);
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

  return (
    <aside className="bg-panel border-r border-edge flex flex-col h-full overflow-hidden">
      <div className="flex items-center gap-2 px-4 border-b border-edge"
        style={{ paddingTop: 34, paddingBottom: 12, WebkitAppRegion: "drag" } as React.CSSProperties}>
        <span className="bg-accent/15 text-accent font-bold px-1.5 py-0.5 rounded-md text-[11px] tracking-tight">PM</span>
        <span className="font-medium text-[13px] text-txt/90">Puppetmaster</span>
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
        {activeSessions.map((s) => (
          <div key={s.id} className="group relative">
            <button onClick={() => switchSession(s.id)}
              onContextMenu={(e) => handleContextMenu(e, s)}
              className={`w-full text-left rounded px-2 py-1.5 mb-0.5 flex items-center gap-2 text-[13px] transition
                ${s.active ? "bg-accent2/40 text-txt font-semibold" : "hover:bg-panel2 text-muted"}`}>
              <MessageSquare size={12} />
              <span className="flex-1 truncate">{s.title || "Untitled"}</span>
            </button>
          </div>
        ))}

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
                    <button onClick={() => switchSession(s.id)}
                      onContextMenu={(e) => handleContextMenu(e, s)}
                      className={`w-full text-left rounded px-2 py-1.5 mb-0.5 flex items-center gap-2 text-[13px] transition opacity-60 hover:opacity-100
                        ${s.active ? "bg-accent2/40 text-txt font-semibold" : "hover:bg-panel2 text-muted"}`}>
                      <MessageSquare size={12} />
                      <span className="flex-1 truncate">{s.title || "Untitled"}</span>
                    </button>
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
