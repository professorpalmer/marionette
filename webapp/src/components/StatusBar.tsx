import { useEffect, useRef, useState } from "react";
import { Circle, GitBranch, Cpu, PanelLeft, PanelRight, Coins, ArrowUpCircle, RefreshCw, Zap } from "lucide-react";
import { api, type Config, type SessionState, type UsageData } from "../lib/api";
import { isDesktop } from "../lib/transport";
import { usePolling } from "../lib/usePolling";
import CostBreakdown, { listPriceValueTotal, spendIsEstimated } from "./CostBreakdown";
import { sanitizeUpdateMessage } from "../lib/updateMessages";

type FooterRuntimeStatus = "ready" | "thinking" | "busy";

/** Mirror Conversation/LeftRail: pilot state + active-view runner liveness. */
export function deriveFooterRuntimeStatus(
  sessionState: SessionState | null,
): FooterRuntimeStatus {
  if (!sessionState) return "ready";
  if (sessionState.state === "awaiting_swarm" || sessionState.pending_swarms) return "busy";
  if (sessionState.state === "thinking") return "thinking";
  const runners = sessionState.runners ?? {};
  const activeId = sessionState.active_view_id;
  // Only the active VIEW's runner drives the footer "thinking" chrome.
  // Background sessions may keep running under the lease without flipping
  // the active view to thinking.
  if (activeId && runners[activeId] === "running") return "thinking";
  return "ready";
}

// Bottom status strip (Hermes shell/statusbar pattern): runtime health, active
// workspace branch, pilot model, spend, and panel toggles. Job inventory lives
// in LeftRail SESSION JOBS -- a footer total was stale across dir swaps and
// disagreed with the scoped list, so it was removed.
export default function StatusBar({ config, leftOpen, rightOpen, onToggleLeft, onToggleRight }: {
  config: Config | null;
  leftOpen: boolean; rightOpen: boolean;
  onToggleLeft: () => void; onToggleRight: () => void;
}) {
  const [branch, setBranch] = useState("");
  const [usage, setUsage] = useState<UsageData["session"] | null>(null);
  const [costOpen, setCostOpen] = useState(false);
  const costRef = useRef<HTMLDivElement | null>(null);
  const [update, setUpdate] = useState<{ behind: number; branch: string; version: string } | null>(null);
  const [apply, setApply] = useState<{ stage: string; message: string; percent: number | null } | null>(null);
  const [toast, setToast] = useState<{
    message: string;
    actionLabel?: string;
    actionEvent?: string;
  } | null>(null);
  const [sessionState, setSessionState] = useState<SessionState | null>(null);

  // Transient toast (e.g. a refused model switch). Auto-dismisses; never blocks.
  // detail may be a string or { message, actionLabel?, actionEvent? } for Undo.
  useEffect(() => {
    const onToast = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      let next: { message: string; actionLabel?: string; actionEvent?: string } | null = null;
      if (typeof detail === "string" && detail) {
        next = { message: detail };
      } else if (detail && typeof detail === "object" && typeof detail.message === "string" && detail.message) {
        next = {
          message: detail.message,
          actionLabel: typeof detail.actionLabel === "string" ? detail.actionLabel : undefined,
          actionEvent: typeof detail.actionEvent === "string" ? detail.actionEvent : undefined,
        };
      }
      if (!next) return;
      setToast(next);
      const snapshot = next;
      window.setTimeout(
        () => setToast((cur) => (cur?.message === snapshot.message ? null : cur)),
        4000,
      );
    };
    window.addEventListener("harness-toast", onToast);
    return () => window.removeEventListener("harness-toast", onToast);
  }, []);

  // Self-update check: how far behind the tracked branch we are (desktop only).
  // Silent on failure -- an update nudge must never get in the way. Re-checks
  // every 30 minutes and on window focus (throttled) so a release that lands
  // mid-session surfaces without a relaunch -- previously the pill only ever
  // checked once on mount.
  useEffect(() => {
    const ipc = (window as any).harnessIPC;
    if (!ipc || !ipc.updates) return;
    let cancelled = false;
    let lastCheck = 0;
    const MIN_GAP_MS = 5 * 60 * 1000;
    const check = (force = false) => {
      const now = Date.now();
      if (!force && now - lastCheck < MIN_GAP_MS) return;
      lastCheck = now;
      ipc.updates.check()
        .then((res: any) => {
          if (!cancelled && res && res.available) {
            setUpdate({ behind: res.behind || 0, branch: res.branch || "main", version: res.current || "" });
          }
        })
        .catch(() => {});
    };
    check(true);
    const interval = window.setInterval(() => check(true), 30 * 60 * 1000);
    const onFocus = () => check();
    window.addEventListener("focus", onFocus);
    // PUSH path: the main-process update watcher notifies the moment its
    // background fetch sees new commits, so the pill appears without waiting
    // for the next renderer poll tick.
    const offAvailable = ipc.updates.onAvailable
      ? ipc.updates.onAvailable((res: any) => {
          if (!cancelled && res && res.available) {
            setUpdate({ behind: res.behind || 0, branch: res.branch || "main", version: res.current || "" });
          }
        })
      : null;
    return () => {
      cancelled = true;
      window.clearInterval(interval);
      window.removeEventListener("focus", onFocus);
      if (offAvailable) offAvailable();
    };
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
        setApply((prev) => (prev ? { stage: p.stage, message: sanitizeUpdateMessage(p.stage, p.message || ""), percent: p.percent ?? null } : prev));
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
  // After a session/project switch, accept the next meters even if zero so a
  // prior chat's totals cannot stick via the zeros-guard.
  const acceptZeroUsageRef = useRef(false);
  const fetchUsage = () => {
    if (usageInFlight.current) return;
    usageInFlight.current = true;
    api.getUsage()
      .then((data) => {
        if (data && data.session) {
          // Belt-and-suspenders: a workspace switch rebuilds the pilot and can
          // briefly report all-zero meters before the copy lands (or if an older
          // backend omits the carry). Keep last-good spend rather than blanking
          // the status-bar cluster on a zeros response — but never across
          // session boundaries (see harness-session-changed).
          setUsage((prev) => {
            const next = data.session;
            const nextZero =
              (next.tokens_used ?? 0) === 0 && (next.est_cost_usd ?? 0) === 0;
            const prevHadSpend =
              !!prev && ((prev.tokens_used ?? 0) > 0 || (prev.est_cost_usd ?? 0) > 0);
            if (acceptZeroUsageRef.current) {
              acceptZeroUsageRef.current = false;
              return next;
            }
            if (nextZero && prevHadSpend) return prev;
            return next;
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

  // Poll runner/pilot liveness so the footer reflects real busy state (LeftRail
  // uses the same endpoint on the same cadence for per-session dots).
  usePolling(() => api.getSessionState()
    .then((stateRes) => { if (stateRes) setSessionState(stateRes); })
    .catch(() => {}), 4000);

  const runtimeStatus = deriveFooterRuntimeStatus(sessionState);
  const runtimeReady = runtimeStatus === "ready";
  // While a runner is busy, poll tok/$ every 2s so multi-step host-tool turns
  // (and the jump when a long Cursor CLI stream finally meters) show up live.
  // Idle stays on the 10s cadence to avoid request pileup.
  const usageBusy = runtimeStatus === "busy" || runtimeStatus === "thinking";

  useEffect(() => {
    fetchUsage();
    const interval = setInterval(fetchUsage, usageBusy ? 2000 : 10000);
    const onRefresh = () => fetchUsage();
    const onSessionChanged = () => {
      acceptZeroUsageRef.current = true;
      setUsage(null);
      fetchUsage();
    };
    window.addEventListener("harness-config-changed", onRefresh);
    window.addEventListener("harness-project-selected", onRefresh);
    window.addEventListener("harness-new-session", onRefresh);
    window.addEventListener("harness-usage-refresh", onRefresh);
    window.addEventListener("harness-session-changed", onSessionChanged);
    return () => {
      clearInterval(interval);
      window.removeEventListener("harness-config-changed", onRefresh);
      window.removeEventListener("harness-project-selected", onRefresh);
      window.removeEventListener("harness-new-session", onRefresh);
      window.removeEventListener("harness-usage-refresh", onRefresh);
      window.removeEventListener("harness-session-changed", onSessionChanged);
    };
  }, [usageBusy]);

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

  const showUsage = usage && (usage.tokens_used > 0 || usage.est_cost_usd > 0);

  return (
    <div className="flex items-center gap-3 px-3 h-6 border-t border-edge bg-panel text-[10px] text-muted select-none">
      <button onClick={onToggleLeft} title="Toggle sessions panel (Ctrl/Cmd+B)"
        className={`p-0.5 rounded hover:bg-panel2 ${leftOpen ? "text-txt" : "text-muted"}`}><PanelLeft size={12} /></button>
      <button onClick={onToggleRight} title="Toggle right panel (Ctrl/Cmd+J)"
        className={`p-0.5 rounded hover:bg-panel2 ${rightOpen ? "text-txt" : "text-muted"}`}><PanelRight size={12} /></button>
      <span className="w-px h-3 bg-edge" />
      <span
        className={`flex items-center gap-1 ${runtimeReady ? "text-good" : "text-accent"}`}
        title={runtimeReady ? "Idle" : runtimeStatus === "busy" ? "Swarm or background work in progress" : "Session runner active"}
      >
        <Circle
          size={7}
          className={runtimeReady ? "fill-good text-good" : "fill-accent text-accent animate-pulse"}
        />
        {runtimeStatus}
      </span>
      {branch && <span className="flex items-center gap-1"><GitBranch size={10} />{branch}</span>}
      {showUsage && (
        <>
          <span className="w-px h-3 bg-edge/40" />
          <span className="flex items-center gap-1.5 text-muted/80" title="Process-wide token usage and estimated cost since app launch (not repo session spend in Swarm pane; survives backend restart; resets on full quit)">
            <Coins size={10} className="text-faint" />
            <span>{formatTokens(usage.tokens_used)} tok</span>
            {(() => {
              const cached = usage.tokens_cached || 0;
              const compacted = usage.tool_output_tokens_saved || 0;
              const cacheValue =
                (typeof usage.cache_savings_gross_usd === "number"
                  ? usage.cache_savings_gross_usd
                  : usage.cache_savings_usd || 0)
                + (usage.cache_saved_usd_swarm || 0);
              const savedUsd = listPriceValueTotal(usage);
              if (cached <= 0 && compacted <= 0 && savedUsd <= 0) return null;
              const delegationMeasured =
                usage.delegation_savings_basis === "actual_usage";
              const modelSelectionUsd =
                delegationMeasured || (usage.delegation_saved_usd || 0) > 0
                  ? usage.delegation_saved_usd || 0
                  : usage.routing_saved_usd || 0;
              const detail = [
                cached > 0
                  ? `${formatTokens(cached)} prompt tokens served from cache${
                      cacheValue > 0 ? ` (~${formatCost(cacheValue)} prompt-cache value)` : ""
                    }`
                  : "",
                compacted > 0
                  ? `${formatTokens(compacted)} tool-output tokens compacted away${
                      usage.tool_output_savings_usd ? ` (~${formatCost(usage.tool_output_savings_usd)})` : ""
                    }`
                  : "",
                modelSelectionUsd
                  ? `model selection value vs frontier-equivalent list price (~${formatCost(modelSelectionUsd)}${
                      usage.routing_savings_basis === "estimated" && !delegationMeasured
                        ? ", estimate"
                        : ""
                    })`
                  : "",
              ].filter(Boolean).join("  ·  ");
              return (
                <span
                  className="inline-flex items-center gap-1 px-1.5 py-px rounded-full bg-good/10 border border-good/20 text-good/90"
                  title={`List-price value from model selection, prompt-cache, and compaction (additive, not overlapping cash refunds): ${detail}`}
                >
                  <span className="text-good/60" aria-hidden="true">&#8595;</span>
                  {savedUsd > 0 ? `${formatCost(savedUsd)} saved` : `${formatTokens(cached + compacted)} saved`}
                </span>
              );
            })()}
            {/* The estimated cost is now a click/hover trigger for a compact
                routing-value breakdown (why this model / what it saved). It
                stays a plain figure when there is nothing meaningful to expand. */}
            <span className="relative inline-flex items-center gap-1" ref={costRef}>
              <button
                type="button"
                onClick={() => setCostOpen((v) => !v)}
                title={
                  !spendIsEstimated(usage)
                    ? "Process-wide billed spend since app launch (provider usage.cost) -- click for the full cost breakdown"
                    : usage.cost_source === "plan_estimated"
                      ? "Process-wide plan-credit estimate since app launch (subscription pilots; not an API receipt) -- click for the full cost breakdown"
                      : usage.price_source === "default"
                        ? "Process-wide estimated spend using default rates (live/catalog pricing unavailable) -- click for the full cost breakdown"
                        : "Process-wide estimated spend since app launch -- click for the full cost breakdown (Swarm pane shows per-repo session spend)"
                }
                className="inline-flex items-center gap-1 px-1.5 py-px rounded-full bg-panel2 border border-edge text-txt/90 font-medium hover:border-good/40 hover:text-good transition cursor-pointer"
              >
                {spendIsEstimated(usage) ? "~" : ""}
                {formatCost(usage.est_cost_usd)}
              </button>
              <span className="text-faint/70 normal-case font-sans tracking-normal">process</span>
              {costOpen && (
                <div className="absolute bottom-full right-0 mb-1.5 z-50">
                  <CostBreakdown
                    data={{
                      tokens_used: usage.tokens_used,
                      est_cost_usd: usage.est_cost_usd,
                      cost_source: usage.cost_source,
                      price_source: usage.price_source,
                      estimated: usage.estimated,
                      tokens_cached: usage.tokens_cached,
                      cache_savings_usd: usage.cache_savings_usd,
                      cache_savings_gross_usd: usage.cache_savings_gross_usd,
                      cache_savings_basis: usage.cache_savings_basis,
                      routing_saved_usd: usage.routing_saved_usd,
                      routing_savings_basis: usage.routing_savings_basis,
                      routing_tokens_compared: usage.routing_tokens_compared,
                      delegation_saved_usd: usage.delegation_saved_usd,
                      delegation_savings_basis: usage.delegation_savings_basis,
                      delegation_tokens_compared: usage.delegation_tokens_compared,
                      cache_saved_usd_swarm: usage.cache_saved_usd_swarm,
                      tool_output_tokens_saved: usage.tool_output_tokens_saved,
                      tool_output_savings_usd: usage.tool_output_savings_usd,
                      history_compactions: usage.history_compactions,
                      history_tokens_saved: usage.history_tokens_saved,
                      spill_count: usage.spill_count,
                      spill_chars: usage.spill_chars,
                      evals_recorded: usage.evals_recorded,
                      evals_failed: usage.evals_failed,
                      memory_layers: usage.memory_layers,
                      compaction_advice: usage.compaction_advice,
                      history_compaction_ran: usage.history_compaction_ran,
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
        <span className="flex items-center gap-1.5 px-2 py-0.5 rounded bg-amber-500/10 border border-amber-500/30 text-amber-300/90">
          <span>{toast.message}</span>
          {toast.actionLabel && toast.actionEvent ? (
            <button
              type="button"
              className="underline font-semibold hover:text-amber-200 focus-visible:outline focus-visible:outline-1 focus-visible:outline-amber-300 rounded-sm"
              onClick={() => {
                window.dispatchEvent(new CustomEvent(toast.actionEvent!));
                setToast(null);
              }}
            >
              {toast.actionLabel}
            </button>
          ) : null}
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
      <span className="text-muted/60">{isDesktop() ? "desktop" : "web"}</span>
    </div>
  );
}
