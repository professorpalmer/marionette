import { useEffect, useState } from "react";
import { GitBranch, Plus, MessageSquare, Boxes, Check, Loader2 } from "lucide-react";
import { api, type Workspace, type Session, type Job } from "../lib/api";

export default function LeftRail({ jobsRefresh, onSessionChange }: {
  jobsRefresh: number;
  onSessionChange?: (id: string) => void;
}) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [swapping, setSwapping] = useState<string | null>(null);

  const loadWs = () => api.workspaces().then(setWorkspaces).catch(() => {});
  const loadSess = () => api.sessions().then((sess) => {
    setSessions(sess);
    const active = sess.find((s) => s.active);
    if (active) {
      onSessionChange?.(active.id);
    }
  }).catch(() => {});
  useEffect(() => { loadWs(); loadSess(); }, []);
  useEffect(() => { api.jobs().then(setJobs).catch(() => {}); }, [jobsRefresh]);

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
        {sessions.map((s) => (
          <div key={s.id} className="group relative">
            <button onClick={() => switchSession(s.id)}
              className={`w-full text-left rounded px-2 py-1.5 mb-0.5 flex items-center gap-2 text-[13px] transition
                ${s.active ? "bg-accent2/40 text-txt" : "hover:bg-panel2 text-muted"}`}>
              <MessageSquare size={12} />
              <span className="flex-1 truncate mr-12">{s.title || "Untitled"}</span>
            </button>
            <div className={`absolute right-1 top-1.5 hidden group-hover:flex items-center gap-1 bg-panel border border-edge rounded px-1 py-0.5 z-10
              ${s.active ? "!flex" : ""}`}>
              <button onClick={(e) => { e.stopPropagation(); handleExport(s.id, "md"); }}
                className="text-[9px] font-bold text-muted hover:text-accent uppercase px-1">
                md
              </button>
              <span className="text-[9px] text-edge">|</span>
              <button onClick={(e) => { e.stopPropagation(); handleExport(s.id, "json"); }}
                className="text-[9px] font-bold text-muted hover:text-accent uppercase px-1">
                json
              </button>
            </div>
          </div>
        ))}
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
