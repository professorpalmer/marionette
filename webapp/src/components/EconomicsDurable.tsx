import type { EconomicsData, EconomicsJobRow } from "../lib/api";
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

function hasFiniteReceiptTotals(data: EconomicsData | null): boolean {
  return isFiniteNumber(data?.counterfactual?.actual_cost_usd)
    && isFiniteNumber(data?.counterfactual?.naive_cost_usd)
    && isFiniteNumber(data?.counterfactual?.avoided_usd);
}

/** True when EconomicsDurable will render the scoped 4-stat receipt hero. */
export function durableReceiptHeroAvailable(data: EconomicsData | null): boolean {
  if (!data || data.counterfactual_source === "routing_report") return false;
  return hasFiniteReceiptTotals(data);
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
}: {
  data: EconomicsData | null;
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
  const receiptMeasured = data?.counterfactual?.measured_cost_usd;
  const receiptEstimated = data?.counterfactual?.estimated_cost_usd;
  const receiptBasis = data?.counterfactual?.spend_basis;
  const alignedJobSavings = data?.counterfactual_source === "job_financial_reports";
  const isRoutingForecast = data?.counterfactual_source === "routing_report";
  const spendHeading = receiptBasis === "plan"
    ? "Included in your plan"
    : receiptBasis === "mixed"
      ? "Usage cost"
    : receiptBasis === "estimated"
    ? "Estimated cost"
    : receiptBasis === "measured_usage_x_registry_price"
      ? "Measured usage cost"
      : "Cost unavailable";
  const hasReceipt = hasFiniteReceiptTotals(data);
  const savingsPercent = hasReceipt && receiptReference > 0
    ? Math.max(0, Math.min(100, (receiptSavings / receiptReference) * 100))
    : null;
  const financialIssue = data?.counterfactual_status === "mixed_reference"
    ? "Savings unavailable because these jobs use different comparison models."
    : data?.counterfactual_status === "receipt_mismatch"
      ? "Savings unavailable because the job reports do not agree."
      : data?.counterfactual_status === "incomplete"
        ? (
          hasReceipt
            ? "Complete reports were used; incomplete reports were excluded."
            : "Savings unavailable because one or more job reports are incomplete."
        )
        : "";

  return (
    <div className="w-full pb-4 text-[11px] text-txt">
      {data && data.available === false ? (
        <p className="px-3 pb-3 text-[10px] leading-snug text-muted">
          {data.error || "Economics unavailable."}
        </p>
      ) : null}

      {durableReceiptHeroAvailable(data) ? (
        <section className="mx-3 mb-3 rounded-md border border-edge/50 bg-panel2/20 px-3 py-2.5">
          <div className="grid grid-cols-2 gap-x-4 gap-y-2.5">
            <div className="min-w-0">
              <div className="text-[10px] text-muted">{spendHeading}</div>
              <div className="mt-0.5 text-[15px] font-medium tabular-nums text-txt">{receiptBasis === "estimated" || receiptBasis === "mixed" ? "~" : ""}{fmtUnknownMoney(receiptSpend)}</div>
            </div>
            <div className="min-w-0">
              <div className="text-[10px] text-muted">Estimated frontier cost</div>
              <div className="mt-0.5 text-[15px] font-medium tabular-nums text-txt">~{fmtUnknownMoney(receiptReference)}</div>
            </div>
            <div className="min-w-0">
              <div className="text-[10px] text-muted">Estimated savings</div>
              <div className="mt-0.5 text-[15px] font-medium tabular-nums text-good/65">~{fmtUnknownMoney(receiptSavings)}</div>
            </div>
            <div className="min-w-0">
              <div className="text-[10px] text-muted">Less than frontier</div>
              <div className="mt-0.5 text-[15px] font-medium tabular-nums text-good/65">
                {savingsPercent === null ? "—" : `${savingsPercent.toFixed(1)}%`}
              </div>
            </div>
          </div>
          {receiptBasis === "mixed" ? (
            <div className="mt-2.5 flex gap-4 border-t border-edge/40 pt-2 text-[10px] text-muted">
              <span>
                <span>Measured usage</span>{" "}
                <span className="tabular-nums text-txt">{fmtUnknownMoney(receiptMeasured)}</span>
              </span>
              <span>
                <span>Estimated usage</span>{" "}
                <span className="tabular-nums text-txt">~{fmtUnknownMoney(receiptEstimated)}</span>
              </span>
            </div>
          ) : null}
          <div className="mt-3 border-t border-edge/50 pt-2 text-[10px] text-faint">
            {isFiniteNumber(receiptJobs) || isFiniteNumber(receiptTasks) ? (
              <div className="flex justify-between gap-3">
                <span>{isFiniteNumber(receiptJobs) ? `${receiptJobs} jobs considered` : ""}</span>
                <span>{isFiniteNumber(receiptTasks) ? `${receiptTasks} priced tasks` : ""}</span>
              </div>
            ) : null}
            {referenceId ? (
              <div className="mt-1.5 leading-snug">
                Compared with <strong className="font-mono font-medium text-txt">{referenceId}</strong>. Savings are estimates, not cash back.
              </div>
            ) : null}
          </div>
          {receiptBasis === "measured_usage_x_registry_price" ? (
            <div className="mt-2 text-[10px] text-faint">Based on measured usage and current model prices.</div>
          ) : receiptBasis === "mixed" ? (
            <div className="mt-2 text-[10px] text-faint">Includes measured and estimated usage.</div>
          ) : null}
          {financialIssue ? (
            <p className="mt-2 text-[10px] leading-snug text-warn">{financialIssue}</p>
          ) : null}
        </section>
      ) : isRoutingForecast && hasReceipt ? (
        <section className="mx-3 mb-3 rounded-md border border-edge/50 bg-panel2/20 px-3 py-2.5">
          <div className="text-[10px] text-muted">Cost unavailable</div>
          <div className="mt-1 text-[10px] leading-snug text-faint">No terminal job receipts for this scope.</div>
          <div className="mt-2.5 grid grid-cols-3 gap-3 border-t border-edge/40 pt-2.5">
            <div className="min-w-0">
              <div className="text-[10px] text-muted">Route forecast</div>
              <div className="mt-0.5 font-medium tabular-nums text-txt">~{fmtUnknownMoney(receiptSpend)}</div>
            </div>
            <div className="min-w-0">
              <div className="text-[10px] text-muted">Estimated frontier forecast</div>
              <div className="mt-0.5 font-medium tabular-nums text-txt">~{fmtUnknownMoney(receiptReference)}</div>
            </div>
            <div className="min-w-0">
              <div className="text-[10px] text-muted">Estimated difference</div>
              <div className="mt-0.5 font-medium tabular-nums text-txt">~{fmtUnknownMoney(receiptSavings)}</div>
            </div>
          </div>
          {referenceId ? (
            <div className="mt-2 text-[10px] leading-snug text-faint">
              Compared with <strong className="font-mono font-medium text-txt">{referenceId}</strong>. Forecasts are predictions, not spend.
            </div>
          ) : null}
        </section>
      ) : financialIssue ? (
        <p className="px-3 pb-3 text-[10px] leading-snug text-warn">{financialIssue}</p>
      ) : data?.scope === "conversation" && data.available !== false ? (
        <p className="px-3 pb-3 text-[10px] leading-snug text-muted">
          {jobs.length ? "A full comparison is not available for this session yet." : "No owned jobs for this session."}
        </p>
      ) : null}


      <section className="border-t border-edge/60">
          <div className="flex items-center justify-between px-3 py-2 text-[10px] font-medium text-muted">
            <span>Job receipts</span>
            {isFiniteNumber(data?.recent_jobs_total) && data.recent_jobs_total > jobs.length ? (
              <span className="font-normal normal-case tracking-normal text-faint">Showing {jobs.length} of {data.recent_jobs_total} jobs in this scope</span>
            ) : null}
          </div>
          <div className="px-3 pb-3">
          {jobs.length === 0 ? (
            <div className="py-2 text-[10px] text-faint">No job receipts in this scope.</div>
          ) : jobs.map((job) => {
            const owned = Boolean(job.accounting_owned);
            const modelIds = (job.models || []).map((model) => model.model_id || "").filter(Boolean);
            const measuredCost = isFiniteNumber(job.measured_cost_usd)
              && (job.measured_cost_usd > 0 || job.cost_basis === "measured")
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
            return (
              <div key={job.job_id || `${job.source}-${job.status}`} className="border-t border-edge/40 py-2 first:border-t-0">
                <div className="flex items-center justify-between gap-2 font-mono text-[10px]">
                  {job.job_id ? (
                    <button
                      type="button"
                      className="min-w-0 truncate text-left text-accent/80 hover:text-accent hover:underline underline-offset-2"
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
                    {!owned ? "Visible only" : includedInPlan ? "Included in your plan" : noBillableWorker ? "No billable worker ran" : measuredCost !== null || estimatedCost !== null ? (
                      <span className="flex flex-wrap gap-x-3 gap-y-1">
                        {measuredCost !== null ? (
                          <span>
                            <span>Measured usage</span>{" "}
                            <span className="tabular-nums text-txt">{fmtUnknownMoney(measuredCost)}</span>
                          </span>
                        ) : null}
                        {estimatedCost !== null ? (
                          <span>
                            <span>Estimated usage</span>{" "}
                            <span className="tabular-nums text-txt">~{fmtUnknownMoney(estimatedCost)}</span>
                          </span>
                        ) : null}
                      </span>
                    ) : "Cost unavailable"}
                  </span>
                  {jobSavings !== null ? (
                    <span className="shrink-0 tabular-nums text-good/65">Estimated savings ~{fmtUnknownMoney(jobSavings)}</span>
                  ) : null}
                </div>
              </div>
            );
          })}
          </div>
        </section>

    </div>
  );
}
