import type { EconomicsData, EconomicsJobRow, EconomicsScope } from "../lib/api";
import { openAgentSwarmJob } from "../lib/agentLinks";

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
}: {
  data: EconomicsData | null;
  scope: EconomicsScope;
}) {
  const referenceId = data?.counterfactual?.reference_model_id
    || data?.savings?.counterfactual?.reference_model_id
    || "";
  const jobs = Array.isArray(data?.recent_jobs) ? data.recent_jobs : [];
  const receiptSpend = data?.counterfactual?.actual_cost_usd;
  const receiptReference = data?.counterfactual?.naive_cost_usd;
  const receiptSavings = data?.counterfactual?.avoided_usd;
  const receiptJobs = data?.counterfactual?.jobs;
  const receiptTasks = data?.counterfactual?.tasks;
  const receiptBasis = data?.counterfactual?.spend_basis;
  const alignedJobSavings = data?.counterfactual_source === "job_financial_reports";
  const spendHeading = receiptBasis === "plan"
    ? "Included in your plan"
    : receiptBasis === "estimated" || receiptBasis === "mixed"
    ? "Estimated cost"
    : receiptBasis === "measured_usage_x_registry_price"
      ? "Measured usage cost"
      : "Route forecast";
  const hasReceipt = isFiniteNumber(receiptSpend)
    && isFiniteNumber(receiptReference)
    && isFiniteNumber(receiptSavings);
  const savingsPercent = hasReceipt && receiptReference > 0
    ? Math.max(0, Math.min(100, (receiptSavings / receiptReference) * 100))
    : null;
  const financialIssue = data?.counterfactual_status === "mixed_reference"
    ? "Savings unavailable because these jobs use different comparison models."
    : data?.counterfactual_status === "receipt_mismatch"
      ? "Savings unavailable because the job reports do not agree."
      : data?.counterfactual_status === "incomplete"
        ? "Savings unavailable because one or more job reports are incomplete."
        : "";

  return (
    <div className="w-full pb-4 text-[11px] text-txt">
      {data && data.available === false ? (
        <p className="px-3 pb-3 text-[10px] leading-snug text-muted">
          {data.error || "Economics unavailable."}
        </p>
      ) : null}

      {hasReceipt ? (
        <section className="mx-3 mb-3 rounded-lg border border-edge/60 bg-panel2/25 px-3 py-3">
          <div className="mb-2.5 text-[9px] uppercase tracking-wide text-faint">
            {scope === "all_projects" ? "All projects" : "This repo"}
            {data?.window_days ? " · last 30 days" : " · all time"}
          </div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-3">
            <div className="min-w-0">
              <div className="text-[10px] text-muted">{spendHeading}</div>
              <div className="mt-0.5 text-[20px] font-semibold tracking-tight tabular-nums text-warn/90">{fmtUnknownMoney(receiptSpend)}</div>
            </div>
            <div className="min-w-0">
              <div className="text-[10px] text-muted">Estimated frontier cost</div>
              <div className="mt-0.5 text-[20px] font-semibold tracking-tight tabular-nums">~{fmtUnknownMoney(receiptReference)}</div>
            </div>
            <div className="min-w-0">
              <div className="text-[10px] text-muted">Estimated savings</div>
              <div className="mt-0.5 text-[20px] font-semibold tracking-tight tabular-nums text-good/90">~{fmtUnknownMoney(receiptSavings)}</div>
            </div>
            <div className="min-w-0">
              <div className="text-[10px] text-muted">Less than frontier</div>
              <div className="mt-0.5 text-[20px] font-semibold tracking-tight tabular-nums text-good/90">
                {savingsPercent === null ? "—" : `${savingsPercent.toFixed(1)}%`}
              </div>
            </div>
          </div>
          {isFiniteNumber(receiptJobs) || isFiniteNumber(receiptTasks) ? (
            <div className="mt-3 flex justify-between gap-3 border-t border-edge/50 pt-2 text-[10px] text-faint">
              <span>{isFiniteNumber(receiptJobs) ? `${receiptJobs} jobs considered` : ""}</span>
              <span>{isFiniteNumber(receiptTasks) ? `${receiptTasks} priced tasks` : ""}</span>
            </div>
          ) : null}
          {receiptBasis === "measured_usage_x_registry_price" ? (
            <div className="mt-2 text-[10px] text-faint">Based on measured usage and current model prices.</div>
          ) : receiptBasis === "mixed" ? (
            <div className="mt-2 text-[10px] text-faint">Includes measured and estimated usage.</div>
          ) : null}
        </section>
      ) : financialIssue ? (
        <p className="px-3 pb-3 text-[10px] leading-snug text-warn">{financialIssue}</p>
      ) : scope === "conversation" && data?.available !== false ? (
        <p className="px-3 pb-3 text-[10px] leading-snug text-muted">
          {jobs.length ? "A full comparison is not available for this conversation yet." : "No owned jobs for this conversation."}
        </p>
      ) : null}


      <section className="border-t border-edge/60">
          <div className="flex items-center justify-between px-3 py-2.5 text-[10px] font-semibold uppercase tracking-wide text-txt">
            <span>Recent jobs</span>
            {isFiniteNumber(data?.recent_jobs_total) && data.recent_jobs_total > jobs.length ? (
              <span className="font-normal normal-case tracking-normal text-faint">Showing {jobs.length} of {data.recent_jobs_total}</span>
            ) : null}
          </div>
          <div className="px-3 pb-3">
          {jobs.length === 0 ? (
            <div className="py-2 text-[10px] text-faint">No recent jobs.</div>
          ) : jobs.map((job) => {
            const owned = Boolean(job.accounting_owned);
            const modelIds = (job.models || []).map((model) => model.model_id || "").filter(Boolean);
            const measuredCost = isFiniteNumber(job.measured_cost_usd) && job.measured_cost_usd > 0
              ? job.measured_cost_usd
              : (!isFiniteNumber(job.estimated_cost_usd)
                  && isFiniteNumber(job.actual_marginal_usd)
                  && job.actual_marginal_usd > 0
                ? job.actual_marginal_usd
                : null);
            const estimatedCost = isFiniteNumber(job.estimated_cost_usd) && job.estimated_cost_usd > 0
              ? job.estimated_cost_usd
              : null;
            const noBillableWorker = owned
              && job.status === "failed"
              && modelIds.length === 0
              && jobHeadlineTotal(job) === 0;
            const includedInPlan = owned && job.cost_basis === "plan";
            const jobSavings = alignedJobSavings
              && job.counterfactual?.reference_priced === true
              && job.counterfactual.reference_model_id === referenceId
              && isFiniteNumber(job.counterfactual.avoided_usd)
              && job.counterfactual.avoided_usd > 0
                ? job.counterfactual.avoided_usd
                : null;
            const spendKind = measuredCost !== null
              ? "Measured usage"
              : estimatedCost !== null
                ? "Estimated usage"
                : null;
            const spendAmount = measuredCost ?? estimatedCost;

            return (
              <div key={job.job_id || `${job.source}-${job.status}`} className="border-t border-edge/40 py-2 first:border-t-0">
                <div className="flex items-center justify-between gap-2 font-mono text-[10px]">
                  {job.job_id ? (
                    <button
                      type="button"
                      className="min-w-0 truncate text-left text-blue-400/90 hover:text-blue-300 hover:underline underline-offset-2"
                      onClick={() => openAgentSwarmJob(job.job_id || "")}
                    >
                      {job.job_id}
                    </button>
                  ) : <span>Job</span>}
                  <span className="shrink-0 text-faint">{job.status || "unknown"}</span>
                </div>
                {modelIds.length > 0 ? (
                  <div className="mt-1 truncate font-mono text-[10px] text-faint" title={modelIds.join(", ")}>{modelIds.join(", ")}</div>
                ) : null}
                <div className="mt-1 flex items-center justify-between gap-3 text-[10px] text-muted">
                  <span>
                    {!owned ? "Visible only" : includedInPlan ? "Included in your plan" : noBillableWorker ? "No billable worker ran" : spendKind ? (
                      <>
                        {spendKind}{" "}
                        <span className="font-medium tabular-nums text-warn/90">{fmtUnknownMoney(spendAmount)}</span>
                      </>
                    ) : "Cost unavailable"}
                  </span>
                  {jobSavings !== null ? (
                    <span className="shrink-0 tabular-nums text-good/90">Estimated savings ~{fmtUnknownMoney(jobSavings)}</span>
                  ) : null}
                </div>
              </div>
            );
          })}
          </div>
        </section>

      {referenceId ? (
        <div className="mx-3 mt-3 rounded bg-panel2/30 px-2.5 py-2 text-[10px] leading-snug text-muted">
          Compared with <strong className="font-mono font-medium text-txt">{referenceId}</strong>. Savings are estimates, not cash back.
        </div>
      ) : null}
    </div>
  );
}
