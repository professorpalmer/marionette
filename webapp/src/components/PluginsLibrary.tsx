import { useEffect, useState } from "react";
import { FolderPlus, Power, PowerOff, Puzzle } from "lucide-react";
import { api, type AgentPlugin } from "../lib/api";
import { usePanelNotice } from "../lib/useOperationalDiagnostic";
import PluginInstallModal from "./PluginInstallModal";

/** Agent plugin cards from GET /api/plugins, plus the install modal. */
export default function PluginsLibrary({ embedded = false }: { embedded?: boolean }) {
  const [plugins, setPlugins] = useState<AgentPlugin[]>([]);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  const [installOpen, setInstallOpen] = useState(false);
  const errorNotice = usePanelNotice(error || null);

  const refresh = () => {
    api.plugins()
      .then((r) => {
        setPlugins(r.plugins || []);
        setError(r.error || "");
      })
      .catch((err: { message?: string }) => {
        setError(err?.message || "Failed to load plugins");
      });
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
      refresh();
    } catch {
      setError("toggle failed");
    } finally {
      setBusy("");
    }
  };

  return (
    <div className={embedded ? "space-y-3" : "space-y-3 max-w-2xl"} data-testid="plugins-library">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2 text-[12px] text-muted">
          <Puzzle size={14} className="text-accent" />
          <span>
            Portable Agent Plugins (skills + stdio MCP). Default-disabled until you enable them.
          </span>
        </div>
        <button
          type="button"
          onClick={() => setInstallOpen(true)}
          className="shrink-0 inline-flex items-center gap-1 px-2.5 py-1.5 rounded-md bg-panel2 border border-edge/50 text-[12px] text-txt hover:bg-panel2/80"
          data-testid="plugins-install-open"
        >
          <FolderPlus size={13} />
          Install
        </button>
      </div>

      {(msg || errorNotice) && (
        <div className={`text-[11px] ${errorNotice ? "text-red-400" : "text-muted"}`}>
          {errorNotice || msg}
        </div>
      )}

      {plugins.length === 0 ? (
        <div className="text-[12px] text-faint" data-testid="plugins-library-empty">
          No plugins installed.
        </div>
      ) : (
        <ul className="grid gap-3 sm:grid-cols-2" data-testid="plugins-library-cards">
          {plugins.map((p) => (
            <li
              key={p.id}
              data-testid={`plugin-card-${p.id}`}
              className="rounded-lg border border-edge/40 bg-panel2/60 p-3 flex flex-col gap-2"
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
                className="self-start inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] border border-edge/50 text-txt hover:bg-panel2 disabled:opacity-50"
                data-testid={`plugin-toggle-${p.id}`}
              >
                {p.enabled ? <PowerOff size={12} /> : <Power size={12} />}
                {p.enabled ? "Disable" : "Enable"}
              </button>
            </li>
          ))}
        </ul>
      )}

      <PluginInstallModal
        open={installOpen}
        onClose={() => setInstallOpen(false)}
        onInstalled={() => refresh()}
        onEnabled={() => {
          setMsg("Enabled plugin");
          refresh();
        }}
      />
    </div>
  );
}
