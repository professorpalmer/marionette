import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { api, type AgentPlugin } from "../lib/api";
import { OverlayPortal } from "../lib/overlayPortal";
import { PluginSourceError, resolvePluginSource } from "../lib/pluginSourceUrls";
import { usePanelNotice } from "../lib/useOperationalDiagnostic";

export type PluginInstallModalProps = {
  open: boolean;
  onClose: () => void;
  onInstalled?: (plugin: AgentPlugin) => void;
  onEnabled?: (plugin: AgentPlugin) => void;
};

/** Install an Agent Plugin from a path, git URL, https URL, or GitHub source. */
export default function PluginInstallModal({
  open,
  onClose,
  onInstalled,
  onEnabled,
}: PluginInstallModalProps) {
  const sourceRef = useRef<HTMLInputElement>(null);
  const [source, setSource] = useState("");
  const [force, setForce] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [installed, setInstalled] = useState<AgentPlugin | null>(null);
  const errorNotice = usePanelNotice(error || null);

  useEffect(() => {
    if (!open) return;
    setSource("");
    setForce(false);
    setBusy("");
    setError("");
    setInstalled(null);
  }, [open]);

  const handleClose = () => {
    if (busy) return;
    onClose();
  };

  const install = async (e: React.FormEvent) => {
    e.preventDefault();
    const raw = source.trim();
    if (!raw) {
      setError("Plugin source path, git URL, https URL, or GitHub repo is required");
      return;
    }
    try {
      resolvePluginSource(raw);
    } catch (err) {
      setError(err instanceof PluginSourceError ? err.message : "Invalid plugin source");
      return;
    }
    setBusy("install");
    setError("");
    try {
      const res = await api.pluginInstall(raw, { force });
      if (!res.ok || !res.plugin) {
        setError(res.error || "install failed");
        return;
      }
      setInstalled(res.plugin);
      onInstalled?.(res.plugin);
    } catch {
      setError("install failed");
    } finally {
      setBusy("");
    }
  };

  const enable = async () => {
    if (!installed) return;
    setBusy("enable");
    setError("");
    try {
      const res = await api.pluginEnable(installed.id);
      if (!res.ok) {
        setError(res.error || "enable failed");
        return;
      }
      onEnabled?.(res.plugin || installed);
      onClose();
    } catch {
      setError("enable failed");
    } finally {
      setBusy("");
    }
  };

  const title = installed ? "Enable plugin" : "Install plugin";

  return (
    <OverlayPortal
      open={open}
      onClose={handleClose}
      testId="plugin-install-modal"
      className="fixed inset-0 z-[90] bg-black/50 flex items-center justify-center p-4"
      initialFocusRef={sourceRef}
      onBackdropClick={(e) => {
        if (e.target === e.currentTarget) handleClose();
      }}
    >
      <form
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onSubmit={installed ? (e) => e.preventDefault() : install}
        className="w-full max-w-md rounded-lg border border-edge bg-bg shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-edge/40">
          <h2 className="text-[13px] font-semibold text-txt">{title}</h2>
          <button
            type="button"
            onClick={handleClose}
            title="Close"
            disabled={Boolean(busy)}
            className="p-1 rounded-md text-muted hover:text-txt hover:bg-panel2 disabled:opacity-50"
          >
            <X size={14} />
          </button>
        </div>

        <div className="px-4 py-3 space-y-3">
          {installed ? (
            <div data-testid="plugin-install-enable-prompt" className="space-y-2">
              <p className="text-[12px] text-txt">
                Installed {installed.name}
                {installed.version ? ` v${installed.version}` : ""}. Enable it now?
              </p>
              <p className="text-[11px] text-muted">
                Agent plugins stay disabled until you enable them.
              </p>
            </div>
          ) : (
            <>
              <label className="block space-y-1">
                <span className="text-[11px] text-muted">Source</span>
                <input
                  ref={sourceRef}
                  data-testid="plugin-install-source"
                  type="text"
                  value={source}
                  onChange={(e) => setSource(e.target.value)}
                  disabled={Boolean(busy)}
                  placeholder="/absolute/path or https://github.com/owner/repo"
                  className="w-full bg-panel2 border border-edge rounded px-2 py-1.5 text-txt text-[12px] font-mono focus:outline-none focus:border-accent disabled:opacity-50 placeholder:text-faint"
                />
              </label>
              <label className="flex items-center gap-2 text-[12px] text-txt">
                <input
                  data-testid="plugin-install-force"
                  type="checkbox"
                  checked={force}
                  onChange={(e) => setForce(e.target.checked)}
                  disabled={Boolean(busy)}
                />
                Force reinstall if already installed
              </label>
            </>
          )}

          {errorNotice ? (
            <div data-testid="plugin-install-error" className="text-[11px] text-red-400">
              {errorNotice}
            </div>
          ) : null}
        </div>

        <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-edge/40">
          {installed ? (
            <>
              <button
                type="button"
                data-testid="plugin-install-skip-enable"
                onClick={handleClose}
                disabled={Boolean(busy)}
                className="text-muted hover:text-txt border border-edge rounded px-2.5 py-1 text-[11px] disabled:opacity-50"
              >
                Not now
              </button>
              <button
                type="button"
                data-testid="plugin-install-enable"
                onClick={enable}
                disabled={busy === "enable"}
                className="bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 rounded px-2.5 py-1 text-[11px] font-medium disabled:opacity-30"
              >
                Enable
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={handleClose}
                disabled={Boolean(busy)}
                className="text-muted hover:text-txt border border-edge rounded px-2.5 py-1 text-[11px] disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="submit"
                data-testid="plugin-install-submit"
                disabled={busy === "install"}
                className="bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 rounded px-2.5 py-1 text-[11px] font-medium disabled:opacity-30"
              >
                Install
              </button>
            </>
          )}
        </div>
      </form>
    </OverlayPortal>
  );
}
