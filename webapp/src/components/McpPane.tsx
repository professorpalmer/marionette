import { useEffect, useRef, useState } from "react";
import { Plug, Play, Square, Trash2, Plus, Check, X, ChevronDown, ChevronRight } from "lucide-react";
import { api } from "../lib/api";

const MCP_TOOLS_COLLAPSED_KEY = "pmharness.mcpPane.toolsCollapsed";
const MCP_TOOLS_HEIGHT_KEY = "pmharness.mcpPane.toolsHeight.v1";
const MCP_TOOLS_MIN_HEIGHT = 96;
const MCP_TOOLS_DEFAULT_HEIGHT = 176; // ~max-h-44 legacy

function loadMcpToolsCollapsed(): boolean {
  try {
    return localStorage.getItem(MCP_TOOLS_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function loadMcpToolsHeight(): number {
  const fallback = MCP_TOOLS_DEFAULT_HEIGHT;
  try {
    const raw = localStorage.getItem(MCP_TOOLS_HEIGHT_KEY);
    if (!raw) return fallback;
    const n = Number.parseInt(raw, 10);
    if (!Number.isFinite(n) || n <= 0) return fallback;
    const conservativeMax =
      typeof window === "undefined"
        ? n
        : Math.max(MCP_TOOLS_MIN_HEIGHT, Math.round(window.innerHeight * 0.5));
    return Math.min(conservativeMax, Math.max(MCP_TOOLS_MIN_HEIGHT, n));
  } catch {
    return fallback;
  }
}

function saveMcpToolsHeight(height: number): void {
  try {
    localStorage.setItem(MCP_TOOLS_HEIGHT_KEY, String(Math.round(height)));
  } catch {
    // localStorage full/unavailable -- height still works for this session.
  }
}

// MCP server manager: add/start/stop/remove MCP servers and see their tools.
// Embedded mode lives under State (CodeGraph / Wiki) so the right rail does not
// need a separate MCP tab.
export default function McpPane({ embedded = false }: { embedded?: boolean }) {
  const [servers, setServers] = useState<any[]>([]);
  const [tools, setTools] = useState<any[]>([]);
  const [catalog, setCatalog] = useState<Record<string, any>>({});
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState("");
  const [toolsCollapsed, setToolsCollapsed] = useState(loadMcpToolsCollapsed);
  const [toolsHeight, setToolsHeight] = useState(loadMcpToolsHeight);

  const paneRef = useRef<HTMLDivElement>(null);
  const headerRef = useRef<HTMLDivElement>(null);
  const serversRef = useRef<HTMLDivElement>(null);
  const toolsHeightRef = useRef(toolsHeight);
  const resizeDragRef = useRef<{ startY: number; startH: number } | null>(null);
  toolsHeightRef.current = toolsHeight;

  const refresh = () => api.mcp().then((d) => { setServers(d.servers); setTools(d.tools); }).catch(() => {});
  useEffect(() => {
    refresh();
    api.mcpCatalog().then((d) => setCatalog(d.catalog)).catch(() => {});
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, []);

  const getMaxToolsHeight = () => {
    const pane = paneRef.current;
    const header = headerRef.current;
    const servers = serversRef.current;
    if (!pane || !header || !servers) return MCP_TOOLS_MIN_HEIGHT;
    // Sum servers children for natural content height (same trick as LeftRail
    // Session Jobs -- scroll containers report scrollHeight >= rendered height).
    const serversContent = Array.from(servers.children).reduce(
      (sum, el) => sum + (el as HTMLElement).offsetHeight,
      0,
    );
    // Keep a thin strip of the servers list visible when tools is dragged tall.
    const serversReserve = Math.min(80, Math.max(40, serversContent));
    const available = pane.clientHeight - header.offsetHeight - serversReserve;
    return Math.max(MCP_TOOLS_MIN_HEIGHT, available);
  };

  const clampToolsHeight = (height: number) =>
    Math.min(getMaxToolsHeight(), Math.max(MCP_TOOLS_MIN_HEIGHT, height));

  useEffect(() => {
    if (toolsCollapsed || tools.length === 0) return;
    const clampToViewport = () => {
      setToolsHeight((h) => clampToolsHeight(h));
    };
    clampToViewport();
    window.addEventListener("resize", clampToViewport);
    return () => window.removeEventListener("resize", clampToViewport);
  }, [toolsCollapsed, tools.length, servers.length, adding]);

  const onToolsResizePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (toolsCollapsed) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    resizeDragRef.current = { startY: e.clientY, startH: toolsHeightRef.current };
  };

  const onToolsResizePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!resizeDragRef.current) return;
    // Drag up = taller tools panel (mirrors Session Jobs).
    const delta = resizeDragRef.current.startY - e.clientY;
    setToolsHeight(clampToolsHeight(resizeDragRef.current.startH + delta));
  };

  const finishToolsResize = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!resizeDragRef.current) return;
    resizeDragRef.current = null;
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    saveMcpToolsHeight(toolsHeightRef.current);
  };

  const toggleToolsCollapsed = () => {
    setToolsCollapsed((v) => {
      const next = !v;
      try {
        localStorage.setItem(MCP_TOOLS_COLLAPSED_KEY, next ? "1" : "0");
      } catch {
        // ignore
      }
      return next;
    });
  };

  const start = async (n: string) => { setBusy(n); try { await api.mcpStart(n); await refresh(); } finally { setBusy(""); } };
  const stop = async (n: string) => { setBusy(n); try { await api.mcpStop(n); await refresh(); } finally { setBusy(""); } };
  const remove = async (n: string) => { setBusy(n); try { await api.mcpRemove(n); await refresh(); } finally { setBusy(""); } };

  return (
    <div
      ref={paneRef}
      className={`flex flex-col text-[12px] ${embedded ? "h-full min-h-0" : "h-full"}`}
    >
      <div
        ref={headerRef}
        className={`flex items-center justify-between shrink-0 ${embedded ? "px-2 py-1" : "px-3 py-2 border-b border-edge"}`}
      >
        {!embedded ? (
          <span className="uppercase tracking-wider text-[10px] text-faint font-medium flex items-center gap-1.5">
            <Plug size={11} /> MCP Servers
          </span>
        ) : (
          <span className="text-[9px] text-faint">
            {servers.length === 0
              ? "No servers"
              : `${servers.filter((s) => s.running).length}/${servers.length} running`}
          </span>
        )}
        <button
          onClick={() => setAdding((v) => !v)}
          className="text-muted hover:text-txt"
          title={adding ? "Cancel add" : "Add MCP server"}
        >
          <Plus size={14} />
        </button>
      </div>

      <div
        ref={serversRef}
        className="flex-1 min-h-0 overflow-y-auto p-2 flex flex-col gap-1.5"
      >
        {servers.length === 0 && !adding && (
          <div className={`text-faint text-[11px] text-center px-3 leading-relaxed ${embedded ? "mt-2" : "mt-6"}`}>
            No MCP servers yet. Add github, aws, vercel, a browser controller, or a Docker HTTP URL
            (e.g. http://localhost:8085/mcp). Discord bot recipe (optional, not built-in):
            docs/discord-mcp.md in the Marionette repo.
          </div>
        )}

        {servers.map((s) => (
          <div key={s.name} className="border border-edge rounded-lg p-2 bg-panel2/40">
            <div className="flex items-center gap-2">
              <span className={`w-1.5 h-1.5 rounded-full ${s.running ? "bg-good" : "bg-faint"}`} />
              <span className="font-medium text-txt flex-1 truncate flex items-center gap-1.5">
                <span>{s.name}</span>
                {s.transport && (
                  <span className="px-1 py-0.5 rounded bg-panel border border-edge text-faint text-[8.5px] font-mono uppercase tracking-wider">
                    {s.transport}
                  </span>
                )}
              </span>
              <span className="text-faint text-[10px]">{s.running ? `${s.tools} tools` : "stopped"}</span>
              {s.running
                ? <button onClick={() => stop(s.name)} disabled={busy === s.name} title="Stop" className="text-muted hover:text-warn"><Square size={12} /></button>
                : <button onClick={() => start(s.name)} disabled={busy === s.name} title="Start" className="text-muted hover:text-good"><Play size={12} /></button>}
              <button onClick={() => remove(s.name)} disabled={busy === s.name} title="Remove" className="text-muted hover:text-risk"><Trash2 size={12} /></button>
            </div>
            <div className="text-faint text-[10px] mt-0.5 truncate font-mono">{s.command}</div>
            {s.error && <div className="text-risk text-[10px] mt-1 break-words">{s.error}</div>}
          </div>
        ))}

        {adding && <AddForm catalog={catalog} onDone={() => { setAdding(false); refresh(); }} />}
      </div>

      {tools.length > 0 && (
        <div
          className="border-t border-edge/40 shrink-0 min-w-0 flex flex-col"
          style={toolsCollapsed ? undefined : { height: toolsHeight }}
        >
          {!toolsCollapsed && (
            <div
              role="separator"
              aria-orientation="horizontal"
              aria-label="Resize available MCP tools panel"
              onPointerDown={onToolsResizePointerDown}
              onPointerMove={onToolsResizePointerMove}
              onPointerUp={finishToolsResize}
              onPointerCancel={finishToolsResize}
              className="h-1.5 -mt-1.5 mb-0.5 cursor-row-resize touch-none flex items-center justify-center group shrink-0"
            >
              <div className="w-8 h-0.5 rounded-full bg-edge/80 group-hover:bg-muted/80 transition-colors" />
            </div>
          )}
          <div
            className={`flex items-center justify-between px-2 mb-1 gap-2 min-w-0 shrink-0 ${
              toolsCollapsed ? "pt-2.5 mt-0.5" : "pt-1 mt-0"
            }`}
          >
            <button
              onClick={toggleToolsCollapsed}
              className="flex items-center gap-1 min-w-0 text-[11px] uppercase tracking-wider text-muted font-semibold hover:text-txt focus:outline-none"
            >
              {toolsCollapsed ? (
                <ChevronRight size={11} className="shrink-0" />
              ) : (
                <ChevronDown size={11} className="shrink-0" />
              )}
              <span className="truncate">Available tools</span>
              <span className="text-faint/70 normal-case tracking-normal shrink-0">
                ({tools.length})
              </span>
            </button>
          </div>
          {!toolsCollapsed && (
            <div className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden px-2 pb-1.5">
              {tools.map((t) => (
                <div
                  key={t.qualified}
                  className="py-1 border-b border-edge/20 last:border-none flex flex-wrap items-baseline gap-x-1.5 min-w-0"
                >
                  <span
                    className="text-accent font-mono text-[11px] truncate max-w-full"
                    title={t.qualified}
                  >
                    {t.qualified}
                  </span>
                  <span
                    className="text-faint text-[10px] truncate max-w-full"
                    title={t.description}
                  >
                    {t.description}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function AddForm({ catalog, onDone }: { catalog: Record<string, any>; onDone: () => void }) {
  const [name, setName] = useState("");
  const [command, setCommand] = useState("npx");
  const [argStr, setArgStr] = useState("");
  const [envStr, setEnvStr] = useState("");
  const [url, setUrl] = useState("");
  const [err, setErr] = useState("");

  const pickPreset = (key: string) => {
    const c = catalog[key];
    if (!c) return;
    setName(key); setCommand(c.command || ""); setArgStr((c.args || []).join(" "));
    setEnvStr((c.env_hint || []).map((k: string) => `${k}=`).join("\n"));
    setUrl("");
  };

  const submit = async () => {
    if (url.trim()) {
      const r = await api.mcpAdd(name.trim(), undefined, undefined, undefined, url.trim());
      if (r.ok) onDone(); else setErr(r.error || "failed to add");
    } else {
      const args = argStr.trim() ? argStr.trim().split(/\s+/) : [];
      const env: Record<string, string> = {};
      envStr.split("\n").forEach((l) => { const i = l.indexOf("="); if (i > 0) env[l.slice(0, i).trim()] = l.slice(i + 1).trim(); });
      const r = await api.mcpAdd(name.trim(), command.trim(), args, env);
      if (r.ok) onDone(); else setErr(r.error || "failed to add");
    }
  };

  return (
    <div className="border border-edge2 rounded-lg p-2 bg-panel2/60 flex flex-col gap-1.5">
      <div className="flex flex-wrap gap-1">
        {Object.keys(catalog).map((k) => (
          <button key={k} onClick={() => pickPreset(k)}
            className="px-1.5 py-0.5 rounded bg-bg border border-edge text-[10px] text-muted hover:text-txt">{k}</button>
        ))}
      </div>
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="name (e.g. github)"
        className="bg-bg border border-edge rounded px-2 h-6 text-[11px] focus:outline-none focus:border-accent2" />
      
      <input value={url} onChange={(e) => { setUrl(e.target.value); if (e.target.value.trim()) { setCommand(""); setArgStr(""); setEnvStr(""); } }}
        placeholder="URL (for HTTP, e.g. http://localhost:8000/mcp)"
        className="bg-bg border border-edge rounded px-2 h-6 text-[11px] font-mono focus:outline-none focus:border-accent2" />

      {!url.trim() && (
        <>
          <input value={command} onChange={(e) => setCommand(e.target.value)} placeholder="command (npx, uvx, ...)"
            className="bg-bg border border-edge rounded px-2 h-6 text-[11px] font-mono focus:outline-none focus:border-accent2" />
          <input value={argStr} onChange={(e) => setArgStr(e.target.value)} placeholder="args (space-separated)"
            className="bg-bg border border-edge rounded px-2 h-6 text-[11px] font-mono focus:outline-none focus:border-accent2" />
          <textarea value={envStr} onChange={(e) => setEnvStr(e.target.value)} placeholder="env (KEY=value per line)"
            rows={2} className="bg-bg border border-edge rounded px-2 py-1 text-[11px] font-mono resize-none focus:outline-none focus:border-accent2" />
        </>
      )}

      {err && <div className="text-risk text-[10px]">{err}</div>}
      <div className="flex gap-1.5">
        <button onClick={submit} disabled={!name.trim() || (!url.trim() && !command.trim())}
          className="flex-1 h-6 rounded bg-accent text-black/90 text-[11px] font-semibold flex items-center justify-center gap-1 disabled:opacity-40">
          <Check size={11} /> Add &amp; start
        </button>
        <button onClick={onDone} className="px-2 h-6 rounded border border-edge text-muted text-[11px] flex items-center gap-1"><X size={11} /></button>
      </div>
    </div>
  );
}
