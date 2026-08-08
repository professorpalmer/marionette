import { useEffect, useState } from "react";
import { Puzzle, Power, PowerOff, FolderPlus } from "lucide-react";
import { api, type AgentPlugin } from "../lib/api";

/** Minimal Agent Plugins v1 Settings pane: list, enable/disable, install path. */
export default function PluginsPane({ embedded = false }: { embedded?: boolean }) {
  const [plugins, setPlugins] = useState<AgentPlugin[]>([]);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");
  const [installPath, setInstallPath] = useState("");
  const [error, setError] = useState("");

  const refresh = () => {
    api.plugins()
      .then((r) => setPlugins(r.plugins || []))
      .catch(() => {});
  };
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  const toggle = async (plugin: AgentPlugin) => {
    setBusy(plugin.id);
    setMsg("");
    setError("");
    try {
      const res = plugin.enabled
        ? await api.pluginDisable(plugin.id)
        : await api.pluginEnable(plugin.id);
      if (!res.ok) {
        setError(res.error || "toggle failed");
        return;
      }
      setMsg(plugin.enabled ? `Disabled ${plugin.name}` : `Enabled ${plugin.name}`);
      await refresh();
    } catch {
      setError("toggle failed");
    } finally {
      setBusy("");
    }
  };

  const install = async () => {
    const path = installPath.trim();
    if (!path) {
      setError("Absolute package path is required");
      return;
    }
    if (!path.startsWith("/") && !/^[A-Za-z]:[\\/]/.test(path)) {
      setError("Install path must be absolute");
      return;
    }
    setBusy("install");
    setError("");
    setMsg("");
    try {
      const res = await api.pluginInstall(path);
      if (!res.ok) {
        setError(res.error || "install failed");
        return;
      }
      setInstallPath("");
      setMsg(`Installed ${res.plugin?.name || "plugin"} (disabled until enabled)`);
      await refresh();
    } catch {
      setError("install failed");
    } finally {
      setBusy("");
    }
  };

  return (
    <div className={embedded ? "space-y-3" : "space-y-3 max-w-2xl"} data-testid="plugins-pane">
      <div className="flex items-center gap-2 text-[12px] text-muted">
        <Puzzle size={14} className="text-accent" />
        <span>
          Portable Agent Plugins (skills + stdio MCP). Default-disabled until you enable them.
        </span>
      </div>

      <div className="flex gap-2 items-center">
        <input
          type="text"
          value={installPath}
          onChange={(e) => setInstallPath(e.target.value)}
          placeholder="/absolute/path/to/plugin"
          className="flex-1 min-w-0 px-2 py-1.5 rounded-md bg-panel2 border border-edge/50 text-[12px] text-txt placeholder:text-faint"
          data-testid="plugins-install-path"
        />
        <button
          type="button"
          onClick={install}
          disabled={busy === "install"}
          className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-md bg-panel2 border border-edge/50 text-[12px] text-txt hover:bg-panel2/80 disabled:opacity-50"
          data-testid="plugins-install-btn"
        >
          <FolderPlus size={13} />
          Install
        </button>
      </div>

      {(msg || error) && (
        <div className={`text-[11px] ${error ? "text-red-400" : "text-muted"}`}>
          {error || msg}
        </div>
      )}

      {plugins.length === 0 ? (
        <div className="text-[12px] text-faint">No plugins installed.</div>
      ) : (
        <ul className="space-y-2">
          {plugins.map((p) => (
            <li
              key={p.id}
              className="flex items-start justify-between gap-3 py-2 border-b border-edge/30 last:border-0"
            >
              <div className="min-w-0">
                <div className="text-[12px] font-medium text-txt truncate">
                  {p.name}
                  {p.version ? (
                    <span className="ml-1.5 text-faint font-normal">v{p.version}</span>
                  ) : null}
                </div>
                {p.description ? (
                  <div className="text-[11px] text-muted mt-0.5 line-clamp-2">{p.description}</div>
                ) : null}
                <div className="text-[10px] text-faint mt-1">
                  {p.enabled ? "enabled" : "disabled"}
                  {" · "}
                  {p.skill_count} skill{p.skill_count === 1 ? "" : "s"}
                  {" · "}
                  {p.mcp_count} MCP
                  {p.error ? ` · ${p.error}` : ""}
                </div>
              </div>
              <button
                type="button"
                onClick={() => toggle(p)}
                disabled={busy === p.id}
                className="shrink-0 inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] border border-edge/50 text-txt hover:bg-panel2 disabled:opacity-50"
                data-testid={`plugin-toggle-${p.id}`}
              >
                {p.enabled ? <PowerOff size={12} /> : <Power size={12} />}
                {p.enabled ? "Disable" : "Enable"}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
