import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Loader2, CheckCircle2, XCircle, Circle, ChevronDown, ChevronRight, Cpu, Activity, Network, X } from "lucide-react";
import { api, jobArtifactList, type SwarmLive, type Job, type Artifact, type Task } from "../lib/api";
import { lastSelectedProjectRoot, panelOpacityClass, useProjectSwitching } from "../lib/panelTransition";
import { useStaleWhileRevalidate } from "../lib/useStaleWhileRevalidate";

// A clean, self-contained hover tooltip. The native `title=` tooltip renders as a
// large unstyled OS box that covers the tracker and never wraps sensibly; this
// draws a width-capped, styled bubble through a portal so it escapes the pane's
// overflow clip and clamps to the viewport instead of running off the right edge.
function Tooltip({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  const TIP_WIDTH = 340;
  const show = () => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const x = Math.max(8, Math.min(r.left, window.innerWidth - TIP_WIDTH - 8));
    setPos({ x, y: r.bottom + 6 });
  };
  const hide = () => setPos(null);
  return (
    <span ref={ref} className={className} onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}>
      {children}
      {pos && label &&
        createPortal(
          <div
            style={{ position: "fixed", left: pos.x, top: pos.y, maxWidth: TIP_WIDTH, zIndex: 200 }}
            className="pointer-events-none rounded-md border border-edge bg-panel2 px-2.5 py-1.5 text-[10.5px] leading-relaxed text-txt shadow-2xl whitespace-pre-wrap break-words"
          >
            {label}
          </div>,
          document.body,
        )}
    </span>
  );
}

type Status = "pending" | "in_progress" | "completed" | "failed" | "cancelled";

// Compact "how long ago" label for a job's last activity. Accepts epoch seconds
// or an ISO string (the backend sends created_at/updated_at as either). Returns
// "" when we can't parse a timestamp so the caller can omit the affordance.
function relativeSince(ts: unknown, nowMs: number): string {
  let t: number | null = null;
  if (typeof ts === "number" && isFinite(ts)) {
    // Heuristic: seconds vs milliseconds.
    t = ts > 1e12 ? ts : ts * 1000;
  } else if (typeof ts === "string" && ts) {
    const parsed = Date.parse(ts);
    if (!isNaN(parsed)) t = parsed;
  }
  if (t === null) return "";
  const secs = Math.max(0, Math.round((nowMs - t) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ago`;
}

// Turn the router's raw policy string ("policy=balanced: cheapest sufficient
// model whose capability_score (99) >= needed (50) (plan-billed...)") into one
// plain-language sentence. The raw string + a per-model rejection wall reads
// like an error dump; this reads like a decision.
function summarizeRouting(art: Artifact): string {
  const detail = typeof art.detail === "string" ? art.detail : "";
  const policy = (detail.match(/policy=(\w+)/) || [])[1] || "";
  const planBilled = /plan-billed|in-subscription/i.test(detail);
  const lead: Record<string, string> = {
    balanced: "Right-sized: cheapest model that clears the task's need",
    cheap: "Cheapest available model",
    quality: "Highest-capability model for the task",
    escalating: "Cheapest sufficient model, escalates if it stalls",
  };
  const base = lead[policy] || "Router pick";
  return planBilled ? `${base} \u00b7 plan-billed, no marginal cost` : base;
}

function jobStatus(j: Job): Status {
  const s = (j.status || "").toLowerCase();
  if (s.includes("complete") || s.includes("done")) return "completed";
  // User cancel / abort — distinct from ordinary worker failure chrome.
  if (
    s.includes("cancel")
    || s.includes("user-aborted")
    || s.includes("user_aborted")
  ) {
    return "cancelled";
  }
  if (
    s.includes("fail")
    || s.includes("error")
    || s.includes("stall")
    || s.includes("dead")
  ) {
    return "failed";
  }
  if (s.includes("run") || s.includes("progress") || s.includes("active")) return "in_progress";
  return "pending";
}

// A job is "finished" once it can no longer change -- completed, failed, or
// cancelled. These are the runs we fold away so a long session doesn't stack
// into a wall.
function isTerminal(j: Job): boolean {
  const st = jobStatus(j);
  return st === "completed" || st === "failed" || st === "cancelled";
}

function taskState(t: Task): "running" | "done" | "fail" | "idle" {
  const s = (t.status || "").toLowerCase();
  if (s.includes("run") || s.includes("progress") || s.includes("active")) return "running";
  if (s.includes("complete") || s.includes("done")) return "done";
  if (s.includes("fail") || s.includes("cancel") || s.includes("error")) return "fail";
  return "idle";
}

// Dismissed job ids are VIEW state, never a deletion: the durable Puppetmaster
// store (and the PM dashboard) remain the archive, so anything hidden here is
// still recallable there. Persisted per active repo so clearing the tracker in
// one project does not hide jobs when viewing another. Soft-capped per repo so
// a very long-lived install can't grow it unbounded.
const DISMISS_KEY_V1 = "swarm.dismissed.v1";
const DISMISS_KEY = "swarm.dismissed.v2";
const DISMISS_CAP = 2000;

type DismissStore = Record<string, string[]>;

function repoDismissKey(repo?: string): string {
  return repo || "__default__";
}

function readDismissStore(): DismissStore {
  try {
    const raw = localStorage.getItem(DISMISS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as DismissStore;
      }
    }
  } catch {
    // Fall through to v1 migration / empty store.
  }
  return migrateDismissV1();
}

/** One-time import of the pre-Wave-4 global blob into the unscoped default view. */
function migrateDismissV1(): DismissStore {
  try {
    const raw = localStorage.getItem(DISMISS_KEY_V1);
    if (!raw) return {};
    const arr = JSON.parse(raw);
    const ids = Array.isArray(arr)
      ? arr.filter((id): id is string => typeof id === "string")
      : [];
    const store: DismissStore = ids.length > 0 ? { [repoDismissKey()]: ids } : {};
    try {
      if (ids.length > 0) localStorage.setItem(DISMISS_KEY, JSON.stringify(store));
      localStorage.removeItem(DISMISS_KEY_V1);
    } catch {
      // localStorage full/unavailable -- in-memory dismiss still works.
    }
    return store;
  } catch {
    return {};
  }
}

function loadDismissed(repo?: string): Set<string> {
  const store = readDismissStore();
  const ids = store[repoDismissKey(repo)] || [];
  return new Set(Array.isArray(ids) ? ids : []);
}

function saveDismissed(repo: string | undefined, ids: Set<string>): void {
  try {
    const store = readDismissStore();
    store[repoDismissKey(repo)] = [...ids].slice(-DISMISS_CAP);
    localStorage.setItem(DISMISS_KEY, JSON.stringify(store));
  } catch {
    // localStorage full/unavailable -- dismissal still works for this session.
  }
}

// Cheap, render-relevant fingerprint of a live-swarm payload. During a big swarm
// the payload can be ~1MB; JSON.stringify-diffing it (or blindly setData every
// poll) re-renders the whole tree for no delta and blocks the main thread. We
// hash only the fields the UI actually draws -- job/task status, counts, tokens,
// cost, savings, compact-token meters, dead-run failure text, activity
// timestamps, and artifact headlines -- so an unchanged poll skips the re-render.
function swarmSignature(res: SwarmLive | null): string {
  if (!res) return "";
  const parts: string[] = [];
  for (const j of res.jobs || []) {
    const tasks = j.tasks || [];
    const arts = jobArtifactList(j);
    parts.push(
      `${j.id}:${j.status}:${tasks.length}:${arts.length}` +
      `:${j.tokens ?? 0}:${(j.est_cost_usd ?? 0).toFixed(4)}` +
      `:${j.tool_output_tokens_saved ?? 0}` +
      `:${(j.routing_saved_usd ?? 0).toFixed(4)}` +
      `:${(j.cache_saved_usd ?? 0).toFixed(4)}` +
      `:${(j.tool_output_savings_usd ?? 0).toFixed(4)}` +
      `:${j.source ?? "harness"}` +
      `:${j.dead_run_failure ?? ""}:${j.updated_at ?? ""}`,
    );
    for (const t of tasks) {
      parts.push(
        `${t.id}=${t.status}:${t.tokens ?? 0}:${(t.est_cost_usd ?? 0).toFixed(4)}`,
      );
    }
    for (const a of arts) {
      parts.push(
        `A:${(a.type || "").slice(0, 8)}:${(a.headline || "").slice(0, 120)}:${a.result ?? ""}`,
      );
    }
  }
  const s = res.session;
  if (s) {
    parts.push(
      `S:${s.driver ?? ""}:${s.tokens_used ?? 0}:${(s.est_cost_usd ?? 0).toFixed(4)}` +
      `:${(s.routing_saved_usd ?? 0).toFixed(4)}` +
      `:${(s.cache_saved_usd_swarm ?? 0).toFixed(4)}` +
      `:${(s.cache_savings_usd ?? 0).toFixed(4)}` +
      `:${s.tool_output_tokens_saved ?? 0}` +
      `:${(s.tool_output_savings_usd ?? 0).toFixed(4)}`,
    );
  }
  return parts.join("|");
}

// Findings arrive one-per-worker and repeat heavily: every agentic worker emits
// a VERIFICATION artifact echoing the same task instruction, so a 5-worker swarm
// shows the identical line 5x. Collapse exact (type + headline) duplicates into a
// single row with an xN badge, and sort real signal (RISK/BUG/DECISION) above
// process noise (VERIFICATION) so substance reads first.
type FindingRow = { art: Artifact; count: number };
function dedupeFindings(arts: Artifact[]): FindingRow[] {
  const rows = new Map<string, FindingRow>();
  for (const art of arts) {
    const key = `${(art.type || "").toUpperCase()}::${(art.headline || "").trim().toLowerCase()}`;
    const hit = rows.get(key);
    if (hit) hit.count += 1;
    else rows.set(key, { art, count: 1 });
  }
  const rank = (t?: string) => {
    const u = (t || "").toUpperCase();
    if (u === "RISK" || u === "BUG") return 0;
    if (u === "DECISION" || u === "FINDING") return 1;
    if (u === "VERIFICATION") return 3;
    return 2;
  };
  return [...rows.values()].sort((a, b) => rank(a.art.type) - rank(b.art.type));
}

// A 5-worker swarm stores two ROUTING artifacts per task (created_by="router"
// plus "router-fallback", sometimes "router-escalation"). Cost accounting already
// ignores non-router rows; display should show ONE card per task — the final
// choice that actually ran. Prefer escalation > fallback > router > other.
function routingCreatedByRank(createdBy?: string): number {
  switch (createdBy) {
    case "router-escalation": return 3;
    case "router-fallback": return 2;
    case "router": return 1;
    default: return 0;
  }
}

function dedupeRouting(arts: Artifact[]): Artifact[] {
  const groups = new Map<string, Artifact>();
  for (const art of arts) {
    const taskId = (art.task_id || "").trim();
    const key = taskId
      ? `task:${taskId}`
      : `model:${(art.model || "").trim().toLowerCase()}`;
    const existing = groups.get(key);
    if (!existing || routingCreatedByRank(art.created_by) > routingCreatedByRank(existing.created_by)) {
      groups.set(key, art);
    }
  }
  return [...groups.values()];
}

// A "dead run": the job reads complete, but every artifact it produced is a
// failed verdict -- e.g. all workers fast-failed with no_model before doing any
// work. Older Puppetmaster versions stitched these into COMPLETE, so the
// tracker painted a fully dead swarm as a healthy green "done" at $0 (the
// worst failure mode: it looks like success). Prefer the server stamp
// (computed before the live payload is slimmed); fall back to client scan
// when the full artifact list is still present.
function jobDeadRunFailure(j: Job): string | null {
  if (typeof j.dead_run_failure === "string" && j.dead_run_failure) {
    return j.dead_run_failure;
  }
  if (j.dead_run_failure === null) return null;
  if (jobStatus(j) !== "completed") return null;
  // Slim live payloads omit FINDING rows; never infer dead-run from a partial list.
  if (j.artifacts_complete === false) return null;
  const arts = jobArtifactList(j);
  if (arts.length === 0) return null;
  const failed = arts.filter((a) => (a.result || "").toLowerCase() === "failed" || (a.result || "").toLowerCase() === "blocked");
  if (failed.length !== arts.length) return null;
  return failed.find((a) => a.failure)?.failure || "workers failed";
}

/** Merge a fresh /swarm/live poll into cached state without wiping expanded full artifacts. */
function mergeSwarmLive(prev: SwarmLive | null | undefined, next: SwarmLive): SwarmLive {
  if (!prev?.jobs?.length) return next;
  const prevById = new Map(prev.jobs.map((j) => [j.id, j]));
  return {
    ...next,
    jobs: (next.jobs || []).map((j) => {
      const old = prevById.get(j.id);
      if (!old) return j;
      // Keep a previously hydrated full artifact list when the poll is slim.
      if (j.artifacts_complete === false && old.artifacts_complete === true) {
        return {
          ...j,
          artifacts: old.artifacts,
          artifacts_complete: true,
          tasks: (j.tasks || []).map((t) => {
            const ot = (old.tasks || []).find((x) => x.id === t.id);
            if (ot?.instruction && !t.instruction) {
              return { ...t, instruction: ot.instruction };
            }
            return t;
          }),
        };
      }
      return j;
    }),
  };
}

function jobCost(j: Job): number {
  return Number(j.est_cost_usd || 0);
}

function jobTokens(j: Job): number {
  return Number(j.tokens || 0);
}

function jobCompactTokens(j: Job): number {
  return Number(j.tool_output_tokens_saved || 0);
}

function formatCost(cost: number, estimated?: boolean): string {
  if (!(cost > 0)) return "$0";
  const body = `$${cost.toFixed(4)}`;
  return estimated ? `~${body}` : body;
}

function positiveUsd(n?: number): number {
  return typeof n === "number" && isFinite(n) && n > 0 ? n : 0;
}

type SavingsParts = {
  routing: number;
  delegation: number;
  modelSelection: number;
  cache: number;
  compact: number;
  total: number;
};

function jobSavings(j: Job): SavingsParts {
  const routing = positiveUsd(j.routing_saved_usd);
  const delegation = positiveUsd(j.delegation_saved_usd);
  const delegationMeasured = j.delegation_savings_basis === "actual_usage";
  const modelSelection =
    delegationMeasured || delegation > 0 ? delegation : routing;
  const cache = positiveUsd(j.cache_saved_usd);
  const compact = positiveUsd(j.tool_output_savings_usd);
  return {
    routing,
    delegation,
    modelSelection,
    cache,
    compact,
    total: modelSelection + cache + compact,
  };
}

function savingsDetail(parts: SavingsParts): string {
  return [
    parts.modelSelection > 0
      ? `model selection value vs frontier-equivalent list price (~${formatCost(parts.modelSelection)})`
      : "",
    parts.cache > 0 ? `prompt-cache value (~${formatCost(parts.cache)})` : "",
    parts.compact > 0 ? `tool-output compaction (~${formatCost(parts.compact)})` : "",
  ].filter(Boolean).join("  ·  ");
}

function SavingsChip({ parts, className }: { parts: SavingsParts; className?: string }) {
  if (parts.total <= 0) return null;
  return (
    <span
      className={`inline-flex items-center gap-0.5 px-1 py-px rounded-full bg-good/10 border border-good/20 text-good/80 tabular-nums ${className ?? ""}`}
      title={`List-price value from model selection, prompt-cache, and compaction (additive): ${savingsDetail(parts)}`}
    >
      <span className="text-good/60" aria-hidden="true">{"\u2193"}</span>
      {formatCost(parts.total)} saved
    </span>
  );
}

// The four visible phases of a swarm's life. A job advances left-to-right; the
// strip fills behind the active phase so a running swarm reads as *moving*
// instead of a static spinner. "failed" paints the reached phase red.
const PHASES = ["dispatched", "routing", "workers", "done"] as const;

function jobPhase(j: Job): { key: string; label: string; index: number; failed: boolean } {
  const st = jobStatus(j);
  const tasks = j.tasks || [];
  const total = tasks.length;
  const running = tasks.filter((t) => taskState(t) === "running").length;
  const doneCount = tasks.filter((t) => taskState(t) === "done").length;
  const hasRouting = jobArtifactList(j).some((a) => (a.type || "").toUpperCase() === "ROUTING");

  if (st === "failed" || st === "cancelled") {
    const reached = total > 0 ? 2 : hasRouting ? 1 : 0;
    const label = st === "cancelled" ? "cancelled" : "failed";
    return { key: label, label, index: reached, failed: true };
  }
  if (st === "completed") return { key: "done", label: "done", index: 3, failed: false };
  if (total > 0 && running > 0) return { key: "workers", label: `running ${doneCount}/${total}`, index: 2, failed: false };
  if (total > 0) return { key: "workers", label: `${total} worker${total > 1 ? "s" : ""}`, index: 2, failed: false };
  if (hasRouting) return { key: "routing", label: "routing", index: 1, failed: false };
  return { key: "dispatched", label: "dispatched", index: 0, failed: false };
}

function PhaseStrip({ job, phase }: { job: Job; phase?: ReturnType<typeof jobPhase> }) {
  const { index, failed, key } = phase ?? jobPhase(job);
  const active = key !== "done" && !failed;
  return (
    <div className="flex items-center gap-1 mt-1.5" title={PHASES.join(" -> ")}>
      {PHASES.map((_, i) => {
        const reached = i <= index;
        const isActiveSeg = i === index && active;
        const color = failed && i === index
          ? (key === "cancelled" ? "bg-muted" : "bg-risk")
          : reached
          ? (key === "done" ? "bg-good" : "bg-accent")
          : "bg-edge/60";
        return (
          <div
            key={i}
            className={`h-1 flex-1 rounded-full transition-all ${color} ${isActiveSeg ? "animate-pulse" : ""}`}
          />
        );
      })}
    </div>
  );
}

function WorkerProgress({ tasks }: { tasks: Task[] }) {
  const total = tasks.length;
  if (total === 0) return null;
  const done = tasks.filter((t) => taskState(t) === "done").length;
  const failed = tasks.filter((t) => taskState(t) === "fail").length;
  const donePct = Math.round((done / total) * 100);
  const failedPct = Math.round((failed / total) * 100);
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-panel2 border border-edge/50 rounded-full overflow-hidden flex">
        {donePct > 0 && (
          <div className="h-full bg-good transition-all duration-500" style={{ width: `${donePct}%` }} />
        )}
        {failedPct > 0 && (
          <div className="h-full bg-risk transition-all duration-500" style={{ width: `${failedPct}%` }} />
        )}
      </div>
      <span className={`text-[9px] tabular-nums shrink-0 ${failed > 0 ? "text-risk/80" : "text-faint"}`}>
        {done + failed}/{total}{failed > 0 ? ` · ${failed} failed` : ""}
      </span>
    </div>
  );
}

export default function SwarmPane() {
  // Seeded so an instance mounting after the project-selected event scopes
  // to the same repo as its siblings instead of the unscoped default view.
  const [selectedProjectRoot, setSelectedProjectRoot] = useState(lastSelectedProjectRoot);
  const projectSwitching = useProjectSwitching();
  const [expandedJobs, setExpandedJobs] = useState<Record<string, boolean>>({});
  const [expandedAlts, setExpandedAlts] = useState<Record<string, boolean>>({});
  const [expandedTasks, setExpandedTasks] = useState<Record<string, boolean>>({});
  const [expandedFindings, setExpandedFindings] = useState<Record<string, boolean>>({});
  // Findings section open/closed per job. Default open (missing key); user toggle sticks.
  const [findingsOpen, setFindingsOpen] = useState<Record<string, boolean>>({});
  const scopedRepo = selectedProjectRoot || undefined;
  const scopedRepoRef = useRef(scopedRepo);
  scopedRepoRef.current = scopedRepo;

  const [dismissed, setDismissed] = useState<Set<string>>(() => loadDismissed(scopedRepo));
  const [finishedOpen, setFinishedOpen] = useState(false);
  // Job ids we have asked the backend to cancel. Held in local view state so the
  // row can show a subtle 'cancelling...' affordance immediately, before the next
  // poll reflects the terminal 'cancelled' status from /api/swarm/live.
  const [cancelling, setCancelling] = useState<Set<string>>(new Set());
  // Job ids currently fetching full artifacts after expand (slim live payload).
  const [loadingArts, setLoadingArts] = useState<Set<string>>(new Set());
  // Bumped every second so relative "last activity" times re-render while a job
  // runs, making a live worker visibly move rather than freeze between polls.
  const [nowTick, setNowTick] = useState(() => Date.now());

  const toggleTask = (id: string) => setExpandedTasks((p) => ({ ...p, [id]: !p[id] }));
  const toggleFinding = (id: string) => setExpandedFindings((p) => ({ ...p, [id]: !p[id] }));

  useEffect(() => {
    setDismissed(loadDismissed(scopedRepo));
  }, [scopedRepo]);

  useEffect(() => {
    saveDismissed(scopedRepoRef.current, dismissed);
  }, [dismissed]);

  useEffect(() => {
    const onProject = (e: Event) => {
      const path = (e as CustomEvent<string>).detail;
      if (typeof path === "string") setSelectedProjectRoot(path);
    };
    window.addEventListener("harness-project-selected", onProject);
    return () => window.removeEventListener("harness-project-selected", onProject);
  }, []);

  // Holds latest live payload so the SWR fetcher / poll can merge without
  // wiping artifacts hydrated via /api/artifacts on expand.
  const dataRef = useRef<SwarmLive | null | undefined>(undefined);

  const {
    data,
    isValidating,
    isTransitioning,
    isShowingStale,
    mutate,
  } = useStaleWhileRevalidate<SwarmLive | null>(
    `swarm:${scopedRepo || "__default__"}`,
    async () => {
      const res = await api.swarmLive(scopedRepo);
      return mergeSwarmLive(dataRef.current ?? undefined, res);
    },
  );
  dataRef.current = data;

  const loadingArtsRef = useRef(loadingArts);
  loadingArtsRef.current = loadingArts;

  const applyLive = useCallback((res: SwarmLive) => {
    mutate(mergeSwarmLive(dataRef.current ?? undefined, res));
  }, [mutate]);

  // Hydrate full artifacts when a slim finished card expands.
  const ensureFullArtifacts = useCallback((job: Job) => {
    if (job.artifacts_complete !== false) return;
    if (loadingArtsRef.current.has(job.id)) return;
    setLoadingArts((prev) => new Set(prev).add(job.id));
    api.artifacts(job.id)
      .then((arts) => {
        const prev = dataRef.current;
        if (!prev) return;
        mutate({
          ...prev,
          jobs: (prev.jobs || []).map((j) =>
            j.id === job.id
              ? { ...j, artifacts: Array.isArray(arts) ? arts : [], artifacts_complete: true }
              : j,
          ),
        });
      })
      .catch(() => {
        // Leave slim payload; user can collapse/re-expand to retry.
      })
      .finally(() => {
        setLoadingArts((prev) => {
          const next = new Set(prev);
          next.delete(job.id);
          return next;
        });
      });
  }, [mutate]);

  const lastSigRef = useRef("");

  // Drive a 1s clock only while something is running so relative "last activity"
  // labels advance live. Stops ticking when nothing is running to avoid needless
  // re-renders.
  const hasLiveJob = (data?.jobs || []).some((j) => jobStatus(j) === "in_progress");
  useEffect(() => {
    if (!hasLiveJob) return;
    // PERF: 5s, not 1s. This tick exists only to refresh relative "3s ago /
    // 2m ago" labels, which do not need per-second precision -- and each tick
    // re-renders the whole SwarmPane (every job row + phase strip). At 1s that
    // was a steady re-render tax stacked on top of a long transcript while a
    // swarm ran. Also pause it entirely while the app is backgrounded.
    let id: number | undefined;
    const start = () => {
      if (id == null && !document.hidden) id = window.setInterval(() => setNowTick(Date.now()), 5000);
    };
    const stop = () => { if (id != null) { window.clearInterval(id); id = undefined; } };
    const onVis = () => { if (document.hidden) stop(); else start(); };
    start();
    document.addEventListener("visibilitychange", onVis);
    return () => { stop(); document.removeEventListener("visibilitychange", onVis); };
  }, [hasLiveJob]);

  // Fire-and-refetch cancel. Best-effort on the backend (a provider call in a
  // Python thread cannot be force-killed), so the row shows 'cancelling...' until
  // the next poll surfaces the terminal 'cancelled' state.
  const cancelJob = async (id: string) => {
    setCancelling((prev) => new Set(prev).add(id));
    let accepted = false;
    try {
      const res = await api.swarmCancel(id);
      accepted = !!res.ok;
    } catch {
      // Fall through -- treated as not accepted below.
    }
    if (!accepted) {
      // Restore the Kill button so the user can retry. Leaving the id in the
      // set rendered a permanent 'cancelling...' with no affordance to retry.
      setCancelling((prev) => { const next = new Set(prev); next.delete(id); return next; });
    }
    try {
      const res = await api.swarmLive(scopedRepo);
      applyLive(res);
    } catch {
      // Ignore; the poll loop will refetch shortly.
    }
  };

  // Drop cancel markers once their job leaves in_progress, so the set cannot
  // accumulate stale ids across job lifetimes.
  useEffect(() => {
    const live = data?.jobs;
    if (!live || cancelling.size === 0) return;
    const stillRunning = new Set(live.filter((j) => jobStatus(j) === "in_progress").map((j) => j.id));
    const survivors = [...cancelling].filter((id) => stillRunning.has(id));
    if (survivors.length !== cancelling.size) setCancelling(new Set(survivors));
  }, [data]);

  // Self-scheduling poll (not setInterval) so a new request is only ever queued
  // AFTER the previous one settles. The old fixed 2s interval fired regardless of
  // whether the last request had returned: during an active swarm the backend is
  // slow (every /swarm/live formats all artifacts and holds a worker slot), so
  // requests piled up, each grabbed a slot, saturated the server, and starved
  // every other panel's fetch -- that was the "loads in chunks / can't X out of
  // settings" jank. This loop guarantees at most one in-flight poll, pauses when
  // the window is hidden, backs off when the backend is under load, and skips the
  // re-render when nothing changed.
  useEffect(() => {
    lastSigRef.current = swarmSignature(data ?? null);
  }, [scopedRepo]);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    let inFlight = false;

    const schedule = (ms: number) => {
      if (active) timer = window.setTimeout(tick, ms);
    };

    const tick = () => {
      if (document.hidden) { schedule(3000); return; }
      if (inFlight) { schedule(500); return; }
      inFlight = true;
      const startedAt = performance.now();
      api.swarmLive(scopedRepo)
        .then((res) => {
          if (!active) return;
          const sig = swarmSignature(res);
          if (sig !== lastSigRef.current) {
            lastSigRef.current = sig;
            applyLive(res);
          }
          const hasRunning = (res.jobs || []).some((j) => jobStatus(j) === "in_progress");
          const elapsed = performance.now() - startedAt;
          const base = hasRunning ? 2000 : 5000;
          const backoff = elapsed > 1500 ? Math.min(elapsed, 8000) : 0;
          schedule(base + backoff);
        })
        .catch(() => { if (active) schedule(8000); })
        .finally(() => { inFlight = false; });
    };

    tick();
    const onVisible = () => {
      if (!document.hidden && !inFlight) { window.clearTimeout(timer); tick(); }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      active = false;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [scopedRepo, applyLive]);

  const allJobs = data?.jobs || [];
  // Clear/dismiss is archive chrome for finished runs only. Live (and pending)
  // jobs must stay visible even if their id was previously dismissed — otherwise
  // a CLI-started swarm looks "gone" while workers are still running, and pilots
  // burn tokens inventing recovery paths.
  const visibleJobs = allJobs.filter((j) => !isTerminal(j) || !dismissed.has(j.id));
  const running = visibleJobs.filter((j) => !isTerminal(j));
  const finished = visibleJobs.filter((j) => isTerminal(j));
  // Ordinary failures + dead-run stamps count as failed; true user cancels do not.
  const failedCount = finished.filter((j) =>
    jobDeadRunFailure(j) !== null || jobStatus(j) === "failed",
  ).length;
  const cancelledCount = finished.filter((j) =>
    jobDeadRunFailure(j) === null && jobStatus(j) === "cancelled",
  ).length;
  const completedCount = finished.length - failedCount - cancelledCount;
  const runningCount = running.filter((j) => jobStatus(j) === "in_progress").length;
  const anyRunning = runningCount > 0;

  const dismissJob = (id: string) =>
    setDismissed((prev) => {
      const target = allJobs.find((j) => j.id === id);
      if (target && !isTerminal(target)) return prev;
      return new Set(prev).add(id);
    });
  const clearFinished = () =>
    setDismissed((prev) => {
      const next = new Set(prev);
      for (const j of finished) next.add(j.id);
      return next;
    });
  const restoreDismissed = () => setDismissed(new Set());
  const hiddenCount = allJobs.filter((j) => isTerminal(j) && dismissed.has(j.id)).length;


  // One card renderer, reused by both the running list and the Finished
  // accordion. Defined in-scope so it closes over the expand/dismiss state
  // instead of threading a dozen props.
  const renderJob = (j: Job) => {
    const deadRunFailure = jobDeadRunFailure(j);
    // dead_run_failure is authoritative: paint as failed, never as a user cancel.
    const st: Status = deadRunFailure ? "failed" : jobStatus(j);
    const manualExpanded = expandedJobs[j.id];
    const isExpanded = manualExpanded !== undefined ? manualExpanded : (st === "in_progress");
    const phase = deadRunFailure
      ? { key: "failed", label: "failed", index: 2, failed: true }
      : jobPhase(j);

    const artifacts = jobArtifactList(j);
    const routingArts = dedupeRouting(
      artifacts.filter((a: Artifact) => (a.type || "").toUpperCase() === "ROUTING"),
    );
    const streamArts = artifacts.filter((a: Artifact) => (a.type || "").toUpperCase() !== "ROUTING");
    const tasks = j.tasks || [];
    // Prefer the deduped final routing card (fallback/escalation wins) over the
    // job.model field so a stale initial router pick never badges the header.
    // Local agentic jobs stamp model as "agentic/<id>"; strip the engine prefix
    // so the badge shows the real model next to the separate adapter chip.
    const routerModel = routingArts.find((a: Artifact) => a.model)?.model || j.model || "";
    const workerCount = tasks.length;
    const adapter = j.adapter || tasks[0]?.adapter || "";
    const displayModel = (routerModel || "")
      .replace(/^(?:agentic|native)\//i, "")
      .trim() || adapter;
    const terminal = isTerminal(j);
    const savings = jobSavings(j);

    const toggle = () => {
      const next = !isExpanded;
      setExpandedJobs((prev) => ({ ...prev, [j.id]: next }));
      if (next) ensureFullArtifacts(j);
    };

    return (
      <div
        key={j.id}
        // shrink-0 is load-bearing: as a flex child of the flex-col scroll list,
        // an overflow-hidden card is allowed to shrink BELOW its content, so it
        // collapsed and clipped its own findings instead of pushing the list into
        // overflow. Pinning shrink-0 keeps the card at full content height so the
        // list actually scrolls.
        className={`shrink-0 rounded-md border bg-panel2/30 flex flex-col overflow-hidden transition-colors ${
          st === "in_progress"
            ? "border-accent/30"
            : st === "completed"
            ? "border-good/25"
            : st === "failed"
            ? "border-risk/25"
            : st === "cancelled"
            ? "border-muted/40"
            : "border-edge"
        }`}
      >
        {/* Header row. A div (not a button) so the dismiss control can be a real
            nested button without invalid button-in-button markup. */}
        <div
          role="button"
          tabIndex={0}
          onClick={toggle}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } }}
          className="w-full flex flex-col gap-0 p-2 hover:bg-panel2/50 text-left transition-colors select-none cursor-pointer focus:outline-none"
        >
          <div className="flex items-center justify-between w-full">
            <div className="flex items-center gap-2 min-w-0 flex-1">
              <span className="shrink-0 text-faint">
                {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              </span>
              <span className="shrink-0">
                {st === "in_progress" ? (
                  <Loader2 size={12} className="animate-spin text-accent" />
                ) : st === "completed" ? (
                  <CheckCircle2 size={12} className="text-good" />
                ) : st === "failed" ? (
                  <XCircle size={12} className="text-risk" />
                ) : st === "cancelled" ? (
                  <XCircle size={12} className="text-muted" />
                ) : (
                  <Circle size={12} className="text-muted" />
                )}
              </span>
              <Tooltip label={j.goal} className="font-semibold text-[11px] text-txt truncate">
                {j.goal}
              </Tooltip>
            </div>
            <div className="flex items-center gap-3 shrink-0 text-[10px] pl-2">
              {(terminal || jobCost(j) > 0 || jobTokens(j) > 0 || jobCompactTokens(j) > 0 || savings.total > 0) && (
                <span className="text-muted font-mono flex items-center gap-1.5">
                  {jobTokens(j) > 0 && <span>{jobTokens(j).toLocaleString()}t</span>}
                  {jobCompactTokens(j) > 0 && (
                    <span className="text-accent/90">{jobCompactTokens(j).toLocaleString()} compact</span>
                  )}
                  <span className="text-good/90">
                    {formatCost(
                      jobCost(j),
                      j.estimated !== false && j.cost_provenance !== "provider",
                    )}
                  </span>
                  <SavingsChip parts={savings} className="text-[9px] font-sans" />
                </span>
              )}
              {/* Kill: running jobs only. Best-effort cooperative cancel on the
                  backend. Shows 'cancelling...' until the next poll flips the job
                  to a terminal state. */}
              {st === "in_progress" && (
                cancelling.has(j.id) ? (
                  <span className="text-[9px] text-risk/70 italic tabular-nums">cancelling...</span>
                ) : (
                  <button
                    onClick={(e) => { e.stopPropagation(); void cancelJob(j.id); }}
                    title="Cancel this job"
                    className="text-faint/50 hover:text-risk transition-colors focus:outline-none"
                  >
                    <X size={12} />
                  </button>
                )
              )}
              {/* Dismiss: terminal runs only -- hiding a live worker would be
                  confusing. Non-destructive; the run stays in PM history. */}
              {terminal && (
                <button
                  onClick={(e) => { e.stopPropagation(); dismissJob(j.id); }}
                  title="Dismiss from tracker (stays in Puppetmaster history)"
                  className="text-faint/50 hover:text-risk transition-colors focus:outline-none"
                >
                  <X size={12} />
                </button>
              )}
            </div>
          </div>

          {/* Model + worker count + adapter -- the "who's doing this and on what"
              line, so the swarm's shape reads without expanding. CLI/external
              jobs (Cursor MCP, terminal `puppetmaster`) share the workspace
              store and are merged in on purpose; label them so they don't look
              like Marionette-dispatched swarms. */}
          {(displayModel || workerCount > 0 || adapter || j.source === "cli") && (
            <div className="flex items-center gap-1.5 pl-6 pr-1 mt-1 flex-wrap">
              {displayModel && (
                <span className="flex items-center gap-1 text-[9px] font-mono text-accent/90 bg-accent/10 px-1.5 py-0.5 rounded" title={`Model: ${displayModel}`}>
                  <Cpu size={9} /> {displayModel}
                </span>
              )}
              {workerCount > 0 && (
                <span className="text-[9px] text-muted bg-panel2/60 px-1.5 py-0.5 rounded tabular-nums">
                  {workerCount} worker{workerCount > 1 ? "s" : ""}
                </span>
              )}
              {adapter && adapter.toLowerCase() !== displayModel.toLowerCase() && (
                <span className="text-[9px] text-faint bg-panel2/40 px-1.5 py-0.5 rounded lowercase">{adapter}</span>
              )}
              {j.source === "cli" && (
                <span
                  className="text-[9px] text-muted bg-panel2/60 border border-edge/50 px-1.5 py-0.5 rounded"
                  title="Started outside Marionette (Cursor MCP or terminal Puppetmaster) for this workspace"
                >
                  external
                </span>
              )}
            </div>
          )}

          {/* Dead run: every worker fast-failed before doing work. Say so and
              why, instead of letting the card read as a normal finished swarm. */}
          {deadRunFailure && (
            <div className="pl-6 pr-1 mt-1 text-[9.5px] text-risk/90">
              all workers failed: {deadRunFailure.replace(/_/g, " ")} -- no work ran, nothing was spent
            </div>
          )}

          {/* Phase strip + label -- the at-a-glance "where is this swarm". */}
          <div className="flex items-center gap-2 pl-6 pr-1 mt-1">
            <div className="flex-1"><PhaseStrip job={j} phase={phase} /></div>
            <span className={`text-[9px] font-medium tabular-nums shrink-0 ${
              phase.key === "cancelled"
                ? "text-muted"
                : phase.failed
                ? "text-risk/80"
                : phase.key === "done"
                ? "text-good/80"
                : "text-accent/80"
            }`}>
              {phase.label}
            </span>
          </div>

          {/* Live-progress line for a running job: a "last activity" relative time
              plus a compact token readout that updates on each poll, so the row
              visibly moves instead of sitting on a static spinner. Cost is
              deliberately omitted here (shown once in the bottom status bar). */}
          {st === "in_progress" && (() => {
            const since = relativeSince(j.updated_at ?? j.created_at, nowTick);
            const showTokens = j.tokens !== undefined && j.tokens > 0;
            if (!since && !showTokens) return null;
            return (
              <div className="flex items-center gap-2 pl-6 pr-1 mt-1 text-[9px] text-faint tabular-nums">
                {since && (
                  <span className="flex items-center gap-1">
                    <Activity size={9} className="text-accent/70 animate-pulse" />
                    {since}
                  </span>
                )}
                {showTokens && <span className="font-mono text-muted">{j.tokens!.toLocaleString()}t</span>}
              </div>
            );
          })()}
        </div>

        {/* Expanded details */}
        {isExpanded && (
          <div className="px-2 pb-2 pt-1 flex flex-col gap-2 bg-panel2/10">
            {/* Routing */}
            {routingArts.length > 0 && (
              <div className="flex flex-col gap-1.5">
                {routingArts.map((art: Artifact, idx: number) => {
                  const hasRejected = art.rejected && art.rejected.length > 0;
                  const key = `${art.id || idx}`;
                  const altsExpanded = !!expandedAlts[key];
                  return (
                    <div key={key} className="p-2 bg-panel rounded border border-edge/45 text-[10px] flex flex-col gap-1.5">
                      <div className="flex items-center justify-between text-muted">
                        <span className="flex items-center gap-1.5 truncate max-w-[72%]">
                          <Cpu size={11} className="text-accent shrink-0" />
                          <span className="text-txt font-mono font-medium truncate" title={art.model}>
                            {art.model || "Unknown model"}
                          </span>
                        </span>
                        <span className="font-mono text-good shrink-0 font-semibold">
                          {art.est_cost_usd !== undefined && art.est_cost_usd > 0
                            ? `$${Number(art.est_cost_usd).toFixed(4)}`
                            : "$0"}
                        </span>
                      </div>
                      {/* One plain-language line on why this model won. */}
                      <div className="text-[9.5px] text-faint leading-relaxed">
                        {summarizeRouting(art)}
                      </div>
                      {/* Alternatives, deliberately de-emphasized: a muted count,
                          expanding to model-name chips (full reason on hover)
                          instead of a red-looking wall of text. */}
                      {hasRejected && (
                        <div>
                          <button
                            onClick={(e) => { e.stopPropagation(); setExpandedAlts((prev) => ({ ...prev, [key]: !altsExpanded })); }}
                            className="text-[9px] text-faint/80 hover:text-muted flex items-center gap-0.5 focus:outline-none"
                          >
                            {altsExpanded ? <ChevronDown size={9} /> : <ChevronRight size={9} />}
                            {art.rejected?.length} alternatives considered
                          </button>
                          {altsExpanded && (
                            <div className="mt-1.5 flex flex-wrap gap-1">
                              {art.rejected?.map((rej: { model: string; reason: string }, ridx: number) => (
                                <Tooltip
                                  key={ridx}
                                  label={rej.reason}
                                  className="font-mono text-[8.5px] text-faint bg-panel2/50 border border-edge/30 px-1.5 py-0.5 rounded cursor-default"
                                >
                                  {rej.model}
                                </Tooltip>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            {/* Single-worker / provider jobs (run_implement, run_parallel) have
                NO routing artifact, so the per-worker cost row above never
                rendered for them -- the dash showed a single-worker swarm with no
                price. Synthesize a cost/model line from the job fields so every
                job surfaces its spend, matching multi-worker swarms. */}
            {routingArts.length === 0 && ((j.est_cost_usd ?? 0) > 0 || (j.tokens ?? 0) > 0) && (
              <div className="p-2 bg-panel rounded border border-edge/45 text-[10px] flex items-center justify-between text-muted">
                <span className="flex items-center gap-1.5 truncate max-w-[72%]">
                  <Cpu size={11} className="text-accent shrink-0" />
                  <span className="text-txt font-mono font-medium truncate" title={displayModel}>
                    {displayModel || "worker"}
                  </span>
                </span>
                <span className="flex items-center gap-2 shrink-0 font-mono">
                  {(j.tokens ?? 0) > 0 && <span className="text-faint">{j.tokens!.toLocaleString()}t</span>}
                  <span className="text-good font-semibold">
                    {formatCost(
                      Number(j.est_cost_usd || 0),
                      j.estimated !== false && j.cost_provenance !== "provider",
                    )}
                  </span>
                </span>
              </div>
            )}

            {/* Workers -- with a completion progress bar so a wave of parallel
                workers reads as a single advancing unit. */}
            {tasks.length > 0 && (
              <div className="border-t border-edge/20 pt-1.5 flex flex-col gap-1.5">
                <div className="flex items-center justify-between">
                  <span className="text-[9px] uppercase tracking-wider text-faint font-medium">Workers ({tasks.length})</span>
                </div>
                <WorkerProgress tasks={tasks} />
                <div className="flex flex-col gap-1 mt-0.5">
                  {tasks.map((task) => {
                    const ts = taskState(task);
                    const tExpanded = !!expandedTasks[task.id];
                    const hasInstruction = !!task.instruction;
                    return (
                      <div
                        key={task.id}
                        role={hasInstruction ? "button" : undefined}
                        tabIndex={hasInstruction ? 0 : undefined}
                        onClick={hasInstruction ? () => toggleTask(task.id) : undefined}
                        onKeyDown={hasInstruction ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleTask(task.id); } } : undefined}
                        className={`p-1.5 rounded bg-panel/25 border border-edge/20 flex items-start gap-2 text-[10px] ${hasInstruction ? "cursor-pointer hover:bg-panel/45 focus:outline-none" : ""}`}
                      >
                        <span className="mt-0.5 shrink-0">
                          {ts === "running" ? <Loader2 size={10} className="animate-spin text-accent" />
                            : ts === "done" ? <CheckCircle2 size={10} className="text-good" />
                            : ts === "fail" ? <XCircle size={10} className="text-risk" />
                            : <Circle size={10} className="text-muted" />}
                        </span>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center justify-between gap-1">
                            <span className="font-semibold text-txt truncate flex items-center gap-1 min-w-0">
                              {hasInstruction && (tExpanded
                                ? <ChevronDown size={9} className="text-faint shrink-0" />
                                : <ChevronRight size={9} className="text-faint shrink-0" />)}
                              <span className="truncate">
                                {task.role || "Worker"}{" "}
                                <span className="text-faint font-normal">({task.adapter || "no-adapter"})</span>
                              </span>
                            </span>
                            <span className="flex items-center gap-1.5 shrink-0">
                              {((task.tokens ?? 0) > 0 || (task.est_cost_usd ?? 0) > 0) && (
                                <span className="text-muted font-mono text-[9px] flex items-center gap-1 tabular-nums">
                                  {(task.tokens ?? 0) > 0 && (
                                    <span>{Number(task.tokens).toLocaleString()}t</span>
                                  )}
                                  <span className="text-good/90">
                                    {formatCost(
                                      Number(task.est_cost_usd || 0),
                                      task.estimated !== false && task.cost_provenance !== "provider",
                                    )}
                                  </span>
                                </span>
                              )}
                              <span className={`text-[8px] uppercase font-bold px-1 rounded ${
                                ts === "running" ? "text-accent bg-accent/10"
                                  : ts === "done" ? "text-good bg-good/10"
                                  : ts === "fail" ? "text-risk bg-risk/10"
                                  : "text-muted bg-panel"
                              }`}>{task.status}</span>
                            </span>
                          </div>
                          {hasInstruction && (tExpanded ? (
                            <div className="text-muted text-[9.5px] mt-1 whitespace-pre-wrap break-words leading-relaxed">{task.instruction}</div>
                          ) : (
                            <Tooltip label={task.instruction} className="block text-muted text-[9.5px] mt-0.5 truncate">{task.instruction}</Tooltip>
                          ))}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Findings / artifacts stream -- the substance of an audit, made
                first-class: type badge, confidence, headline. Section collapses
                so a long finished swarm does not force a wall of rows. */}
            {streamArts.length > 0 && (() => {
              const findingRows = dedupeFindings(streamArts);
              const sectionOpen = findingsOpen[j.id] !== false;
              const countLabel = `${findingRows.length}${findingRows.length !== streamArts.length ? ` of ${streamArts.length}` : ""}`;
              return (
              <div className="border-t border-edge/20 pt-1.5 flex flex-col">
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setFindingsOpen((prev) => ({ ...prev, [j.id]: !sectionOpen }));
                  }}
                  className="w-full flex items-center gap-1 text-[9px] uppercase tracking-wider text-faint font-medium mb-1 hover:text-muted focus:outline-none"
                  title={sectionOpen ? "Collapse findings" : "Expand findings"}
                >
                  {sectionOpen ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
                  Findings ({countLabel})
                </button>
                {sectionOpen && (
                <div className="pr-1 flex flex-col gap-1 border border-edge/20 rounded p-1.5 bg-panel/30">
                  {findingRows.map(({ art, count }, idx: number) => {
                    const fid = art.id || `f${idx}`;
                    const fExpanded = !!expandedFindings[fid];
                    const detailStr = art.detail == null
                      ? ""
                      : (typeof art.detail === "string"
                          ? art.detail
                          : (() => { try { return JSON.stringify(art.detail, null, 2); } catch { return String(art.detail); } })());
                    return (
                    <div key={fid} className="text-[9.5px] border-b border-edge/10 pb-1 last:border-0 last:pb-0 flex flex-col gap-0.5">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-bold text-accent uppercase tracking-wider text-[8px] flex items-center gap-1">
                          {art.type}
                          {count > 1 && <span className="text-faint bg-edge/20 px-1 rounded normal-case tracking-normal">x{count}</span>}
                        </span>
                        {art.confidence !== undefined && art.confidence !== null && (
                          <span className="text-[8px] text-faint bg-edge/20 px-1 rounded shrink-0">
                            {Math.round(art.confidence * 100)}%
                          </span>
                        )}
                      </div>
                      <div
                        role="button"
                        tabIndex={0}
                        onClick={() => toggleFinding(fid)}
                        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleFinding(fid); } }}
                        className="flex items-start gap-1 text-txt break-words leading-relaxed cursor-pointer hover:text-white focus:outline-none"
                      >
                        <span className="mt-0.5 shrink-0 text-faint">
                          {fExpanded ? <ChevronDown size={9} /> : <ChevronRight size={9} />}
                        </span>
                        {fExpanded ? (
                          <span className="flex-1 min-w-0 whitespace-pre-wrap">{art.headline}</span>
                        ) : (
                          <Tooltip label={art.headline} className="flex-1 min-w-0 line-clamp-2">{art.headline}</Tooltip>
                        )}
                      </div>
                      {fExpanded && detailStr && (
                        <div className="mt-1 ml-4 text-[9px] text-muted whitespace-pre-wrap break-words bg-panel/40 border border-edge/20 rounded p-1.5 font-mono max-h-72 overflow-auto">
                          {detailStr}
                        </div>
                      )}
                    </div>
                    );
                  })}
                </div>
                )}
              </div>
              );
            })()}

            {tasks.length === 0 && streamArts.length === 0 && routingArts.length === 0 && (
              <div className="text-[9.5px] text-faint italic px-1 py-0.5">
                {loadingArts.has(j.id) || (j.artifacts_complete === false)
                  ? "Loading artifacts..."
                  : st === "in_progress"
                    ? "Worker running -- artifacts will stream in as they land."
                    : "No artifacts recorded."}
              </div>
            )}
          </div>
        )}
      </div>
    );
  };

  // Dim only on genuine transitions -- dimming every ~2.5s poll cycle made the
  // whole pane visibly "blink" while a swarm ran.
  const panelDimmed = projectSwitching || isTransitioning;

  return (
    <div className={`flex flex-col h-full overflow-hidden bg-panel ${panelOpacityClass(panelDimmed, isShowingStale)}`}>
      {/* Persistent header: the tracker always announces itself, with live
          aggregate counts, so it reads as a dashboard even at rest. */}
      <div className="shrink-0 flex items-center justify-between px-3 py-2 border-b border-edge/60 select-none">
        <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-faint font-semibold">
          <span className="relative inline-flex">
            <Network size={11} className={anyRunning ? "text-accent" : "text-faint/70"} />
            {anyRunning ? (
              <span
                className="absolute -top-0.5 -right-0.5 h-1.5 w-1.5 rounded-full bg-accent animate-pulse"
                title={`${runningCount} running`}
                aria-hidden
              />
            ) : null}
          </span>
          <span>Swarm Tracker</span>
          {isShowingStale && !isTransitioning && (
            <span className="text-[9px] normal-case tracking-normal text-faint/70 italic">refreshing…</span>
          )}
          {isTransitioning && <Loader2 size={10} className="animate-spin text-muted shrink-0" />}
          {visibleJobs.length > 0 && <span className="text-faint/60 normal-case tracking-normal">({visibleJobs.length})</span>}
        </div>
        <div className="flex items-center gap-2.5 text-[10px]">
          {anyRunning && (
            <span className="flex items-center gap-1 text-accent">
              <Loader2 size={10} className="animate-spin" /> {runningCount} running
            </span>
          )}
          {completedCount > 0 && (
            <span className="flex items-center gap-1 text-good/80">
              <CheckCircle2 size={10} /> {completedCount}
            </span>
          )}
        </div>
      </div>

      {/* Scrollable Jobs list. min-h-0 is load-bearing: without it a flex-1 item
          in a flex-col defaults to min-height:auto, refuses to shrink below its
          content, grows past the panel, and the root's overflow-hidden clips it
          -- so overflow-y-auto never engages and the list can't scroll. */}
      <div className="flex-1 min-h-0 overflow-y-auto p-2 flex flex-col gap-2">
        {visibleJobs.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-48 text-center px-6 gap-2">
            <Network size={20} className="text-faint/50" />
            <span className="text-[12px] text-muted font-medium">
              {isValidating && !data
                ? "Loading swarm jobs..."
                : hiddenCount > 0 ? "All swarm jobs cleared" : "No swarm jobs yet"}
            </span>
            {hiddenCount > 0 ? (
              // "Clear" hid every job. Without this affordance the pane read as
              // "No swarm jobs yet" even though the backend had a full history --
              // indistinguishable from a broken tracker.
              <button
                onClick={restoreDismissed}
                className="text-[10.5px] text-accent hover:underline focus:outline-none"
              >
                Show {hiddenCount} hidden job{hiddenCount === 1 ? "" : "s"}
              </button>
            ) : (
              <span className="text-[10.5px] text-faint leading-relaxed">
                Every dispatched worker lands here -- run_implement, run_parallel,
                and run_swarm alike -- with its phase, router choice, live workers,
                and streamed findings. Inline tool calls stay in the chat.
              </span>
            )}
          </div>
        ) : (
          <>
            {/* Active runs pinned on top, newest first. */}
            {running.slice().reverse().map(renderJob)}

            {/* Finished runs folded into a collapsible section so a long session
                stays a short list. Non-destructive: "Clear" only hides. */}
            {finished.length > 0 && (
              <div className="shrink-0 flex flex-col gap-2">
                <div className="flex items-center justify-between px-1 pt-0.5">
                  <button
                    onClick={() => setFinishedOpen((o) => !o)}
                    className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-faint font-semibold hover:text-muted focus:outline-none"
                  >
                    {finishedOpen ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
                    Finished
                    <span className="text-faint/60 normal-case tracking-normal">({finished.length})</span>
                    {failedCount > 0 && (
                      <span className="text-risk/70 normal-case tracking-normal">{"\u00b7"} {failedCount} failed</span>
                    )}
                    {cancelledCount > 0 && (
                      <span className="text-muted normal-case tracking-normal">{"\u00b7"} {cancelledCount} cancelled</span>
                    )}
                  </button>
                  <button
                    onClick={clearFinished}
                    title="Hide all finished runs from the tracker (stays in Puppetmaster history)"
                    className="text-[9px] text-faint/70 hover:text-risk uppercase tracking-wider focus:outline-none"
                  >
                    Clear
                  </button>
                </div>
                {finishedOpen && finished.slice().reverse().map(renderJob)}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
