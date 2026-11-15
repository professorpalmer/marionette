import { useState, type FormEvent } from "react";
import { api, type AgentPlugin } from "../lib/api";
import { PluginSourceError, resolvePluginSource } from "../lib/pluginSourceUrls";
import { usePanelNotice } from "../lib/useOperationalDiagnostic";
import { usePolling } from "../lib/usePolling";
import {
  SETTINGS_PRIMARY_BUTTON,
  SETTINGS_QUIET_BUTTON,
  SettingsGroup,
  SettingsPage,
  SettingsRow,
} from "./SettingsLayout";

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

/** The state of one plugin in words: on or off, then what it adds. */
function pluginStateText(plugin: AgentPlugin): string {
  if (plugin.error) return `Error: ${plugin.error}`;
  const adds = `${plural(plugin.skill_count, "skill")}, ${plural(plugin.mcp_count, "MCP server")}`;
  return `${plugin.enabled ? "On" : "Off"} · ${adds}`;
}

type InstallForm = { source: string; force: boolean };

/** Settings Plugins page: installed agent plugins, one row each, and an install row. */
export default function PluginsPane() {
  const [plugins, setPlugins] = useState<AgentPlugin[] | null>(null);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [form, setForm] = useState<InstallForm | null>(null);
  const errorNotice = usePanelNotice(error || null);

  const refresh = () => api.plugins()
    .then((res) => {
      setPlugins(res.plugins || []);
      setError(res.error || "");
    })
    .catch((err: { message?: string }) => {
      setError(err?.message || "Could not read plugins");
    });

  usePolling(refresh, 5000);

  const toggle = async (plugin: AgentPlugin) => {
    setBusy(plugin.id);
    setMessage("");
    setError("");
    try {
      const res = plugin.enabled ? await api.pluginDisable(plugin.id) : await api.pluginEnable(plugin.id);
      if (!res.ok) {
        setError(res.error || "The change did not save");
        return;
      }
      setMessage(plugin.enabled ? `Turned off ${plugin.name}.` : `Turned on ${plugin.name}.`);
      await refresh();
    } catch {
      setError("The change did not save");
    } finally {
      setBusy("");
    }
  };

  const install = async (event: FormEvent) => {
    event.preventDefault();
    if (!form) return;
    const raw = form.source.trim();
    try {
      resolvePluginSource(raw);
    } catch (err) {
      setError(err instanceof PluginSourceError ? err.message : "The plugin source is not valid");
      return;
    }
    setBusy("install");
    setMessage("");
    setError("");
    try {
      const res = await api.pluginInstall(raw, { force: form.force });
      if (!res.ok || !res.plugin) {
        setError(res.error || "The install did not complete");
        return;
      }
      setForm(null);
      setMessage(`Installed ${res.plugin.name}. It stays off until you turn it on.`);
      await refresh();
    } catch {
      setError("The install did not complete");
    } finally {
      setBusy("");
    }
  };

  return (
    <SettingsPage
      title="Plugins"
      description="An agent plugin adds skills and MCP servers. A new plugin stays off until you turn it on."
      testId="plugins-pane"
    >
      {errorNotice || message ? (
        <p
          className={`text-ui-11 ${errorNotice ? "text-risk" : "text-muted"}`}
          role={errorNotice ? "alert" : "status"}
          data-testid="plugins-result"
        >
          {errorNotice || message}
        </p>
      ) : null}

      <SettingsGroup title="Installed" testId="plugins-installed">
        {plugins === null ? (
          <SettingsRow label="Reading plugins..." />
        ) : plugins.length === 0 ? (
          <SettingsRow label="No plugins installed." testId="plugins-empty" />
        ) : (
          plugins.map((plugin) => (
            <SettingsRow
              key={plugin.id}
              testId={`plugin-row-${plugin.id}`}
              label={(
                <>
                  {plugin.name}
                  {plugin.version ? <span className="ml-1.5 text-faint">v{plugin.version}</span> : null}
                </>
              )}
              description={plugin.description ? <span className="line-clamp-1">{plugin.description}</span> : undefined}
              state={<span className={plugin.error ? "text-risk" : undefined}>{pluginStateText(plugin)}</span>}
              control={(
                <button
                  type="button"
                  className={plugin.enabled ? SETTINGS_QUIET_BUTTON : SETTINGS_PRIMARY_BUTTON}
                  disabled={busy === plugin.id}
                  onClick={() => { void toggle(plugin); }}
                  data-testid={`plugin-toggle-${plugin.id}`}
                >
                  {plugin.enabled ? "Turn off" : "Turn on"}
                </button>
              )}
            />
          ))
        )}
      </SettingsGroup>

      <SettingsGroup>
        <SettingsRow
          label="Install a plugin"
          description="From an absolute folder path, a git URL, an https URL, or a GitHub repo."
          control={form ? undefined : (
            <button
              type="button"
              className={SETTINGS_PRIMARY_BUTTON}
              onClick={() => { setForm({ source: "", force: false }); setError(""); setMessage(""); }}
              data-testid="plugins-install-open"
            >
              Install
            </button>
          )}
        >
          {form ? (
            <form onSubmit={install} className="space-y-1.5" data-testid="plugins-install-form">
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={form.source}
                  onChange={(e) => setForm({ ...form, source: e.target.value })}
                  disabled={busy === "install"}
                  placeholder="/absolute/path or https://github.com/owner/repo"
                  aria-label="Plugin source"
                  autoFocus
                  data-testid="plugin-install-source"
                  className="flex-1 bg-panel border border-edge rounded px-2 py-0.5 text-ui-11 font-mono focus:outline-none focus:border-accent disabled:opacity-50 placeholder:text-faint"
                />
                <button
                  type="submit"
                  className={SETTINGS_PRIMARY_BUTTON}
                  disabled={busy === "install" || !form.source.trim()}
                  data-testid="plugin-install-submit"
                >
                  {busy === "install" ? "Installing..." : "Install"}
                </button>
                <button
                  type="button"
                  className={SETTINGS_QUIET_BUTTON}
                  disabled={busy === "install"}
                  onClick={() => setForm(null)}
                >
                  Cancel
                </button>
              </div>
              <label className="flex items-center gap-2 text-ui-11 text-muted">
                <input
                  type="checkbox"
                  checked={form.force}
                  onChange={(e) => setForm({ ...form, force: e.target.checked })}
                  disabled={busy === "install"}
                  data-testid="plugin-install-force"
                />
                Install again if it is already installed
              </label>
            </form>
          ) : null}
        </SettingsRow>
      </SettingsGroup>
    </SettingsPage>
  );
}
