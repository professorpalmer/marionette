import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Loader2, CheckCircle2, XCircle, Circle, ChevronDown, ChevronRight, Cpu, Activity, Network, X, AlertTriangle } from "lucide-react";
import { api, jobArtifactList, type SwarmLive, type Job, type Artifact, type Task } from "../lib/api";
import { displayModelId, isEngineOnlyModelId, modelIdsEqual } from "../lib/modelIdentity";
import { lastSelectedProjectRoot, panelOpacityClass, useProjectSwitching } from "../lib/panelTransition";
import {
  clearPendingSwarmOpenJob,
  peekPendingSwarmOpenArtifact,
  peekPendingSwarmOpenJob,
} from "../lib/pendingSwarmOpenJob";
import { useStaleWhileRevalidate } from "../lib/useStaleWhileRevalidate";
import { filterJobsByScope, loadJobScope, saveJobScope, type JobScope } from "../lib/jobScope";

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
type SortOrder = "newest" | "oldest";
type JobFilter = "all" | "active" | "completed" | "failed" | "untrustworthy" | "cancelled";

function timestampMs(ts: unknown): number | null {
  if (typeof ts === "number" && isFinite(ts)) return ts > 1e12 ? ts : ts * 1000;
  if (typeof ts === "string" && ts) {
    const parsed = Date.parse(ts);
    if (!isNaN(parsed)) return parsed;
  }
  return null;
}

// Compact "how long ago" label for a job's last activity. Accepts epoch seconds
// or an ISO string (the backend sends created_at/updated_at as either). Returns
// "" when we can't parse a timestamp so the caller can omit the affordance.
function relativeSince(ts: unknown, nowMs: number): string {
  const t = timestampMs(ts);
  if (t === null) return "";
  const secs = Math.max(0, Math.round((nowMs - t) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ago`;
}

// Resolve attested routing policy from the structured field first, then the
// legacy "policy=..." detail string. Empty means attribution is unknown.
function routingPolicy(art: Artifact): string {
  const fromField = typeof art.policy === "string" ? art.policy.trim() : "";
  if (fromField) return fromField;
  const detail = typeof art.detail === "string" ? art.detail : "";
  return (detail.match(/policy=(\w+)/) || [])[1] || "";
}

function isPlanBilledRouting(art?: Artifact): boolean {
  if (!art) return false;
  const detail = typeof art.detail === "string" ? art.detail : "";
  return /plan-billed|in-subscription/i.test(detail);
}

// FINDING headlines that look like the worker echoed its prompt rather than
// reporting a finding. Warn with a chip; never rewrite the headline itself.
function looksLikePromptEcho(headline: string): boolean {
  const text = (headline || "").trim();
  if (!text) return false;
  // format_artifacts truncates to 300; near-cap walls are almost always echoes.
  if (text.length >= 200) return true;
  return /^(Role\s*:|Goal\s*:|Return\s+only\b)/i.test(text);
}

function isFailedTerminalStatus(status: string): boolean {
  return (
    status.includes("fail")
    || status.includes("error")
    || status.includes("stall")
    || status.includes("dead")
    || status.includes("interrupt")
    || status.includes("truncat")
    || /time(?:d)?[-_ ]?out/.test(status)
  );
}

function jobStatus(j: Job): Status {
  const s = (j.status || "").toLowerCase();
  // User cancel / abort — distinct from ordinary worker failure chrome.
  if (
    s.includes("cancel")
    || s.includes("user-aborted")
    || s.includes("user_aborted")
  ) {
    return "cancelled";
  }
  if (isFailedTerminalStatus(s)) {
    return "failed";
  }
  if (s.includes("run") || s.includes("progress") || s.includes("active")) return "in_progress";
  if (s.includes("complete") || s.includes("done")) return "completed";
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
  if (
    s.includes("fail")
    || s.includes("cancel")
    || s.includes("error")
    || isFailedTerminalStatus(s)
  ) return "fail";
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

// Outer job-card expand/collapse is view state, scoped per repo like dismiss.
// Explicit true/false overrides the in_progress default on remount; missing keys
// keep active jobs open and terminal jobs closed. Soft-capped per repo.
const EXPAND_KEY = "swarm.expanded.v1";
const EXPAND_CAP = DISMISS_CAP;

type ExpandStore = Record<string, Record<string, boolean>>;

function readExpandStore(): ExpandStore {
  try {
    const raw = localStorage.getItem(EXPAND_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as ExpandStore;
      }
    }
  } catch {
    // Malformed/unavailable storage -- fail closed to defaults.
  }
  return {};
}

function loadExpanded(repo?: string): Record<string, boolean> {
  const store = readExpandStore();
  const raw = store[repoDismissKey(repo)];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  const result: Record<string, boolean> = {};
  for (const [id, val] of Object.entries(raw)) {
    if (typeof id === "string" && typeof val === "boolean") result[id] = val;
  }
  return result;
}

function saveExpanded(repo: string | undefined, expanded: Record<string, boolean>): void {
  try {
    const store = readExpandStore();
    const entries = Object.entries(expanded).slice(-EXPAND_CAP);
    store[repoDismissKey(repo)] = Object.fromEntries(entries);
    localStorage.setItem(EXPAND_KEY, JSON.stringify(store));
  } catch {
    // localStorage full/unavailable -- expansion still works for this session.
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
      `:${j.swarm_cache_savings_basis ?? ""}:${j.swarm_cache_unpriced_tokens ?? 0}` +
      `:${(j.tool_output_savings_usd ?? 0).toFixed(4)}` +
      `:${j.source ?? "harness"}` +
      `:${j.artifacts_complete ?? ""}` +
      `:${j.outcome?.quality ?? ""}:${j.outcome?.trustworthy ?? ""}` +
      `:${(j.outcome?.reasons || []).join("|")}:${j.updated_at ?? ""}`,
    );
    for (const t of tasks) {
      parts.push(
        `${t.id}=${t.status}:${t.model ?? ""}:${t.tokens ?? 0}:${(t.est_cost_usd ?? 0).toFixed(4)}`,
      );
    }
    for (const a of arts) {
      const type = (a.type || "").toUpperCase();
      if (type === "ROUTING") {
        parts.push(
          `R:${a.task_id ?? ""}:${a.model ?? ""}:${a.created_by ?? ""}` +
          `:${routingPolicy(a)}:${a.provider ?? ""}`,
        );
      } else {
        const detail = typeof a.detail === "string" ? a.detail.slice(0, 500) : "";
        parts.push(
          `A:${a.id ?? ""}:${a.task_id ?? ""}:${(a.type || "").slice(0, 8)}` +
          `:${(a.headline || "").slice(0, 120)}:${a.result ?? ""}` +
          `:${a.failure ?? ""}:${detail}`,
        );
      }
    }
  }
  const s = res.session;
  if (s) {
    parts.push(
      `S:${s.driver ?? ""}:${s.tokens_used ?? 0}:${(s.est_cost_usd ?? 0).toFixed(4)}` +
      `:${(s.routing_saved_usd ?? 0).toFixed(4)}` +
      `:${(s.cache_saved_usd_swarm ?? 0).toFixed(4)}` +
      `:${s.swarm_cache_savings_basis ?? ""}:${s.swarm_cache_unpriced_tokens ?? 0}` +
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
type FindingRow = { art: Artifact; count: number; artifactIds: string[] };
function dedupeFindings(arts: Artifact[]): FindingRow[] {
  const rows = new Map<string, FindingRow>();
  for (const art of arts) {
    const key = `${(art.type || "").toUpperCase()}::${(art.headline || "").trim().toLowerCase()}`;
    const hit = rows.get(key);
    if (hit) {
      hit.count += 1;
      if (art.id) hit.artifactIds.push(art.id);
    } else {
      rows.set(key, { art, count: 1, artifactIds: art.id ? [art.id] : [] });
    }
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

function routingByTaskId(arts: Artifact[]): Map<string, Artifact> {
  const map = new Map<string, Artifact>();
  for (const art of arts) {
    const id = (art.task_id || "").trim();
    if (id) map.set(id, art);
  }
  return map;
}

function isUnscopedRouting(art: Artifact): boolean {
  return !(art.task_id || "").trim();
}

function bestUnscopedRouting(arts: Artifact[]): Artifact | undefined {
  let best: Artifact | undefined;
  for (const art of arts) {
    if (!isUnscopedRouting(art)) continue;
    if (!best || routingCreatedByRank(art.created_by) > routingCreatedByRank(best.created_by)) {
      best = art;
    }
  }
  return best;
}

/** Local single-worker jobs use `${job_id}-w0` but the ROUTING preview has no task_id. */
function associateRoutingToTasks(
  arts: Artifact[],
  tasks: Task[],
): { routingForTask: Map<string, Artifact>; unmatched: Artifact[] } {
  const routingForTask = routingByTaskId(arts);
  const consumedUnscoped = new Set<Artifact>();
  if (tasks.length === 1 && !routingForTask.has(tasks[0].id)) {
    const taskId = tasks[0].id;
    const isLocalWorkerSlot = /-w\d+$/.test(taskId);
    if (isLocalWorkerSlot) {
      const best = bestUnscopedRouting(arts);
      if (best) {
        routingForTask.set(tasks[0].id, best);
        for (const art of arts) {
          if (isUnscopedRouting(art)) consumedUnscoped.add(art);
        }
      }
    }
  }
  const ids = new Set(tasks.map((t) => t.id));
  const unmatched = arts.filter((a) => {
    if (consumedUnscoped.has(a)) return false;
    const tid = (a.task_id || "").trim();
    return !tid || !ids.has(tid);
  });
  return { routingForTask, unmatched };
}

function isFailedArtifact(art: Artifact): boolean {
  const result = String(art.result || "").trim().toLowerCase();
  const kind = String(art.type || "").trim().toLowerCase();
  return !!art.failure
    || result === "failed"
    || result === "blocked"
    || result === "error"
    || result === "degraded"
    || kind === "error";
}

export type WorkerOutcome = "running" | "idle" | "ok" | "degraded" | "failed";

/** Product quality for a worker row. Lifecycle stays in taskState(); this never infers from role/model/index. */
export function workerOutcome(task: Task, failureArt?: Artifact | null): WorkerOutcome {
  const life = taskState(task);
  if (life === "running") return "running";
  if (life === "fail") return "failed";
  const unsuccessful = !!failureArt && isFailedArtifact(failureArt);
  if (unsuccessful && (life === "done" || life === "idle")) return "degraded";
  if (life === "done") return "ok";
  return "idle";
}

function WorkerOutcomeGlyph({ outcome }: { outcome: WorkerOutcome }) {
  switch (outcome) {
    case "running":
      return <Loader2 size={10} className="animate-spin semantic-activity-spinner text-accent" />;
    case "ok":
      return <CheckCircle2 size={10} className="text-good" />;
    case "degraded":
      return <AlertTriangle size={10} className="text-warn" />;
    case "failed":
      return <XCircle size={10} className="text-risk" />;
    default:
      return <Circle size={10} className="text-muted" />;
  }
}

function failureArtifactsByTaskId(arts: Artifact[]): Map<string, Artifact> {
  const byTask = new Map<string, Artifact>();
  for (const art of arts) {
    const taskId = String(art.task_id || "").trim();
    if (!taskId || !isFailedArtifact(art)) continue;
    const current = byTask.get(taskId);
    const score = (art.detail ? 4 : 0) + (art.failure ? 2 : 0) + (art.headline ? 1 : 0);
    const currentScore = current
      ? (current.detail ? 4 : 0) + (current.failure ? 2 : 0) + (current.headline ? 1 : 0)
      : -1;
    if (score >= currentScore) byTask.set(taskId, art);
  }
  return byTask;
}

function failureReason(art?: Artifact): string {
  if (!art) return "";
  if (typeof art.detail === "string" && art.detail.trim()) return art.detail.trim();
  if (String(art.type || "").trim().toLowerCase() === "error") {
    return String(art.headline || "").trim();
  }
  return "";
}

function unmatchedAddsTruth(arts: Artifact[], headerModel: string): boolean {
  return arts.some((a) => {
    const policy = routingPolicy(a);
    if (policy === "explicit_pin") return true;
    const created = a.created_by || "";
    if (created === "router-fallback" || created === "router-escalation") return true;
    if (a.rejected && a.rejected.length > 0) return true;
    const model = (a.model || "").trim();
    // Header already owns this model — a provider stamp is not extra truth.
    if (model && headerModel && modelIdsEqual(model, headerModel)) return false;
    if ((a.provider || "").trim()) return true;
    return !!(model && headerModel && !modelIdsEqual(model, headerModel));
  });
}

type WorkerModelView = {
  rawModel: string;
  display: string;
  slot: string;
  pending: boolean;
  residual: boolean;
  pinned: boolean;
  policy: string;
  routing?: Artifact;
};

function isInFlightWorker(task: Task): boolean {
  const ts = taskState(task);
  if (ts === "running") return true;
  if (ts === "done" || ts === "fail") return false;
  const s = (task.status || "").toLowerCase();
  return (
    s.includes("queued")
    || s.includes("pending")
    || s.includes("registered")
    || s.includes("started")
  );
}

function workerModelView(task: Task, routing?: Artifact): WorkerModelView {
  const adapterLabel = (task.adapter || "").trim();
  // Associated final ROUTING wins over a stale task.model preview from an earlier poll.
  const rawModel = (routing?.model || task.model || "").trim();
  const policy = routing ? routingPolicy(routing) : "";
  const display = displayModelId(rawModel, {
    policy,
    adapterFallback: isEngineOnlyModelId(adapterLabel) ? "" : adapterLabel,
  });
  const ts = taskState(task);
  const pinned = policy === "explicit_pin";
  if (display) {
    return { rawModel, display, slot: display, pending: false, residual: false, pinned, policy, routing };
  }
  if (isInFlightWorker(task)) {
    return { rawModel, display, slot: "routing…", pending: true, residual: false, pinned: false, policy, routing };
  }
  const meaningfulResidual =
    ts === "fail" || isEngineOnlyModelId(adapterLabel) || isEngineOnlyModelId(rawModel);
  return {
    rawModel,
    display,
    slot: meaningfulResidual ? "no-model" : "",
    pending: false,
    residual: meaningfulResidual,
    pinned: false,
    policy,
    routing,
  };
}

const DISCLOSURE_FOCUS =
  "focus:outline-none focus-visible:outline focus-visible:outline-1 focus-visible:outline-offset-1 focus-visible:outline-accent";

/** Merge a fresh /swarm/live poll into cached state without wiping expanded full artifacts. */
function mergeSwarmLive(prev: SwarmLive | null | undefined, next: SwarmLive): SwarmLive {
  if (!prev?.jobs?.length) return next;
  const prevById = new Map(prev.jobs.map((j) => [j.id, j]));
  return {
    ...next,
    jobs: (next.jobs || []).map((j) => {
      const old = prevById.get(j.id);
      if (!old) return j;
      // Keep hydrated artifacts only while the fresh row remains healthy. A
      // failed or non-trustworthy terminal poll is authoritative and must not
      // retain stale success evidence from an earlier expansion of the same job.
      const freshStatus = jobStatus(j);
      const mayKeepHydrated = freshStatus !== "failed"
        && freshStatus !== "cancelled"
        && j.outcome?.trustworthy !== false;
      if (
        mayKeepHydrated
        && j.artifacts_complete === false
        && old.artifacts_complete === true
      ) {
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

/** Missing/unpriceable estimates must not paint as measured $0. */
function formatKnownCost(cost: unknown, estimated?: boolean): string {
  if (typeof cost !== "number" || !isFinite(cost)) return "—";
  if (!(cost > 0) && estimated) return "—";
  return formatCost(cost, estimated);
}

function isKnownPositiveCost(cost: unknown): boolean {
  return typeof cost === "number" && isFinite(cost) && cost > 0;
}

function isProviderAttestedExactZero(cost: unknown, estimated?: boolean): cost is number {
  return typeof cost === "number" && isFinite(cost) && !(cost > 0) && estimated === false;
}

function hasMeaningfulJobCost(j: Job): boolean {
  const estimated = j.estimated !== false && j.cost_provenance !== "provider";
  if (isKnownPositiveCost(j.est_cost_usd)) return true;
  return isProviderAttestedExactZero(j.est_cost_usd, estimated);
}

function formatWorkerCost(
  cost: unknown,
  estimated?: boolean,
  planBilled?: boolean,
): string {
  if (isProviderAttestedExactZero(cost, estimated)) {
    return formatCost(cost, false);
  }
  if (planBilled && !isKnownPositiveCost(cost)) {
    return "—";
  }
  return formatKnownCost(cost, estimated);
}

export type SpendBasis = "provider" | "measured" | "estimated" | "unavailable";

export function namedSpend(cost: unknown, basis: SpendBasis): string {
  if (basis === "unavailable" || typeof cost !== "number" || !isFinite(cost)) {
    return "Cost unavailable";
  }
  if (basis === "provider") return `Provider-reported cost ${formatCost(cost, false)}`;
  if (basis === "measured") return `Measured usage cost ${formatCost(cost, false)}`;
  return `Estimated cost ${formatCost(cost, true)}`;
}

export function namedForecast(cost: unknown): string {
  if (typeof cost !== "number" || !isFinite(cost) || !(cost > 0)) return "Cost unavailable";
  return `Route forecast ${formatCost(cost, true)}`;
}

export function namedSavings(cost: unknown): string {
  if (typeof cost !== "number" || !isFinite(cost) || !(cost > 0)) return "Cost unavailable";
  return `Estimated savings ${formatCost(cost, true)}`;
}

export function jobIdentifier(id: string): string {
  const jid = (id || "").trim();
  return jid ? `Job ${jid}` : "Job";
}

export function spendBasisFor(jobOrTask: {
  estimated?: boolean;
  cost_provenance?: string;
  est_cost_usd?: number;
}): SpendBasis {
  const estimated = jobOrTask.estimated !== false && jobOrTask.cost_provenance !== "provider";
  const cost = jobOrTask.est_cost_usd;
  if (typeof cost !== "number" || !isFinite(cost)) return "unavailable";
  if (jobOrTask.cost_provenance === "unknown") return "unavailable";
  if (jobOrTask.cost_provenance === "provider" && estimated === false) return "provider";
  if (jobOrTask.cost_provenance === "live" && estimated === false) return "measured";
  if (!(cost > 0) && estimated) return "unavailable";
  if (estimated) return "estimated";
  return "measured";
}

/** Worker spend never falls back to a ROUTING forecast. */
export function workerSpend(
  task: Task,
  job: Job,
): { cost: number | undefined; estimated: boolean; basis: SpendBasis } | null {
  if (task.est_cost_usd != null) {
    const estimated = task.estimated !== false && task.cost_provenance !== "provider";
    return { cost: task.est_cost_usd, estimated, basis: spendBasisFor(task) };
  }
  const tasks = job.tasks || [];
  if (tasks.length === 1 && hasMeaningfulJobCost(job)) {
    const estimated = job.estimated !== false && job.cost_provenance !== "provider";
    return { cost: job.est_cost_usd, estimated, basis: spendBasisFor(job) };
  }
  return null;
}

function positiveUsd(n?: number): number {
  return typeof n === "number" && isFinite(n) && n > 0 ? n : 0;
}

type SavingsBasis = "actual_usage" | "estimated" | "unknown";

type SavingsParts = {
  routing: number;
  routingBasis?: SavingsBasis;
  delegation: number;
  delegationBasis?: SavingsBasis;
  /** Credited model-selection plane (delegation measured, else routing). */
  modelSelection: number;
  modelSelectionEstimated: boolean;
  cache: number;
  cachePartial: boolean;
  cacheUnpricedTokens: number;
  compact: number;
  total: number;
};

function creditRoutingSavings(
  basis: Job["routing_savings_basis"] | undefined,
  usd?: number,
): number {
  const value = positiveUsd(usd);
  if (value <= 0) return 0;
  if (basis === "unknown") return 0;
  return value;
}

function creditDelegationSavings(
  basis: Job["delegation_savings_basis"] | undefined,
  usd?: number,
): number {
  const value = positiveUsd(usd);
  if (value <= 0) return 0;
  if (basis === "actual_usage" || basis == null) return value;
  return 0;
}

/** Missing ownership is owned for harness/local rows; CLI/external fail closed. */
export function jobAccountingOwned(j: Job): boolean {
  if (j.accounting_owned === true) return true;
  if (j.accounting_owned === false) return false;
  const src = (j.source || "harness").toLowerCase();
  return src !== "cli";
}

/** Exported for focused Vitest — keeps routing / delegation / cache separate. */
export function jobSavings(j: Job): SavingsParts {
  if (!jobAccountingOwned(j)) {
    return {
      routing: 0,
      delegation: 0,
      modelSelection: 0,
      modelSelectionEstimated: false,
      cache: 0,
      cachePartial: false,
      cacheUnpricedTokens: 0,
      compact: 0,
      total: 0,
    };
  }
  const routingBasis = j.routing_savings_basis;
  const delegationBasis = j.delegation_savings_basis;
  const routing = creditRoutingSavings(routingBasis, j.routing_saved_usd);
  const delegation = creditDelegationSavings(delegationBasis, j.delegation_saved_usd);
  const delegationMeasured = delegationBasis === "actual_usage";
  // Measured zero delegation must not be replaced by a routing estimate.
  const modelSelection = delegationMeasured
    ? delegation
    : (delegation > 0 ? delegation : routing);
  const modelSelectionEstimated =
    !delegationMeasured && modelSelection > 0 && routingBasis === "estimated";
  const cache = positiveUsd(j.cache_saved_usd);
  const cacheUnpricedTokens = Math.max(0, j.swarm_cache_unpriced_tokens || 0);
  const cachePartial =
    cache > 0
    && (
      j.swarm_cache_savings_basis === "unknown"
      || cacheUnpricedTokens > 0
    );
  const compact = positiveUsd(j.tool_output_savings_usd);
  return {
    routing,
    routingBasis,
    delegation,
    delegationBasis,
    modelSelection,
    modelSelectionEstimated,
    cache,
    cachePartial,
    cacheUnpricedTokens,
    compact,
    total: modelSelection + cache + compact,
  };
}

function savingsDetail(parts: SavingsParts): string {
  const bits: string[] = [];
  if (parts.delegation > 0) {
    bits.push(
      `delegation value vs frontier-equivalent list price (~${formatCost(parts.delegation)})`,
    );
  }
  if (parts.routing > 0 && parts.delegation > 0) {
    // Both present — keep planes separate in the tooltip.
    bits.push(
      `routing decision value (~${formatCost(parts.routing)}${
        parts.routingBasis === "estimated" ? ", estimate" : ""
      })`,
    );
  } else if (parts.modelSelection > 0 && parts.delegation <= 0) {
    bits.push(
      parts.modelSelectionEstimated
        ? `model selection value vs frontier-equivalent list price (~${formatCost(parts.modelSelection)}, estimate)`
        : `model selection value vs frontier-equivalent list price (~${formatCost(parts.modelSelection)})`,
    );
  }
  if (parts.cache > 0) {
    bits.push(
      `prompt-cache value (~${formatCost(parts.cache)}${
        parts.cachePartial
          ? parts.cacheUnpricedTokens > 0
            ? `, partial; ${parts.cacheUnpricedTokens.toLocaleString()} tokens unpriced`
            : ", partial pricing"
          : ""
      })`,
    );
  }
  if (parts.compact > 0) {
    bits.push(`tool-output compaction (~${formatCost(parts.compact)})`);
  }
  return bits.join("  ·  ");
}

export function SavingsChip({ parts, className }: { parts: SavingsParts; className?: string }) {
  if (parts.total <= 0) return null;
  return (
    <span
      className={`inline-flex items-center gap-1 text-good/80 tabular-nums ${className ?? ""}`}
      title={`List-price value from model selection, prompt-cache, and compaction (additive, not billed): ${savingsDetail(parts)}`}
    >
      <span className="text-good/45" aria-hidden="true">{"\u2193"}</span>
      {namedSavings(parts.total)}
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
  if (st === "completed") {
    const label = j.outcome?.trustworthy === false ? j.outcome.quality : "done";
    return { key: "done", label, index: 3, failed: false };
  }
  if (total > 0 && running > 0) return { key: "workers", label: `running ${doneCount}/${total}`, index: 2, failed: false };
  if (total > 0) return { key: "workers", label: `${total} worker${total > 1 ? "s" : ""}`, index: 2, failed: false };
  if (hasRouting) return { key: "routing", label: "routing", index: 1, failed: false };
  return { key: "dispatched", label: "dispatched", index: 0, failed: false };
}

function PhaseStrip({ job, phase }: { job: Job; phase?: ReturnType<typeof jobPhase> }) {
  const { index, failed, key } = phase ?? jobPhase(job);
  const warning = jobStatus(job) === "completed" && job.outcome?.trustworthy === false;
  const active = key !== "done" && !failed;
  return (
    <div
      className="flex items-center gap-0.5"
      role="progressbar"
      aria-label={`Swarm phase: ${key}`}
      aria-valuemin={0}
      aria-valuemax={PHASES.length - 1}
      aria-valuenow={index}
    >
      {PHASES.map((_, i) => {
        const reached = i <= index;
        const isActiveSeg = i === index && active;
        const color = failed && i === index
          ? (key === "cancelled" ? "bg-muted" : "bg-risk")
          : warning && reached
          ? "bg-warn"
          : reached
          ? (key === "done" ? "bg-good" : "bg-accent")
          : "bg-edge/60";
        return (
          <div
            key={i}
            className={`h-px flex-1 transition-all ${color} ${isActiveSeg ? "animate-pulse" : ""}`}
          />
        );
      })}
    </div>
  );
}

function WorkerProgress({
  tasks,
  failureForTask,
}: {
  tasks: Task[];
  failureForTask: Map<string, Artifact>;
}) {
  const total = tasks.length;
  if (total === 0) return null;
  let ok = 0;
  let degraded = 0;
  let failed = 0;
  for (const t of tasks) {
    const outcome = workerOutcome(t, failureForTask.get(t.id));
    if (outcome === "ok") ok += 1;
    else if (outcome === "degraded") degraded += 1;
    else if (outcome === "failed") failed += 1;
  }
  const finished = ok + degraded + failed;
  const okPct = Math.round((ok / total) * 100);
  const degradedPct = Math.round((degraded / total) * 100);
  const failedPct = Math.round((failed / total) * 100);
  const allOk = ok === total;
  const countText = [
    `${finished}/${total}`,
    failed > 0 ? `${failed} failed` : "",
    degraded > 0 ? `${degraded} degraded` : "",
  ].filter(Boolean).join(" · ");
  return (
    <div
      className="flex items-center gap-2"
      role="progressbar"
      aria-label={`${finished} of ${total} workers finished, ${failed} failed, ${degraded} degraded`}
      aria-valuemin={0}
      aria-valuemax={total}
      aria-valuenow={finished}
    >
      <div className="flex-1 h-px bg-edge/50 overflow-hidden flex">
        {okPct > 0 && (
          <div className="h-full bg-good transition-all duration-500" style={{ width: `${okPct}%` }} />
        )}
        {degradedPct > 0 && (
          <div className="h-full bg-warn transition-all duration-500" style={{ width: `${degradedPct}%` }} />
        )}
        {failedPct > 0 && (
          <div className="h-full bg-risk transition-all duration-500" style={{ width: `${failedPct}%` }} />
        )}
      </div>
      {!allOk && (
        <span className={`text-[9px] tabular-nums shrink-0 ${
          failed > 0 ? "text-risk/80" : degraded > 0 ? "text-warn/80" : "text-faint"
        }`}>
          {countText}
        </span>
      )}
    </div>
  );
}

function WorkerModelSlot({
  view,
}: {
  view: WorkerModelView;
}) {
  if (!view.slot) return null;
  const title = view.pending
    ? "Model routing in progress"
    : view.residual
      ? "No model recorded"
      : `Model: ${view.display || view.rawModel || view.slot}`;
  return (
    <span className="inline-flex items-center gap-1 min-w-0 max-w-full flex-wrap">
      <Cpu size={9} className={`shrink-0 ${view.pending || view.residual ? "text-faint/60" : "text-accent/65"}`} />
      <Tooltip label={title} className="min-w-0 max-w-full">
        <span
          className={`font-mono text-[9px] truncate min-w-0 max-w-full ${
            view.pending
              ? "text-faint italic"
              : view.residual
                ? "text-faint"
                : "text-accent/85"
          }`}
          aria-label={title}
        >
          {view.slot}
        </span>
      </Tooltip>
      {view.pinned && (
        <span
          className="text-[7.5px] text-faint uppercase tracking-[0.12em] shrink-0"
          title="explicit_pin · not auto-routed"
        >
          pinned
        </span>
      )}
    </span>
  );
}

function UnmatchedRoutingNote({
  arts,
  hasTasks,
  headerModel,
}: {
  arts: Artifact[];
  hasTasks: boolean;
  headerModel: string;
}) {
  if (arts.length === 0) return null;
  if (!hasTasks && !unmatchedAddsTruth(arts, headerModel)) return null;
  const firstModel = (arts[0].model || "").trim() || "unresolved";
  const label = arts.length === 1
    ? `Unmatched routing · ${firstModel} · no matching worker`
    : `Unmatched routing · ${arts.length} routes · no matching worker`;
  return (
    <div
      className="text-[9px] text-faint leading-relaxed px-0.5 py-0.5"
      title="ROUTING artifact with no matching worker task_id"
    >
      {label}
    </div>
  );
}

export default function SwarmPane() {
  // Seeded so an instance mounting after the project-selected event scopes
  // to the same repo as its siblings instead of the unscoped default view.
  const [selectedProjectRoot, setSelectedProjectRoot] = useState(lastSelectedProjectRoot);
  const projectSwitching = useProjectSwitching();
  const [expandedAlts, setExpandedAlts] = useState<Record<string, boolean>>({});
  const [expandedTasks, setExpandedTasks] = useState<Record<string, boolean>>({});
  const [expandedFindings, setExpandedFindings] = useState<Record<string, boolean>>({});
  // Findings section open/closed per job. Default open (missing key); user toggle sticks.
  const [findingsOpen, setFindingsOpen] = useState<Record<string, boolean>>({});
  const scopedRepo = selectedProjectRoot || undefined;
  const scopedRepoRef = useRef(scopedRepo);
  scopedRepoRef.current = scopedRepo;

  const [expandedJobs, setExpandedJobs] = useState<Record<string, boolean>>(() => loadExpanded(scopedRepo));
  const [dismissed, setDismissed] = useState<Set<string>>(() => loadDismissed(scopedRepo));
  const [finishedOpen, setFinishedOpen] = useState(false);
  const [jobFilter, setJobFilter] = useState<JobFilter>("all");
  const [sortOrder, setSortOrder] = useState<SortOrder>("newest");
  const [jobScope, setJobScope] = useState<JobScope>(() => loadJobScope());
  const [activeSessionId, setActiveSessionId] = useState("");
  // Job ids we have asked the backend to cancel. Held in local view state so the
  // row can show a subtle 'cancelling...' affordance immediately, before the next
  // poll reflects the terminal 'cancelled' status from /api/swarm/live.
  const [cancelling, setCancelling] = useState<Set<string>>(new Set());
  // Job ids currently fetching full artifacts after expand (slim live payload).
  const [loadingArts, setLoadingArts] = useState<Set<string>>(new Set());
  // Bumped every second so relative "last activity" times re-render while a job
  // runs, making a live worker visibly move rather than freeze between polls.
  const [nowTick, setNowTick] = useState(() => Date.now());

  useEffect(() => {
    if (!scopedRepo) {
      setActiveSessionId("");
      return;
    }
    let cancelled = false;
    api.sessions(scopedRepo).then((rows) => {
      if (cancelled) return;
      const active = (rows || []).find((s) => s.active);
      setActiveSessionId(active?.id || "");
    }).catch(() => {
      if (!cancelled) setActiveSessionId("");
    });
    return () => { cancelled = true; };
  }, [scopedRepo]);

  const toggleTask = (id: string) => setExpandedTasks((p) => ({ ...p, [id]: !p[id] }));
  const toggleFinding = (id: string) => setExpandedFindings((p) => ({ ...p, [id]: !p[id] }));

  useEffect(() => {
    setDismissed(loadDismissed(scopedRepo));
    setExpandedJobs(loadExpanded(scopedRepo));
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
  const pendingOpenRef = useRef<{ jobId: string; artifactId?: string } | null>(null);

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

  // Transcript chrome (job_id chips / ActionCard KV) deep-links here: undismiss,
  // expand, hydrate artifacts, scroll the row into view. Also drains any job id
  // queued before this pane mounted (openAgentSwarmJob races focus-tab mount).
  const openSwarmJobById = useCallback((jobId: string, artifactId?: string) => {
    const id = (jobId || "").trim();
    const artifact = (artifactId || "").trim();
    if (!id) return;
    pendingOpenRef.current = { jobId: id, ...(artifact ? { artifactId: artifact } : {}) };
    setDismissed((prev) => {
      if (!prev.has(id)) return prev;
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
    setExpandedJobs((prev) => {
      const updated = { ...prev, [id]: true };
      saveExpanded(scopedRepoRef.current, updated);
      return updated;
    });
    setFinishedOpen(true);
    setJobFilter("all");
    if (artifact) {
      setFindingsOpen((prev) => ({ ...prev, [id]: true }));
    }
    const job = dataRef.current?.jobs?.find((j) => j.id === id);
    if (job) ensureFullArtifacts(job);
    const scrollToTarget = () => {
      const escape =
        typeof CSS !== "undefined" && typeof CSS.escape === "function"
          ? CSS.escape
          : (s: string) => s.replace(/["\\]/g, "\\$&");
      const el = document.querySelector(`[data-job-id="${escape(id)}"]`);
      if (artifact && el) {
        const finding = el.querySelector<HTMLElement>(
          `[data-artifact-ids~="${escape(artifact)}"]`,
        );
        if (finding) {
          const findingId = finding.dataset.findingId;
          if (findingId) {
            setExpandedFindings((prev) => ({ ...prev, [findingId]: true }));
          }
          finding.scrollIntoView({ block: "center" });
          pendingOpenRef.current = null;
          clearPendingSwarmOpenJob();
          return;
        }
      }
      el?.scrollIntoView({ block: "nearest" });
      if (el && !artifact) {
        pendingOpenRef.current = null;
        clearPendingSwarmOpenJob();
      }
    };
    requestAnimationFrame(() => {
      requestAnimationFrame(scrollToTarget);
    });
    window.setTimeout(scrollToTarget, 80);
    if (artifact) {
      window.setTimeout(scrollToTarget, 250);
      window.setTimeout(scrollToTarget, 600);
    }
  }, [ensureFullArtifacts]);

  useEffect(() => {
    const pending = pendingOpenRef.current;
    if (!pending) return;
    if (!(data?.jobs || []).some((job) => job.id === pending.jobId)) return;
    openSwarmJobById(pending.jobId, pending.artifactId);
  }, [data, openSwarmJobById]);

  useEffect(() => {
    const pending = peekPendingSwarmOpenJob();
    const pendingArtifact = peekPendingSwarmOpenArtifact();
    if (pending) openSwarmJobById(pending, pendingArtifact || undefined);

    const onOpenSwarmJob = (e: Event) => {
      const detail = (e as CustomEvent<{ jobId?: string; artifactId?: string }>).detail;
      const jobId = String(detail?.jobId || "").trim();
      const artifactId = String(detail?.artifactId || "").trim();
      if (!jobId) return;
      // Keep the queue until the target DOM exists; the pane can remount while
      // the right rail opens or before the scoped live payload arrives.
      openSwarmJobById(jobId, artifactId || undefined);
    };
    window.addEventListener("harness-open-swarm-job", onOpenSwarmJob as EventListener);
    return () => {
      window.removeEventListener("harness-open-swarm-job", onOpenSwarmJob as EventListener);
    };
  }, [openSwarmJobById]);

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

  const allJobs = filterJobsByScope(data?.jobs || [], jobScope, activeSessionId);
  // Clear/dismiss is archive chrome for finished runs only. Live (and pending)
  // jobs must stay visible even if their id was previously dismissed — otherwise
  // a CLI-started swarm looks "gone" while workers are still running, and pilots
  // burn tokens inventing recovery paths.
  //
  // If a dismissed id reappears as live, drop it from the dismiss set so its
  // later terminal transition does not vanish again into "Show N hidden".
  useEffect(() => {
    const liveIds = allJobs.filter((j) => !isTerminal(j)).map((j) => j.id);
    if (liveIds.length === 0) return;
    setDismissed((prev) => {
      let changed = false;
      const next = new Set(prev);
      for (const id of liveIds) {
        if (next.delete(id)) changed = true;
      }
      return changed ? next : prev;
    });
  }, [allJobs]);

  const undismissedJobs = allJobs.filter((j) => !isTerminal(j) || !dismissed.has(j.id));
  const matchesJobFilter = (j: Job) => {
    const status = jobStatus(j);
    if (jobFilter === "active") return status === "pending" || status === "in_progress";
    if (jobFilter === "completed") return status === "completed" && j.outcome?.trustworthy !== false;
    if (jobFilter === "failed") return status === "failed";
    if (jobFilter === "untrustworthy") return status === "completed" && j.outcome?.trustworthy === false;
    if (jobFilter === "cancelled") return status === "cancelled";
    return true;
  };
  const compareJobs = (a: Job, b: Job) => {
    const aTime = timestampMs(a.created_at) ?? timestampMs(a.updated_at);
    const bTime = timestampMs(b.created_at) ?? timestampMs(b.updated_at);
    if (aTime === null || bTime === null) {
      if (aTime !== bTime) return aTime === null ? 1 : -1;
      return a.id.localeCompare(b.id);
    }
    const byTime = aTime - bTime;
    if (byTime !== 0) return sortOrder === "oldest" ? byTime : -byTime;
    return a.id.localeCompare(b.id);
  };
  const visibleJobs = undismissedJobs.filter(matchesJobFilter).sort(compareJobs);
  const running = visibleJobs.filter((j) => !isTerminal(j));
  const finished = visibleJobs.filter((j) => isTerminal(j));
  const failedCount = finished.filter((j) => jobStatus(j) === "failed").length;
  const cancelledCount = finished.filter((j) => jobStatus(j) === "cancelled").length;
  const warningCount = finished.filter(
    (j) => jobStatus(j) === "completed" && j.outcome?.trustworthy === false,
  ).length;
  const completedCount = finished.filter(
    (j) => jobStatus(j) === "completed" && j.outcome?.trustworthy !== false,
  ).length;
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
      for (const j of undismissedJobs) {
        if (isTerminal(j)) next.add(j.id);
      }
      return next;
    });
  const restoreDismissed = () => setDismissed(new Set());
  const hiddenCount = allJobs.filter((j) => isTerminal(j) && dismissed.has(j.id)).length;


  // One card renderer, reused by both the running list and the Finished
  // accordion. Defined in-scope so it closes over the expand/dismiss state
  // instead of threading a dozen props.
  const renderJob = (j: Job) => {
    const st = jobStatus(j);
    const outcomeWarning = st === "completed" && j.outcome?.trustworthy === false;
    const manualExpanded = expandedJobs[j.id];
    const isExpanded = manualExpanded !== undefined ? manualExpanded : (st === "in_progress");
    const phase = jobPhase(j);

    const artifacts = jobArtifactList(j);
    const routingArts = dedupeRouting(
      artifacts.filter((a: Artifact) => (a.type || "").toUpperCase() === "ROUTING"),
    );
    const streamArts = artifacts.filter((a: Artifact) => (a.type || "").toUpperCase() !== "ROUTING");
    const failureForTask = failureArtifactsByTaskId(streamArts);
    const tasks = j.tasks || [];
    const { routingForTask, unmatched: unmatchedRouting } = associateRoutingToTasks(routingArts, tasks);
    // Prefer the deduped final routing decision (fallback/escalation wins) over
    // the job.model field so a stale initial router pick never badges the header.
    const primaryRouting = tasks.length === 0
      ? bestUnscopedRouting(routingArts) || routingArts.find((a: Artifact) => a.model) || routingArts[0]
      : routingArts.find((a: Artifact) => a.model) || routingArts[0];
    const routerModel = primaryRouting?.model || j.model || "";
    const attestedPolicy = primaryRouting ? routingPolicy(primaryRouting) : "";
    const workerCount = tasks.length;
    const adapter = j.adapter || tasks[0]?.adapter || "";
    const displayModel = displayModelId(routerModel || "", {
      policy: attestedPolicy,
      adapterFallback: isEngineOnlyModelId(adapter) ? "" : adapter,
    });
    // Canonical task rows own worker→model truth. Job-level model / routing…
    // stays only when there are zero task rows (including pre-task in-flight).
    const headerModel = workerCount === 0 ? displayModel : "";
    const showJobRoutingPlaceholder = workerCount === 0 && !headerModel && st === "in_progress";
    const showUnmatched = unmatchedRouting.length > 0
      && (workerCount > 0 || unmatchedAddsTruth(unmatchedRouting, headerModel));
    const terminal = isTerminal(j);
    const savings = jobSavings(j);
    const showJobTokens = jobTokens(j) > 0;
    const showJobCompactTokens = jobCompactTokens(j) > 0;
    const showJobCost = hasMeaningfulJobCost(j);
    const showJobSavings = savings.total > 0;
    const hasHeaderMeters = showJobTokens || showJobCompactTokens || showJobCost || showJobSavings;

    const toggle = () => {
      const next = !isExpanded;
      setExpandedJobs((prev) => {
        const updated = { ...prev, [j.id]: next };
        saveExpanded(scopedRepoRef.current, updated);
        return updated;
      });
      if (next) ensureFullArtifacts(j);
    };

    return (
      <div
        key={j.id}
        data-job-id={j.id}
        // shrink-0 is load-bearing: as a flex child of the flex-col scroll list,
        // an overflow-hidden card is allowed to shrink BELOW its content, so it
        // collapsed and clipped its own findings instead of pushing the list into
        // overflow. Pinning shrink-0 keeps the card at full content height so the
        // list actually scrolls.
        className={`shrink-0 rounded-md border bg-panel2/20 flex flex-col overflow-hidden transition-colors ${
          st === "in_progress"
            ? "border-accent/30"
            : st === "failed"
            ? "border-risk/25"
            : outcomeWarning
            ? "border-warn/35"
            : st === "completed"
            ? "border-good/25"
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
          aria-expanded={isExpanded}
          aria-label={`${j.goal || "Swarm job"}, ${phase.label}`}
          onClick={toggle}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } }}
          className={`w-full flex flex-col gap-0 p-2 hover:bg-panel2/35 text-left transition-colors select-none cursor-pointer ${DISCLOSURE_FOCUS}`}
        >
          <div className="flex items-center justify-between w-full gap-2">
            <div className="flex items-center gap-2 min-w-0 flex-1">
              <span className="shrink-0 text-faint">
                {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              </span>
              <span className="shrink-0">
                {st === "in_progress" ? (
                  <Loader2 size={12} className="animate-spin semantic-activity-spinner text-accent" />
                ) : st === "failed" ? (
                  <XCircle size={12} className="text-risk" />
                ) : outcomeWarning ? (
                  <Circle size={12} className="text-warn" />
                ) : st === "completed" ? (
                  <CheckCircle2 size={12} className="text-good" />
                ) : st === "cancelled" ? (
                  <XCircle size={12} className="text-muted" />
                ) : (
                  <Circle size={12} className="text-muted" />
                )}
              </span>
              <Tooltip label={j.goal} className="font-semibold text-[11px] text-txt truncate">
                {j.goal}
              </Tooltip>
              <button
                type="button"
                className="shrink-0 font-mono text-[9px] text-faint hover:text-muted"
                title="Copy job identifier"
                aria-label={jobIdentifier(j.id)}
                onClick={(e) => {
                  e.stopPropagation();
                  void navigator.clipboard?.writeText(jobIdentifier(j.id));
                }}
              >
                {jobIdentifier(j.id)}
              </button>
            </div>
            <div className="flex items-center gap-2 shrink-0 text-[10px]">
              {/* Kill: running jobs only. Best-effort cooperative cancel on the
                  backend. Shows 'cancelling...' until the next poll flips the job
                  to a terminal state. */}
              {st === "in_progress" && (
                cancelling.has(j.id) ? (
                  <span className="text-[9px] text-risk/70 italic tabular-nums">cancelling...</span>
                ) : (
                  <button
                    type="button"
                    onClick={(e) => { e.stopPropagation(); void cancelJob(j.id); }}
                    onKeyDown={(e) => e.stopPropagation()}
                    title="Cancel this job"
                    aria-label="Cancel this job"
                    className={`text-faint/50 hover:text-risk transition-colors ${DISCLOSURE_FOCUS}`}
                  >
                    <X size={12} />
                  </button>
                )
              )}
              {/* Dismiss: terminal runs only -- hiding a live worker would be
                  confusing. Non-destructive; the run stays in PM history. */}
              {terminal && (
                <button
                  type="button"
                  onClick={(e) => { e.stopPropagation(); dismissJob(j.id); }}
                  onKeyDown={(e) => e.stopPropagation()}
                  title="Dismiss from tracker (stays in Puppetmaster history)"
                  aria-label="Dismiss from tracker (stays in Puppetmaster history)"
                  className={`text-faint/50 hover:text-risk transition-colors ${DISCLOSURE_FOCUS}`}
                >
                  <X size={12} />
                </button>
              )}
            </div>
          </div>

          {/* One quiet receipt line: spend first, then execution identity.
              Rare provenance remains textual so the header never becomes a
              dashboard of pills. */}
          {(hasHeaderMeters || headerModel || showJobRoutingPlaceholder || workerCount > 0 || adapter || j.source === "cli" || (workerCount === 0 && attestedPolicy === "explicit_pin")) && (
            <div className="flex items-center gap-x-2 gap-y-1 pl-6 pr-1 mt-1.5 flex-wrap text-[9px]">
              {hasHeaderMeters && (
                <span className="inline-flex items-center gap-1.5 min-w-0 flex-wrap font-mono text-muted tabular-nums">
                  {showJobTokens && <span>{jobTokens(j).toLocaleString()}t</span>}
                  {showJobCompactTokens && (
                    <span className="text-accent/80">{jobCompactTokens(j).toLocaleString()} compact</span>
                  )}
                  {showJobCost && (
                    <span
                      className="text-good/85"
                      title={namedSpend(j.est_cost_usd, spendBasisFor(j))}
                    >
                      {namedSpend(j.est_cost_usd, spendBasisFor(j))}
                    </span>
                  )}
                  {showJobSavings && <SavingsChip parts={savings} className="font-sans" />}
                </span>
              )}
              {hasHeaderMeters && (headerModel || showJobRoutingPlaceholder || workerCount > 0 || adapter) && (
                <span className="h-2.5 w-px bg-edge/70" aria-hidden="true" />
              )}
              {headerModel ? (
                <span className="inline-flex items-center gap-1 min-w-0 max-w-full font-mono text-accent/85" title={`Model: ${headerModel}`}>
                  <Cpu size={9} className="shrink-0" />
                  <span className="truncate min-w-0">{headerModel}</span>
                </span>
              ) : showJobRoutingPlaceholder ? (
                <span
                  className="flex items-center gap-1 font-mono text-faint italic"
                  title="Model routing in progress"
                >
                  <Cpu size={9} /> routing…
                </span>
              ) : null}
              {workerCount === 0 && attestedPolicy === "explicit_pin" && (
                <span
                  className="text-[8px] text-faint uppercase tracking-wide"
                  title="explicit_pin · not auto-routed"
                >
                  pin
                </span>
              )}
              {workerCount > 0 && (
                <span className="text-muted tabular-nums">
                  {workerCount} worker{workerCount > 1 ? "s" : ""}
                </span>
              )}
              {adapter && adapter.toLowerCase() !== displayModel.toLowerCase() && (
                <span className="text-faint lowercase">{adapter}</span>
              )}
              {j.source === "cli" && (
                <span
                  className="text-muted uppercase tracking-[0.1em]"
                  title="Started outside Marionette (Cursor MCP or terminal Puppetmaster) for this workspace"
                >
                  external
                </span>
              )}
              {j.reuse_status && ["reused", "partial", "invalidated", "fresh"].includes(
                String(j.reuse_status).toLowerCase(),
              ) && (
                <span
                  className="text-[9px] text-muted bg-panel2/40 border border-edge/50 px-1.5 py-0.5 rounded font-mono"
                  title={
                    (Array.isArray(j.invalidated_paths) && j.invalidated_paths.length
                      ? `invalidated: ${j.invalidated_paths.slice(0, 6).join(", ")}${j.invalidated_paths.length > 6 ? ` (+${j.invalidated_paths.length - 6} more)` : ""}`
                      : "")
                    || j.reuse_reason
                    || (j.source_job_id ? `Source job ${j.source_job_id}` : "Validation reuse status")
                  }
                >
                  {String(j.reuse_status).toLowerCase() === "partial"
                    ? "partially reverified"
                    : String(j.reuse_status).toLowerCase()}
                </span>
              )}
              {Array.isArray(j.invalidated_paths) && j.invalidated_paths.length > 0 && (
                <span
                  className="text-[9px] text-faint font-mono truncate max-w-[40%]"
                  title={j.invalidated_paths.join(", ")}
                >
                  {j.invalidated_paths.slice(0, 2).join(", ")}
                  {j.invalidated_paths.length > 2 ? ` +${j.invalidated_paths.length - 2}` : ""}
                </span>
              )}
              {!jobAccountingOwned(j) && (
                <span
                  className="text-[9px] text-faint bg-panel2/30 border border-edge/40 px-1.5 py-0.5 rounded"
                  title="Visible for cancellation only — does not affect Marionette session cost or savings"
                >
                  visibility only
                </span>
              )}
            </div>
          )}

          {outcomeWarning && j.outcome?.reasons?.length ? (
            <div className="pl-6 pr-1 mt-1 text-[9.5px] text-warn/90">
              {j.outcome.reasons[0]}
            </div>
          ) : null}

          {/* Phase strip + label -- the at-a-glance "where is this swarm". */}
          <div className="flex items-center gap-2 pl-6 pr-1 mt-2">
            <div className="flex-1"><PhaseStrip job={j} phase={phase} /></div>
            <span className={`text-[9px] font-medium tabular-nums shrink-0 ${
              phase.key === "cancelled"
                ? "text-muted"
                : phase.failed
                ? "text-risk/80"
                : outcomeWarning
                ? "text-warn/80"
                : phase.key === "done"
                ? "text-good/80"
                : "text-accent/80"
            }`}>
              {phase.label}
            </span>
          </div>

          {/* Last activity supplies motion without repeating the receipt. */}
          {st === "in_progress" && (() => {
            const since = relativeSince(j.updated_at ?? j.created_at, nowTick);
            if (!since) return null;
            return (
              <div className="flex items-center gap-2 pl-6 pr-1 mt-1 text-[9px] text-faint tabular-nums">
                <span className="flex items-center gap-1">
                  <Activity size={9} className="text-accent/60 animate-pulse" />
                  {since}
                </span>
              </div>
            );
          })()}
        </div>

        {/* Expanded details */}
        {isExpanded && (
          <div className="px-2 pb-2 pt-1 flex flex-col gap-2 bg-panel2/10">
            {/* Workers first -- never a standalone Routing card stack. */}
            {tasks.length > 0 && (
              <div className="border-t border-edge/25 pt-2 flex flex-col gap-1.5">
                <div className="flex items-center justify-between">
                  <span className="text-[8.5px] uppercase tracking-[0.14em] text-faint font-medium">Workers ({tasks.length})</span>
                </div>
                <WorkerProgress tasks={tasks} failureForTask={failureForTask} />
                <div className="flex flex-col divide-y divide-edge/20 mt-0.5">
                  {tasks.map((task) => {
                    const tExpanded = !!expandedTasks[task.id];
                    const routing = routingForTask.get(task.id);
                    const view = workerModelView(task, routing);
                    const failureArtifact = failureForTask.get(task.id);
                    const failureClass = String(failureArtifact?.failure || "").trim();
                    const sourceFailureReason = failureReason(failureArtifact);
                    const adapterLabel = (task.adapter || "").trim();
                    const provider = (routing?.provider || "").trim();
                    const createdBy = routing?.created_by || "";
                    const hasRejected = !!(routing?.rejected && routing.rejected.length > 0);
                    const altKey = `${j.id}:${task.id}`;
                    const altsExpanded = !!expandedAlts[altKey];
                    const spend = workerSpend(task, j);
                    const costValue = spend?.cost;
                    const costEstimated = spend?.estimated ?? true;
                    const showTokens = (task.tokens ?? 0) > 0;
                    const showCost = spend != null && (
                      isKnownPositiveCost(costValue)
                      || isProviderAttestedExactZero(costValue, costEstimated)
                    );
                    const showSpend = showTokens || showCost;
                    const roleLabel = task.role || "Worker";
                    const detailsId = `swarm-worker-${task.id}`;
                    const outcome = workerOutcome(task, failureArtifact);
                    const rawStatus = (task.status || "").trim();
                    const ariaBits = [roleLabel, outcome];
                    if (rawStatus && rawStatus.toLowerCase() !== outcome) ariaBits.push(rawStatus);
                    if (view.slot) ariaBits.push(view.slot);
                    return (
                      <div
                        key={task.id}
                        className="py-1.5 flex flex-col text-[10px]"
                      >
                        <div
                          role="button"
                          tabIndex={0}
                          aria-expanded={tExpanded}
                          aria-controls={detailsId}
                          aria-label={ariaBits.join(", ")}
                          onClick={() => toggleTask(task.id)}
                          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleTask(task.id); } }}
                          className={`group flex items-start gap-2 min-w-0 px-1 py-0.5 cursor-pointer hover:bg-panel2/25 transition-colors ${DISCLOSURE_FOCUS}`}
                        >
                          <span className="shrink-0 mt-0.5">
                            <WorkerOutcomeGlyph outcome={outcome} />
                          </span>
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-1 min-w-0">
                              <span className="min-w-0 flex-1 truncate font-semibold text-txt">
                                {roleLabel}
                              </span>
                              {(outcome === "degraded" || outcome === "failed") && (
                                <span
                                  className={`shrink-0 text-[8.5px] font-medium ${
                                    outcome === "failed" ? "text-risk/90" : "text-warn/90"
                                  }`}
                                >
                                  {outcome}
                                </span>
                              )}
                              <span className="shrink-0 text-faint/60 group-hover:text-faint transition-colors">
                                {tExpanded
                                  ? <ChevronDown size={9} />
                                  : <ChevronRight size={9} />}
                              </span>
                            </div>
                            <div className="mt-0.5 flex items-center gap-x-2 gap-y-0.5 min-w-0 flex-wrap">
                              <WorkerModelSlot view={view} />
                              {showSpend && (
                                <span className="text-muted font-mono text-[8.5px] flex items-center gap-1 tabular-nums">
                                  {showTokens && (
                                    <span>{Number(task.tokens).toLocaleString()}t</span>
                                  )}
                                  {showCost && (
                                    <span className="text-good/85">
                                      {spend
                                        ? namedSpend(costValue, spend.basis)
                                        : formatWorkerCost(
                                            costValue,
                                            costEstimated,
                                            isPlanBilledRouting(routing),
                                          )}
                                    </span>
                                  )}
                                </span>
                              )}
                            </div>
                          </div>
                        </div>
                        {tExpanded && (
                          <div
                            id={detailsId}
                            className="ml-5 mt-1 border-l border-edge/35 pl-2 text-[9px] font-mono text-faint flex flex-col gap-1.5"
                          >
                            {(failureClass || sourceFailureReason) && (
                              <div className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5">
                                {failureClass && <><span>failure</span><span className="text-risk/85 break-all">{failureClass}</span></>}
                                {sourceFailureReason && <><span>reason</span><span className="text-muted whitespace-pre-wrap break-words font-sans">{sourceFailureReason}</span></>}
                              </div>
                            )}
                            <div className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5">
                              <span>task</span><span className="text-muted break-all">{task.id}</span>
                              {view.rawModel && <><span>model</span><span className="text-muted break-all">{view.rawModel}</span></>}
                              {adapterLabel && <><span>adapter</span><span className="text-muted break-all">{adapterLabel}</span></>}
                              {view.policy ? (
                                <><span>policy</span><span className="text-muted break-all">{view.policy}{view.pinned ? " · pin" : ""}</span></>
                              ) : view.routing ? (
                                <><span>policy</span><span className="text-muted break-all">Pin attribution unknown</span></>
                              ) : null}
                              {provider && <><span>provider</span><span className="text-muted break-all">{provider}</span></>}
                              {createdBy === "router-fallback" && <><span>route</span><span className="text-muted">fallback</span></>}
                              {createdBy === "router-escalation" && <><span>route</span><span className="text-muted">escalation</span></>}
                              {hasRejected && (
                                <>
                                  <span>rejected</span>
                                  <span>
                                    <button
                                      type="button"
                                      aria-expanded={altsExpanded}
                                      onClick={(e) => { e.stopPropagation(); setExpandedAlts((prev) => ({ ...prev, [altKey]: !altsExpanded })); }}
                                      onKeyDown={(e) => e.stopPropagation()}
                                      className={`text-[9px] text-faint/80 hover:text-muted inline-flex items-center gap-0.5 ${DISCLOSURE_FOCUS}`}
                                    >
                                      {altsExpanded ? <ChevronDown size={9} /> : <ChevronRight size={9} />}
                                      {routing?.rejected?.length} alternatives
                                    </button>
                                    {altsExpanded && (
                                      <span className="mt-1 flex flex-wrap gap-1">
                                        {routing?.rejected?.map((rej: { model: string; reason: string }, ridx: number) => (
                                          <Tooltip
                                            key={ridx}
                                            label={rej.reason}
                                            className="font-mono text-[8.5px] text-faint bg-panel2/30 border border-edge/40 px-1.5 py-0.5 rounded cursor-default"
                                          >
                                            {rej.model}
                                          </Tooltip>
                                        ))}
                                      </span>
                                    )}
                                  </span>
                                </>
                              )}
                            </div>
                            {task.completed_at && (
                              <div className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5">
                                <span>completed</span><span className="text-muted break-all">{task.completed_at}</span>
                              </div>
                            )}
                            {task.instruction && (
                              <div className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5">
                                <span>instruction</span><span className="text-muted whitespace-pre-wrap break-words font-sans">{task.instruction}</span>
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {showUnmatched && (
              <UnmatchedRoutingNote
                arts={unmatchedRouting}
                hasTasks={workerCount > 0}
                headerModel={headerModel}
              />
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
                <div className="pr-1 flex flex-col gap-1 border border-edge/40 rounded p-1.5 bg-panel2/20">
                  {findingRows.map(({ art, count, artifactIds }, idx: number) => {
                    const fid = art.id || `f${idx}`;
                    const fExpanded = !!expandedFindings[fid];
                    const echoWarn = (art.type || "").toUpperCase() === "FINDING"
                      && looksLikePromptEcho(art.headline || "");
                    const detailStr = art.detail == null
                      ? ""
                      : (typeof art.detail === "string"
                          ? art.detail
                          : (() => { try { return JSON.stringify(art.detail, null, 2); } catch { return String(art.detail); } })());
                    return (
                    <div
                      key={fid}
                      data-finding-id={fid}
                      data-artifact-ids={artifactIds.join(" ")}
                      className="text-[9.5px] border-b border-edge/10 pb-1 last:border-0 last:pb-0 flex flex-col gap-0.5"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-bold text-accent uppercase tracking-wider text-[8px] flex items-center gap-1">
                          {art.type}
                          {count > 1 && <span className="text-faint bg-edge/20 px-1 rounded normal-case tracking-normal">x{count}</span>}
                          {echoWarn && (
                            <span
                              className="text-faint bg-edge/15 px-1 rounded normal-case tracking-normal font-medium"
                              title="Headline looks like the worker echoed its prompt"
                            >
                              looks like prompt echo
                            </span>
                          )}
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
                        <div className="mt-1 ml-4 text-[9px] text-muted whitespace-pre-wrap break-words bg-panel2/30 border border-edge/40 rounded p-1.5 font-mono max-h-72 overflow-auto">
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

            {tasks.length === 0 && streamArts.length === 0 && !showUnmatched && (
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
    <div className={`flex flex-col h-full overflow-hidden bg-transparent ${panelOpacityClass(panelDimmed, isShowingStale)}`}>
      {/* Persistent header: the tracker always announces itself, with live
          aggregate counts, so it reads as a dashboard even at rest. */}
      <div className="shrink-0 flex items-center justify-between px-3 py-2 border-b border-[var(--shell-panel-border)] select-none">
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
              <Loader2 size={10} className="animate-spin semantic-activity-spinner" /> {runningCount} running
            </span>
          )}
          {completedCount > 0 && (
            <span className="flex items-center gap-1 text-good/80">
              <CheckCircle2 size={10} /> {completedCount}
            </span>
          )}
        </div>
      </div>

      <div className="shrink-0 grid grid-cols-2 gap-1.5 px-2 py-1.5 border-b border-[var(--shell-panel-border)] bg-panel2/10">
        <select
          aria-label="Filter swarms"
          value={jobFilter}
          onChange={(e) => {
            const next = e.target.value as JobFilter;
            setJobFilter(next);
            if (next !== "all" && next !== "active") setFinishedOpen(true);
          }}
          className="w-full h-6 rounded border border-edge bg-panel2/40 px-1.5 text-[10px] text-muted focus:outline-none focus:border-accent/60"
        >
          <option value="all">All statuses</option>
          <option value="active">Active</option>
          <option value="completed">Completed</option>
          <option value="failed">Failed</option>
          <option value="untrustworthy">Untrustworthy</option>
          <option value="cancelled">Cancelled</option>
        </select>
        <select
          aria-label="Sort swarms"
          value={sortOrder}
          onChange={(e) => setSortOrder(e.target.value as SortOrder)}
          className="w-full h-6 rounded border border-edge bg-panel2/40 px-1.5 text-[10px] text-muted focus:outline-none focus:border-accent/60"
        >
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
        </select>
        <div className="col-span-2 flex h-6 overflow-hidden rounded border border-edge">
          {(["session", "repo"] as const).map((scope) => (
            <button
              key={scope}
              type="button"
              aria-pressed={jobScope === scope}
              aria-label={scope === "session" ? "This session" : "This repo, ever"}
              onClick={() => { setJobScope(scope); saveJobScope(scope); }}
              className={`flex-1 text-[10px] ${jobScope === scope ? "bg-accent/15 text-txt" : "bg-panel2/40 text-muted hover:text-txt"}`}
            >
              {scope === "session" ? "This session" : "This repo"}
            </button>
          ))}
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
                : undismissedJobs.length > 0
                  ? "No swarm jobs match this filter"
                  : hiddenCount > 0 ? "All swarm jobs cleared" : "No swarm jobs yet"}
            </span>
            {undismissedJobs.length > 0 ? (
              <button
                onClick={() => setJobFilter("all")}
                className="text-[10.5px] text-accent hover:underline focus:outline-none"
              >
                Clear filter
              </button>
            ) : hiddenCount > 0 ? (
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
            {/* Active runs stay pinned above terminal history; sort applies within each group. */}
            {running.map(renderJob)}

            {/* Finished runs folded into a collapsible section so a long session
                stays a short list. Non-destructive: "Clear" only hides. */}
            {finished.length > 0 && (
              <div className="shrink-0 flex flex-col gap-2">
                <div className="swarm-finished-head flex items-center justify-between px-1 pt-0.5">
                  <button
                    onClick={() => setFinishedOpen((o) => !o)}
                    className="flex items-center gap-1 min-w-0 text-[10px] uppercase tracking-wider text-faint font-semibold hover:text-muted focus:outline-none"
                  >
                    {finishedOpen ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
                    <span className="whitespace-nowrap">
                      Finished{" "}
                      <span className="text-faint/60 normal-case tracking-normal">({finished.length})</span>
                    </span>
                    <span className="swarm-finished-chips flex flex-wrap items-center min-w-0">
                      {failedCount > 0 && (
                        <span className="swarm-chip swarm-chip-failed whitespace-nowrap text-risk/70 normal-case tracking-normal">{"\u00b7"} {failedCount} failed</span>
                      )}
                      {warningCount > 0 && (
                        <span className="swarm-chip swarm-chip-warn whitespace-nowrap text-warn/80 normal-case tracking-normal">{"\u00b7"} {warningCount} untrustworthy</span>
                      )}
                      {cancelledCount > 0 && (
                        <span className="swarm-chip swarm-chip-cancel whitespace-nowrap text-muted normal-case tracking-normal">{"\u00b7"} {cancelledCount} cancelled</span>
                      )}
                    </span>
                  </button>
                  <button
                    onClick={clearFinished}
                    title="Hide all finished runs from the tracker (stays in Puppetmaster history)"
                    className="shrink-0 whitespace-nowrap text-[9px] text-faint/70 hover:text-risk uppercase tracking-wider focus:outline-none"
                  >
                    Clear
                  </button>
                </div>
                {finishedOpen && finished.map(renderJob)}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
