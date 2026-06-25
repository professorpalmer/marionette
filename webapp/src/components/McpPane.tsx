import { useEffect, useState } from "react";
import { Plug, Play, Square, Trash2, Plus, Check, X } from "lucide-react";
import { api } from "../lib/api";

// MCP server manager: add/start/stop/remove MCP servers and see their tools.
// "Access other MCPs people wanna add" -- github, aws, vercel, browser, custom.
export default function McpPane() {
  const [servers, setServers] = useState<any[]>([]);
  const [tools, setTools] = useState<any[]>([]);
  const [catalog, setCatalog] = useState<Record<string, any>>({});
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState("");

  const refresh = () => api.mcp().then((d) => { setServers(d.servers); setTools(d.tools); }).catch(() => {});
  useEffect(() => {
    refresh();
    api.mcpCatalog().then((d) => setCatalog(d.catalog)).catch(() => {});
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, []);

  const start = async (n: string) => { setBusy(n); try { await api.mcpStart(n); await refresh(); } finally { setBusy(""); } };
  const stop = async (n: string) => { setBusy(n); try { await api.mcpStop(n); await refresh(); } finally { setBusy(""); } };
  const remove = async (n: string) => { setBusy(n); try { await api.mcpRemove(n); await refresh(); } finally { setBusy(""); } };

  return (
    <div className="flex flex-col h-full text-[12px]">
      <div className="flex items-center justify-between px-3 py-2 border-b border-edge">
        <span className="uppercase tracking-wider text-[10px] text-faint font-medium flex items-center gap-1.5">
          <Plug size={11} /> MCP Servers
        </span>
        <button onClick={() => setAdding((v) => !v)} className="text-muted hover:text-txt"><Plus size={14} /></button>
      </div>

      <div className="flex-1 overflow-y-auto p-2 flex flex-col gap-1.5">
        {servers.length === 0 && !adding && (
          <div className="text-faint text-[11px] text-center mt-6 px-3 leading-relaxed">
            No MCP servers yet. Add github, aws, vercel, a browser controller, or any custom server.
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
        <div className="border-t border-edge p-2 max-h-44 overflow-y-auto">
          <div className="uppercase tracking-wider text-[10px] text-faint mb-1">Available tools ({tools.length})</div>
          {tools.map((t) => (
            <div key={t.qualified} className="py-1 border-b border-edge/20 last:border-none flex flex-wrap items-baseline gap-x-1.5 min-w-0">
              <span className="text-accent font-mono text-[11px] truncate max-w-full" title={t.qualified}>{t.qualified}</span>
              <span className="text-faint text-[10px] truncate max-w-full" title={t.description}>{t.description}</span>
            </div>
          ))}
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
