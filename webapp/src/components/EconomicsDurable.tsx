import type { EconomicsData, EconomicsJobRow, EconomicsScope } from "../lib/api";
import { openAgentSwarmJob } from "../lib/agentLinks";

const OWNERSHIP: Array<{ value: EconomicsScope; label: string }> = [
  { value: "conversation", label: "This conversation" },
  { value: "repo", label: "This repo" },
  { value: "all_projects", label: "All projects" },
];

const PERIODS: Array<{ value: "all" | "30"; label: string }> = [
  { value: "all", label: "All time" },
  { value: "30", label: "Last 30 days" },
];

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** Unknown stays em-dash — never a measured $0. */
function fmtUnknownMoney(value: number | null | undefined): string {
  if (!isFiniteNumber(value)) return "—";
  if (value < 0.001 && value !== 0) return `$${value.toFixed(4)}`;
  if (value < 0.01 && value !== 0) return `$${value.toFixed(3)}`;
  return `$${value.toFixed(2)}`;
}


/** Headline total: measured + estimated when either is known; else legacy actual_marginal. */
export function jobHeadlineTotal(job: Pick<EconomicsJobRow, "measured_cost_usd" | "estimated_cost_usd" | "actual_marginal_usd">): number | null {
  const measured = job.measured_cost_usd;
  const estimated = job.estimated_cost_usd;
  const hasMeasured = isFiniteNumber(measured);
  const hasEstimated = isFiniteNumber(estimated);
  if (hasMeasured || hasEstimated) {
    return (hasMeasured ? measured : 0) + (hasEstimated ? estimated : 0);
  }
  return isFiniteNumber(job.actual_marginal_usd) ? job.actual_marginal_usd : null;
}

export default function EconomicsDurable({
  data,
  scope,
  onScopeChange,
  periodDays,
  onPeriodChange,
}: {
  data: EconomicsData | null;
  scope: EconomicsScope;
  onScopeChange: (scope: EconomicsScope) => void;
  periodDays: 30 | null;
  onPeriodChange: (periodDays: 30 | null) => void;
}) {
  const referenceId = data?.counterfactual?.reference_model_id
    || data?.savings?.counterfactual?.reference_model_id
    || "";
  const routingSaved = data?.savings?.routing?.saved_usd;
  const codegraphEst = data?.savings?.codegraph?.dollars_saved_est;
  const avoided = data?.counterfactual?.avoided_usd;
  const planRouted = data?.savings?.routing?.plan_routed_tasks ?? 0;
  const jobs = Array.isArray(data?.recent_jobs) ? data.recent_jobs : [];

  return (
    <div className="w-full px-3 pb-3 text-[11px] text-txt">
      <div className="text-[10px] uppercase tracking-wide text-faint">Durable</div>
      <p className="text-[10px] text-muted mb-2 leading-snug">
        {scope === "conversation"
          ? "Owned jobs for this conversation. Puppetmaster savings stay on This repo / All projects."
          : "Puppetmaster savings for the selected ownership. Last 30 days is a date cutoff on the selected ownership, not a different project set. App-run spend stays above. Vs reference is a list-price counterfactual, not Swarm Tracker receipt savings. $0 route estimates stay forecasts, not cash."}
      </p>

      <label className="flex items-center justify-between mb-2 text-faint">
        <span className="text-[10px] uppercase tracking-wide">Ownership</span>
        <select
          className="bg-transparent text-[11px] text-txt"
          value={scope === "window30" ? "repo" : scope}
          onChange={(event) => onScopeChange(event.target.value as EconomicsScope)}
          aria-label="Economics ownership"
        >
          {OWNERSHIP.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>

      <label className="flex items-center justify-between mb-2 text-faint">
        <span className="text-[10px] uppercase tracking-wide">Period</span>
        <select
          className="bg-transparent text-[11px] text-txt"
          value={periodDays === 30 ? "30" : "all"}
          onChange={(event) => onPeriodChange(event.target.value === "30" ? 30 : null)}
          aria-label="Economics period"
        >
          {PERIODS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>

      {data && data.available === false ? (
        <p className="text-[10px] text-muted mb-2 leading-snug">
          {data.error || "Durable economics unavailable."}
        </p>
      ) : null}

      {scope === "conversation" ? (
        jobs.length === 0 && data?.available !== false ? (
          <p className="text-[10px] text-muted mb-2 leading-snug">
            No owned jobs stamped for this conversation.
          </p>
        ) : null
      ) : (
      <>
      {referenceId ? (
        <div className="flex items-center justify-between mb-1 text-faint">
          <span>Reference model</span>
          <span className="tabular-nums text-right truncate pl-2">{referenceId}</span>
        </div>
      ) : null}

      {isFiniteNumber(routingSaved) && routingSaved > 0 ? (
        <div className="flex items-center justify-between mb-1 text-faint">
          <span>Routing saved (measured)</span>
          <span className="tabular-nums text-good/90">{fmtUnknownMoney(routingSaved)}</span>
        </div>
      ) : data?.available !== false && data?.savings && !isFiniteNumber(routingSaved) ? (
        <div className="flex items-center justify-between mb-1 text-faint">
          <span>Routing saved (measured)</span>
          <span className="tabular-nums">unknown basis</span>
        </div>
      ) : null}

      {isFiniteNumber(codegraphEst) ? (
        <div className="flex items-center justify-between mb-1 text-faint">
          <span>CodeGraph (estimated)</span>
          <span className="tabular-nums text-good/90">{fmtUnknownMoney(codegraphEst)}</span>
        </div>
      ) : null}

      {isFiniteNumber(avoided) ? (
        <div className="flex items-center justify-between mb-1 text-faint">
          <span title="List-price counterfactual vs the named reference model, not Swarm Tracker receipt savings.">
            {referenceId
              ? `Vs reference (${referenceId})`
              : "Vs reference"}
          </span>
          <span className="tabular-nums text-good/90">{fmtUnknownMoney(avoided)}</span>
        </div>
      ) : null}

      {planRouted > 0 ? (
        <div className="flex items-center justify-between mb-1 text-faint">
          <span>Plan-routed / $0-marginal</span>
          <span className="tabular-nums">{planRouted} tasks, not measured cash</span>
        </div>
      ) : null}
      </>
      )}

      {jobs.length > 0 ? (
        <div className="mt-3">
          <div className="text-[10px] uppercase tracking-wide text-faint mb-2">Recent jobs</div>
          {jobs.map((job) => {
            const owned = Boolean(job.accounting_owned);
            const modelIds = (job.models || []).map((model) => model.model_id || "").filter(Boolean);
            const measuredCost = isFiniteNumber(job.measured_cost_usd)
              ? job.measured_cost_usd
              : (!isFiniteNumber(job.estimated_cost_usd) && isFiniteNumber(job.actual_marginal_usd)
                ? job.actual_marginal_usd
                : null);
            const jobAvoided = owned ? job.counterfactual?.avoided_usd : null;
            return (
              <div key={job.job_id || `${job.source}-${job.status}`} className="mb-2">
                <div className="flex items-center justify-between text-faint">
                  {job.job_id ? (
                    <button
                      type="button"
                      className="truncate pr-2 font-mono text-accent/85 hover:underline underline-offset-2 cursor-pointer bg-transparent border-0 p-0 text-left"
                      onClick={() => openAgentSwarmJob(job.job_id || "")}
                    >
                      {job.job_id}
                    </button>
                  ) : (
                    <span className="truncate pr-2">Job</span>
                  )}
                </div>
                {modelIds.length > 0 ? (
                  <div className="flex items-start justify-between gap-2 text-faint pl-2">
                    <span>{modelIds.length === 1 ? "Model" : "Models"}</span>
                    <span className="font-mono text-right break-words min-w-0">{modelIds.join(", ")}</span>
                  </div>
                ) : null}
                {owned ? (
                  <>
                    <div className="flex items-center justify-between text-faint pl-2">
                      <span>Measured Cost</span>
                      <span className="tabular-nums shrink-0 text-warn/90">{fmtUnknownMoney(measuredCost)}</span>
                    </div>
                    <div className="flex items-center justify-between text-faint pl-2">
                      <span>Estimated Cost</span>
                      <span className="tabular-nums shrink-0 text-warn/90">{fmtUnknownMoney(job.estimated_cost_usd)}</span>
                    </div>
                    <div className="flex items-center justify-between text-faint pl-2">
                      <span title="List-price counterfactual vs the pane reference, not Swarm Tracker receipt savings.">Vs reference</span>
                      <span className="tabular-nums shrink-0 text-good/90">{fmtUnknownMoney(jobAvoided)}</span>
                    </div>
                  </>
                ) : null}
                {!owned ? <div className="text-faint pl-2">visible only</div> : null}
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
