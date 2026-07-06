import { useEffect, useRef, useState } from "react";
import { Circle, GitBranch, Boxes, Cpu, PanelLeft, PanelRight, Coins, ArrowUpCircle, RefreshCw, Zap } from "lucide-react";
import { api, type Config } from "../lib/api";
import { isDesktop } from "../lib/transport";
import CostBreakdown from "./CostBreakdown";

// Bottom status strip (Hermes shell/statusbar pattern): runtime health, active
// workspace branch, job count, pilot model, build mode, and panel toggles.
export default function StatusBar({ config, jobCount, leftOpen, rightOpen, onToggleLeft, onToggleRight }: {
  config: Config | null; jobCount: number;
  leftOpen: boolean; rightOpen: boolean;
  onToggleLeft: () => void; onToggleRight: () => void;
}) {
  const [branch, setBranch] = useState("");
  const [usage, setUsage] = useState<{ tokens_used: number; est_cost_usd: number; tokens_cached?: number; cache_savings_usd?: number; tool_output_tokens_saved?: number; tool_output_savings_usd?: number; price_in?: number; price_out?: number } | null>(null);
  const [costOpen, setCostOpen] = useState(false);
  const costRef = useRef<HTMLDivElement | null>(null);
  const [update, setUpdate] = useState<{ behind: number; branch: string; version: string } | null>(null);
  const [apply, setApply] = useState<{ stage: string; message: string; percent: number | null } | null>(null);
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

  // Self-update check: how far behind the tracked branch we are (desktop only).
  // Silent on failure -- an update nudge must never get in the way.
  useEffect(() => {
    const ipc = (window as any).harnessIPC;
    if (!ipc || !ipc.updates) return;
    let cancelled = false;
    ipc.updates.check()
      .then((res: any) => {
        if (!cancelled && res && res.available) {
          setUpdate({ behind: res.behind || 0, branch: res.branch || "main", version: res.current || "" });
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  // The UpdateBanner owns the single, robust apply() path (latching, error
  // recovery, watchdog, idempotent install). The pill just asks it to start and
  // then mirrors progress -- so the two surfaces can never show conflicting
  // states. The old code ran its own independent apply() loop here and dropped
  // its progress listener the instant apply() resolved, which froze the pill at
  // "Installing update -- restarting" forever when the relaunch stalled.
  const runUpdate = () => {
    if (apply) return;
    setApply({ stage: "prepare", message: "Preparing update", percent: null });
    window.dispatchEvent(new Event("harness-update-apply"));
  };

  // Mirror the banner-owned update flow: "committing" mounts the spinner, "idle"
  // clears it, and progress events advance the label once we're committing. We
  // ignore pre-commit background download churn (prev == null) so the pill never
  // spins before the user actually asked to update.
  useEffect(() => {
    const ipc = (window as any).harnessIPC;
    const onCommitting = () =>
      setApply((prev) => prev || { stage: "prepare", message: "Preparing update", percent: null });
    const onIdle = () => setApply(null);
    window.addEventListener("harness-update-committing", onCommitting);
    window.addEventListener("harness-update-idle", onIdle);
    let off: (() => void) | null = null;
    if (ipc && ipc.updates) {
      off = ipc.updates.onProgress((p: any) => {
        if (!p || !p.stage) return;
        if (p.stage === "error") { setApply(null); return; }
        setApply((prev) => (prev ? { stage: p.stage, message: p.message || "Updating", percent: p.percent ?? null } : prev));
      });
    }
    return () => {
      window.removeEventListener("harness-update-committing", onCommitting);
      window.removeEventListener("harness-update-idle", onIdle);
      if (off) off();
    };
  }, []);

  // In-flight guard: while a swarm keeps the backend busy, getUsage can take
  // longer than the 10s cadence. Skipping an overlapping poll keeps the status
  // bar from adding to the request pileup that made panels load in chunks.
  const usageInFlight = useRef(false);
  const fetchUsage = () => {
    if (usageInFlight.current) return;
    usageInFlight.current = true;
    api.getUsage()
      .then((data) => {
        if (data && data.session) {
          setUsage({
            tokens_used: data.session.tokens_used,
            est_cost_usd: data.session.est_cost_usd,
            tokens_cached: (data.session as { tokens_cached?: number }).tokens_cached,
            cache_savings_usd: (data.session as { cache_savings_usd?: number }).cache_savings_usd,
            tool_output_tokens_saved: (data.session as { tool_output_tokens_saved?: number }).tool_output_tokens_saved,
            tool_output_savings_usd: (data.session as { tool_output_savings_usd?: number }).tool_output_savings_usd,
            price_in: (data.session as { price_in?: number }).price_in,
            price_out: (data.session as { price_out?: number }).price_out,
          });
        }
      })
      .catch((err) => console.error("Failed to load usage in StatusBar", err))
      .finally(() => { usageInFlight.current = false; });
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

  // Dismiss the cost breakdown popover on outside click or Escape, matching the
  // PilotPicker dropdown behavior so the status bar has one consistent pattern.
  useEffect(() => {
    if (!costOpen) return;
    const onDown = (e: MouseEvent) => {
      if (costRef.current && !costRef.current.contains(e.target as Node)) setCostOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setCostOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [costOpen]);

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
            {usage.tokens_cached && usage.tokens_cached > 0 ? (
              <span
                className="text-accent/80"
                title={`Prompt tokens served from cache (near-free input)${
                  usage.cache_savings_usd && usage.cache_savings_usd > 0
                    ? ` -- saved ~${formatCost(usage.cache_savings_usd)} vs no caching`
                    : ""
                }`}
              >
                {formatTokens(usage.tokens_cached)} cached
                {usage.cache_savings_usd && usage.cache_savings_usd > 0
                  ? ` (~${formatCost(usage.cache_savings_usd)} saved)`
                  : ""}
              </span>
            ) : null}
            {usage.tool_output_tokens_saved && usage.tool_output_tokens_saved > 0 ? (
              <span
                className="text-accent/70"
                title={`Context tokens avoided by compacting oversized tool outputs${
                  usage.tool_output_savings_usd && usage.tool_output_savings_usd > 0
                    ? ` (~${formatCost(usage.tool_output_savings_usd)} at input price)`
                    : ""
                }`}
              >
                {formatTokens(usage.tool_output_tokens_saved)} compacted
              </span>
            ) : null}
            {/* The estimated cost is now a click/hover trigger for a compact
                routing-value breakdown (why this model / what it saved). It
                stays a plain figure when there is nothing meaningful to expand. */}
            <span className="relative inline-flex items-center" ref={costRef}>
              <button
                type="button"
                onClick={() => setCostOpen((v) => !v)}
                title="Session estimated cost -- click for the routing cost breakdown"
                className="text-good font-medium hover:text-good/80 transition cursor-pointer"
              >
                ~{formatCost(usage.est_cost_usd)}
              </button>
              {costOpen && (
                <div className="absolute bottom-full right-0 mb-1.5 z-50">
                  <CostBreakdown
                    data={{
                      tokens_used: usage.tokens_used,
                      est_cost_usd: usage.est_cost_usd,
                      tokens_cached: usage.tokens_cached,
                      cache_savings_usd: usage.cache_savings_usd,
                      tool_output_tokens_saved: usage.tool_output_tokens_saved,
                      tool_output_savings_usd: usage.tool_output_savings_usd,
                      price_in: usage.price_in,
                      price_out: usage.price_out,
                    }}
                  />
                </div>
              )}
            </span>
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
      {/* Show the ACTIVE model's provider (the driver spec's prefix), not the
          fallback reach. A "provider:model" driver routes through that provider;
          only a bare, unprefixed model actually falls back to reach. Showing
          reach unconditionally made e.g. anthropic:claude-opus read "openrouter". */}
      <span>{(config?.driver?.includes(":") ? config.driver.split(":")[0] : config?.reach) || ""}</span>
      {config?.edit_engine === "agentic" && (
        <span
          className="flex items-center gap-1 text-good/80"
          title="Standalone: edits and swarms route directly through your provider keys -- no external agent CLI"
        >
          <Zap size={10} className="text-good/70" />standalone
        </span>
      )}
      {apply ? (
        <span
          className="flex items-center gap-1 px-1.5 py-0.5 rounded text-accent"
          title={apply.message}
        >
          <RefreshCw size={11} className="animate-spin" />
          {/* The installed-app updater bakes the percent into the message
              ("Downloading update 87%"), so only append apply.percent when the
              message doesn't already carry one -- otherwise "... 87% 87%". */}
          <span>{apply.message}{apply.percent != null && !/\d%\s*$/.test(apply.message) ? ` ${apply.percent}%` : ""}</span>
        </span>
      ) : update ? (
        <button
          onClick={runUpdate}
          title={`${update.behind ? update.behind + " commit(s)" : "An update is"} behind ${update.branch} -- click to update and relaunch`}
          className="flex items-center gap-1 px-1.5 py-0.5 rounded text-accent hover:bg-accent/10 transition font-medium"
        >
          <ArrowUpCircle size={11} />
          <span>update{update.behind ? ` (${update.behind})` : ""}</span>
        </button>
      ) : null}
      <span className="text-muted/60">{isDesktop ? "desktop" : "web"}</span>
    </div>
  );
}
