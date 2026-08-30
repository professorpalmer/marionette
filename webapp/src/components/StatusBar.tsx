import { useEffect, useRef, useState } from "react";
import {
  Circle,
  GitBranch,
  Cpu,
  PanelLeft,
  PanelRight,
  Coins,
  ArrowUpCircle,
  RefreshCw,
  Zap,
  Target,
  Pause,
  Play,
  Check,
  X,
} from "lucide-react";
import { api, type Config, type EconomicsData, type SessionGoal, type SessionState } from "../lib/api";
import {
  subscribeTaskProfile,
  taskProfileTitle,
  type TaskProfileChip,
} from "../lib/taskProfileChrome";
import { isDesktop } from "../lib/transport";
import { usePolling } from "../lib/usePolling";

import { sanitizeUpdateMessage } from "../lib/updateMessages";
import { shortPilotModelLabel } from "../lib/turnProgress";
import type { UpdateAvailability } from "./UpdateBanner";

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

/** Visible footer label — never paint raw FooterRuntimeStatus enums. */
export function footerRuntimeStatusLabel(status: FooterRuntimeStatus): string {
  if (status === "thinking") return "Thinking…";
  if (status === "busy") return "Busy";
  return "Idle";
}

/** Sticky session GOAL is chip-ready when it has text and is not cleared. */
export function sessionGoalForChip(goal?: SessionGoal | null): SessionGoal | null {
  if (!goal) return null;
  const text = (goal.text || "").trim();
  if (!text) return null;
  const status = String(goal.status || "cleared").toLowerCase();
  if (status === "cleared") return null;
  return { ...goal, text, status };
}

function truncateGoalText(text: string, max = 36): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1)}…`;
}

// Bottom status strip (Hermes shell/statusbar pattern): runtime health, active
// workspace branch, pilot model, spend, and shell toggles. Job inventory lives
// in LeftRail SESSION JOBS -- a footer total was stale across dir swaps and
// disagreed with the scoped list, so it was removed. Narrow widths hide
// secondary labels via container queries instead of overlapping the clusters.
export default function StatusBar({ config, update, leftOpen, rightOpen, onToggleLeft, onToggleRight, onOpenEconomics }: {
  config: Config | null;
  update: UpdateAvailability | null;
  leftOpen: boolean; rightOpen: boolean;
  onToggleLeft: () => void; onToggleRight: () => void;
  onOpenEconomics: () => void;
}) {
  const [branch, setBranch] = useState("");
  const [sessionEconomics, setSessionEconomics] = useState<EconomicsData | null>(null);
  const [apply, setApply] = useState<{ stage: string; message: string; percent: number | null } | null>(null);
  const [toast, setToast] = useState<{
    message: string;
    actionLabel?: string;
    actionEvent?: string;
  } | null>(null);
  const [sessionState, setSessionState] = useState<SessionState | null>(null);
  const [taskProfile, setTaskProfile] = useState<TaskProfileChip | null>(null);
  const [goalBusy, setGoalBusy] = useState(false);

  const refreshSessionState = () =>
    api.getSessionState()
      .then((stateRes) => { if (stateRes) setSessionState(stateRes); })
      .catch(() => {});

  const applyGoalMutation = (
    action: () => Promise<{ ok: boolean; goal: SessionGoal }>,
  ) => {
    if (goalBusy) return;
    setGoalBusy(true);
    action()
      .then((res) => {
        if (!res?.goal) {
          void refreshSessionState();
          return;
        }
        setSessionState((prev) =>
          prev
            ? { ...prev, goal: res.goal }
            : { state: "idle", pending_swarms: false, goal: res.goal },
        );
      })
      .catch((err) => console.error("Session GOAL action failed", err))
      .finally(() => setGoalBusy(false));
  };

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

  useEffect(() => subscribeTaskProfile(setTaskProfile), []);


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
        if (p.stage === "idle") { setApply(null); return; }
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

  const economicsInFlight = useRef(false);
  const fetchSessionEconomics = () => {
    if (economicsInFlight.current) return;
    economicsInFlight.current = true;
    api.getEconomics("conversation", "all")
      .then((data) => {
        if (
          data?.counterfactual_source === "job_financial_reports"
          && data.counterfactual_status === "ok"
          && data.counterfactual
        ) {
          setSessionEconomics(data);
        } else {
          setSessionEconomics(null);
        }
      })
      .catch((err) => console.error("Failed to load session Economics in StatusBar", err))
      .finally(() => { economicsInFlight.current = false; });
  };

  useEffect(() => {
    api.workspaces().then((ws) => {
      const active = ws.find((w) => w.active);
      if (active) setBranch(active.name);
    }).catch(() => {});
  }, [config]);

  // Poll runner/pilot liveness (and sticky GOAL) so the footer reflects real
  // busy state. LeftRail uses the same endpoint on the same cadence for dots.
  usePolling(() => refreshSessionState(), 4000);

  const runtimeStatus = deriveFooterRuntimeStatus(sessionState);
  const runtimeReady = runtimeStatus === "ready";
  const sessionGoal = sessionGoalForChip(sessionState?.goal);
  usePolling(fetchSessionEconomics, 10000);

  useEffect(() => {
    const onRefresh = () => fetchSessionEconomics();
    const onSessionChanged = () => {
      setSessionEconomics(null);
      setTaskProfile(null);
      fetchSessionEconomics();
      // Session/view swaps carry a different sticky GOAL — refresh immediately
      // rather than waiting for the next 4s poll tick.
      void refreshSessionState();
    };
    window.addEventListener("harness-config-changed", onRefresh);
    window.addEventListener("harness-project-selected", onRefresh);
    window.addEventListener("harness-new-session", onSessionChanged);
    window.addEventListener("harness-usage-refresh", onRefresh);
    window.addEventListener("harness-session-changed", onSessionChanged);
    return () => {
      window.removeEventListener("harness-config-changed", onRefresh);
      window.removeEventListener("harness-project-selected", onRefresh);
      window.removeEventListener("harness-new-session", onSessionChanged);
      window.removeEventListener("harness-usage-refresh", onRefresh);
      window.removeEventListener("harness-session-changed", onSessionChanged);
    };
  }, []);

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

  const sessionReceipt = sessionEconomics?.counterfactual;
  const sessionCost = typeof sessionReceipt?.actual_cost_usd === "number"
    && Number.isFinite(sessionReceipt.actual_cost_usd)
    ? sessionReceipt.actual_cost_usd
    : null;
  const sessionSavings = typeof sessionReceipt?.avoided_usd === "number"
    && Number.isFinite(sessionReceipt.avoided_usd)
    ? sessionReceipt.avoided_usd
    : null;
  const sessionBasis = sessionReceipt?.spend_basis;
  const sessionCostLabel = sessionBasis === "plan"
    ? "Included"
    : sessionCost === null
      ? ""
      : `${sessionBasis === "estimated" || sessionBasis === "mixed" ? "~" : ""}${formatCost(sessionCost)}`;
  const openSessionEconomics = () => {
    // The panel can mount after this click's selection event. Keep the exact
    // destination available for that first render; the pane consumes it.
    (window as any).__pmPendingEconomicsSelection = {
      scope: "conversation",
      period: "all",
    };
    onOpenEconomics();
    window.setTimeout(() => {
      window.dispatchEvent(new CustomEvent("harness-economics-selection", {
        detail: { scope: "conversation", period: "all" },
      }));
    }, 0);
  };
  const driverLabel = shortPilotModelLabel(config?.driver) || "pilot";
  const driverProvider = config?.driver?.includes(":")
    ? config.driver.split(":")[0]
    : (config?.reach || "");

  return (
    <div className="shell-inset-footer status-bar px-3 h-7 text-[10px] text-muted select-none">
      <div className="status-bar-cluster status-bar-cluster-start">
      <button onClick={onToggleLeft} title="Toggle sessions panel (Ctrl/Cmd+B)"
        className={`p-0.5 rounded hover:bg-panel2 shrink-0 ${leftOpen ? "text-txt" : "text-muted"}`}><PanelLeft size={12} /></button>
      <button onClick={onToggleRight} title="Toggle floating panels (Ctrl/Cmd+J)"
        className={`p-0.5 rounded hover:bg-panel2 shrink-0 ${rightOpen ? "text-txt" : "text-muted"}`}><PanelRight size={12} /></button>
      <span className="w-px h-3 bg-edge shrink-0" />
      <span
        className={`flex items-center gap-1 shrink-0 ${runtimeReady ? "text-good" : "text-accent"}`}
        title={runtimeReady ? "Idle" : runtimeStatus === "busy" ? "Swarm or background work in progress" : "Session runner active"}
        data-runtime-status={runtimeStatus}
      >
        <Circle
          size={7}
          className={runtimeReady ? "fill-good text-good" : "fill-accent text-accent animate-pulse"}
        />
        <span className="status-bar-optional-sm">{footerRuntimeStatusLabel(runtimeStatus)}</span>
      </span>
      {branch && <span className="status-bar-optional-sm flex items-center gap-1"><GitBranch size={10} />{branch}</span>}
      {taskProfile && (
        <span
          data-testid="task-profile-chip"
          className="status-bar-optional-xs inline-flex items-center gap-1 px-1.5 py-px rounded-full bg-panel2 border border-edge text-txt/90"
          title={taskProfileTitle(taskProfile)}
        >
          <Zap size={10} className="shrink-0 text-accent" aria-hidden="true" />
          <span className="uppercase tracking-wide text-faint shrink-0 status-bar-optional-md">DEPTH</span>
          <span>{taskProfile.profile}</span>
        </span>
      )}
      {sessionGoal && (
        <span
          data-testid="session-goal-chip"
          className="status-bar-optional-xs inline-flex items-center gap-1 max-w-[240px] px-1.5 py-px rounded-full bg-panel2 border border-edge text-txt/90"
          title={`Session GOAL (${sessionGoal.status}): ${sessionGoal.text}`}
        >
          <Target size={10} className="shrink-0 text-accent" aria-hidden="true" />
          <span className="uppercase tracking-wide text-faint shrink-0">GOAL</span>
          <span className="truncate">{truncateGoalText(sessionGoal.text)}</span>
          {sessionGoal.status === "paused" ? (
            <span className="text-amber-300/80 shrink-0">paused</span>
          ) : null}
          {sessionGoal.status === "complete" ? (
            <span className="text-good/80 shrink-0">done</span>
          ) : null}
          {sessionGoal.status === "active" ? (
            <button
              type="button"
              disabled={goalBusy}
              title="Pause session GOAL"
              aria-label="Pause session GOAL"
              className="p-0.5 rounded hover:bg-panel hover:text-txt disabled:opacity-50"
              onClick={() => applyGoalMutation(() => api.pauseSessionGoal())}
            >
              <Pause size={9} />
            </button>
          ) : null}
          {sessionGoal.status === "paused" ? (
            <button
              type="button"
              disabled={goalBusy}
              title="Resume session GOAL"
              aria-label="Resume session GOAL"
              className="p-0.5 rounded hover:bg-panel hover:text-txt disabled:opacity-50"
              onClick={() => applyGoalMutation(() => api.resumeSessionGoal())}
            >
              <Play size={9} />
            </button>
          ) : null}
          {sessionGoal.status === "active" || sessionGoal.status === "paused" ? (
            <button
              type="button"
              disabled={goalBusy}
              title="Complete session GOAL"
              aria-label="Complete session GOAL"
              className="p-0.5 rounded hover:bg-panel hover:text-good disabled:opacity-50"
              onClick={() => applyGoalMutation(() => api.completeSessionGoal())}
            >
              <Check size={9} />
            </button>
          ) : null}
          <button
            type="button"
            disabled={goalBusy}
            title="Clear session GOAL"
            aria-label="Clear session GOAL"
            className="p-0.5 rounded hover:bg-panel hover:text-risk disabled:opacity-50"
            onClick={() => applyGoalMutation(() => api.clearSessionGoal())}
          >
            <X size={9} />
          </button>
        </span>
      )}
      {sessionCostLabel && (
        <>
          <span className="w-px h-3 bg-edge/40 shrink-0" />
          <span className="flex items-center gap-1.5 text-muted/80 min-w-0" title="This session · all time PM financial receipt">
            <Coins size={10} className="text-faint shrink-0" />
            {sessionSavings !== null && sessionSavings > 0 ? (
              <button
                type="button"
                onClick={openSessionEconomics}
                className="status-bar-optional-sm inline-flex items-center gap-1 px-1.5 py-px text-good/65 hover:text-good/80"
                title="Estimated savings for this session · all time — click to open Economics"
              >
                ~{formatCost(sessionSavings)} saved
              </button>
            ) : null}
            <button
              type="button"
              onClick={openSessionEconomics}
              title="Cost for this session · all time — click to open Economics"
              className="inline-flex items-center gap-1 px-1.5 py-px rounded-full bg-panel2 border border-edge text-txt/90 font-medium hover:border-edge hover:text-txt transition cursor-pointer"
            >
              {sessionCostLabel}
            </button>
            <span className="text-faint/70 normal-case font-sans tracking-normal status-bar-optional-lg">session</span>
          </span>
        </>
      )}
      </div>
      <div className="status-bar-cluster status-bar-cluster-end">
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
      <span className="status-bar-model" title={config?.driver || "pilot"}>
        <Cpu size={10} className="shrink-0" />
        <span>{driverLabel}</span>
      </span>
      {/* Show the ACTIVE model's provider (the driver spec's prefix), not the
          fallback reach. A "provider:model" driver routes through that provider;
          only a bare, unprefixed model actually falls back to reach. Showing
          reach unconditionally made e.g. anthropic:claude-opus read "openrouter". */}
      {driverProvider ? (
        <span className="status-bar-optional-md">{driverProvider}</span>
      ) : null}
      {config?.edit_engine === "agentic" && (
        <span
          className="flex items-center gap-1 text-good/80 shrink-0"
          title="Standalone: edits and swarms route directly through your provider keys -- no external agent CLI"
        >
          <Zap size={10} className="text-good/70" />
          <span className="status-bar-optional-md">standalone</span>
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
      <span className="text-muted/60 status-bar-optional-lg">{isDesktop() ? "desktop" : "web"}</span>
      </div>
    </div>
  );
}
