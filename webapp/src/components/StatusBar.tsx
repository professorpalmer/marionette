import { useEffect, useState } from "react";
import { Circle, GitBranch, Boxes, Cpu, PanelLeft, PanelRight, Coins, ArrowUpCircle } from "lucide-react";
import { api, type Config } from "../lib/api";
import { isDesktop } from "../lib/transport";

// Bottom status strip (Hermes shell/statusbar pattern): runtime health, active
// workspace branch, job count, pilot model, build mode, and panel toggles.
export default function StatusBar({ config, jobCount, leftOpen, rightOpen, onToggleLeft, onToggleRight }: {
  config: Config | null; jobCount: number;
  leftOpen: boolean; rightOpen: boolean;
  onToggleLeft: () => void; onToggleRight: () => void;
}) {
  const [branch, setBranch] = useState("");
  const [usage, setUsage] = useState<{ tokens_used: number; est_cost_usd: number } | null>(null);
  const [update, setUpdate] = useState<{ version: string; url: string; name: string } | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  // Transient toast (e.g. a refused model switch). Auto-dismisses; never blocks.
  useEffect(() => {
    const onToast = (e: Event) => {
      const msg = (e as CustomEvent).detail;
      if (typeof msg === "string" && msg) {
        setToast(msg);
        window.setTimeout(() => setToast((cur) => (cur === msg ? null : cur)), 4000);
      }
    };
    window.addEventListener("harness-toast", onToast);
    return () => window.removeEventListener("harness-toast", onToast);
  }, []);

  // Tier-1 update check: ping GitHub Releases once on launch (desktop only).
  // Silent on failure -- an update nudge must never get in the way.
  useEffect(() => {
    const ipc = (window as any).harnessIPC;
    if (!ipc || !ipc.updates) return;
    let cancelled = false;
    ipc.updates.check()
      .then((res: any) => {
        if (!cancelled && res && res.available && res.url) {
          setUpdate({ version: res.version, url: res.url, name: res.name });
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  const openUpdate = () => {
    const ipc = (window as any).harnessIPC;
    if (ipc && ipc.updates && update) ipc.updates.openDownload(update.url);
  };

  const fetchUsage = () => {
    api.getUsage()
      .then((data) => {
        if (data && data.session) {
          setUsage({
            tokens_used: data.session.tokens_used,
            est_cost_usd: data.session.est_cost_usd,
          });
        }
      })
      .catch((err) => console.error("Failed to load usage in StatusBar", err));
  };

  useEffect(() => {
    api.workspaces().then((ws) => {
      const active = ws.find((w) => w.active);
      if (active) setBranch(active.name);
    }).catch(() => {});
  }, [config]);

  useEffect(() => {
    fetchUsage();
    const interval = setInterval(fetchUsage, 10000);
    return () => clearInterval(interval);
  }, [jobCount]);

  const formatTokens = (num: number) => {
    if (num >= 1000000) {
      return (num / 1000000).toFixed(1).replace(/\.0$/, "") + "M";
    }
    if (num >= 1000) {
      return (num / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    }
    return num.toString();
  };

  const formatCost = (num: number) => {
    if (num === 0) return "$0.00";
    if (num < 0.001) {
      return `$${num.toFixed(4)}`;
    }
    if (num < 0.01) {
      return `$${num.toFixed(3)}`;
    }
    return `$${num.toFixed(2)}`;
  };

  const showUsage = usage && usage.tokens_used > 0;

  return (
    <div className="flex items-center gap-3 px-3 h-6 border-t border-edge bg-panel text-[10px] text-muted select-none">
      <button onClick={onToggleLeft} title="Toggle sessions panel (Cmd+B)"
        className={`p-0.5 rounded hover:bg-panel2 ${leftOpen ? "text-txt" : "text-muted"}`}><PanelLeft size={12} /></button>
      <button onClick={onToggleRight} title="Toggle right panel (Cmd+J)"
        className={`p-0.5 rounded hover:bg-panel2 ${rightOpen ? "text-txt" : "text-muted"}`}><PanelRight size={12} /></button>
      <span className="w-px h-3 bg-edge" />
      <span className="flex items-center gap-1 text-good"><Circle size={7} className="fill-good text-good" /> ready</span>
      {branch && <span className="flex items-center gap-1"><GitBranch size={10} />{branch}</span>}
      <span className="flex items-center gap-1"><Boxes size={10} />{jobCount} job{jobCount === 1 ? "" : "s"}</span>
      {showUsage && (
        <>
          <span className="w-px h-3 bg-edge/40" />
          <span className="flex items-center gap-1 text-muted/80" title="Session token usage and estimated cost">
            <Coins size={10} className="text-faint" />
            <span>{formatTokens(usage.tokens_used)} tok</span>
            <span className="text-good font-medium">~{formatCost(usage.est_cost_usd)}</span>
          </span>
        </>
      )}
      <div className="flex-1" />
      {toast && (
        <span className="flex items-center gap-1 px-2 py-0.5 rounded bg-amber-500/10 border border-amber-500/30 text-amber-300/90">
          {toast}
        </span>
      )}
      <span className="flex items-center gap-1"><Cpu size={10} />{config?.driver?.split(":").pop() || "pilot"}</span>
      <span>{config?.reach || ""}</span>
      {update && (
        <button
          onClick={openUpdate}
          title={`Marionette ${update.version} is available -- click to download`}
          className="flex items-center gap-1 px-1.5 py-0.5 rounded text-accent hover:bg-accent/10 transition font-medium"
        >
          <ArrowUpCircle size={11} />
          <span>update {update.version}</span>
        </button>
      )}
      <span className="text-muted/60">{isDesktop ? "desktop" : "web"}</span>
    </div>
  );
}
