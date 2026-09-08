import MetadataJobs from './MetadataJobs';
import { jobArtifactList, type Job, type Artifact, type Task } from "../lib/api";

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

// A job is "finished" once it can no longer change -- completed, failed, or
// cancelled. These are the runs we fold away so a long session doesn't stack
// into a wall.

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

/** How many workers finished degraded or failed. Parent chrome must not paint these as clean-green. */
export function jobDegradedWorkerCount(job: Job): number {
  const tasks = job.tasks || [];
  const arts = jobArtifactList(job);
  const failureForTask = failureArtifactsByTaskId(arts);
  let n = 0;
  for (const t of tasks) {
    const o = workerOutcome(t, failureForTask.get(t.id));
    if (o === "degraded" || o === "failed") n += 1;
  }
  return n;
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

function formatCost(cost: number, estimated?: boolean): string {
  if (!(cost > 0)) return "$0";
  const body = `$${cost.toFixed(4)}`;
  return estimated ? `~${body}` : body;
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

export default function SwarmPane({ enabled = true }: { enabled?: boolean }) {
  return <MetadataJobs enabled={enabled} />;
}
