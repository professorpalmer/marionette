import type { EconomicsData, EconomicsJobRow, EconomicsScope } from "../lib/api";

const SCOPES: Array<{ value: EconomicsScope; label: string }> = [
  { value: "repo", label: "This repo" },
  { value: "window30", label: "Last 30 days" },
  { value: "all_projects", label: "All projects" },
  { value: "conversation", label: "This conversation" },
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

function fmtTokens(value: number | null | undefined): string {
  if (!isFiniteNumber(value)) return "—";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (value >= 1000) return `${(value / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return String(value);
}

function fmtRate(value: number | null | undefined): string {
  if (!isFiniteNumber(value)) return "—";
  return `${Math.round(value * 100)}%`;
}

function shortModel(model: string | undefined): string {
  const text = (model || "").trim();
  if (!text) return "unknown";
  const afterColon = text.includes(":") ? text.slice(text.lastIndexOf(":") + 1) : text;
  const slash = afterColon.lastIndexOf("/");
  return slash >= 0 ? afterColon.slice(slash + 1) : afterColon;
}

function jobModel(row: EconomicsJobRow): string {
  const id = row.models?.[0]?.model_id;
  return shortModel(id);
}

function costBasisLabel(basis: string | null | undefined): string {
  const value = (basis || "").trim().toLowerCase();
  if (value === "plan") return " · plan $0-marginal";
  if (value === "estimated") return " · estimated";
  if (value === "mixed") return " · mixed basis";
  if (value === "unknown") return " · unknown basis";
  if (value.includes("measured")) return " · measured";
  return "";
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
}: {
  data: EconomicsData | null;
  scope: EconomicsScope;
  onScopeChange: (scope: EconomicsScope) => void;
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
          ? "Owned jobs for this conversation. Puppetmaster savings stay on This repo / Last 30 days / All projects."
          : "Puppetmaster savings for the selected scope. Recent jobs are this workspace tracker; Last 30 days keeps jobs created in that window. App-run spend stays above."}
      </p>

      <label className="flex items-center justify-between mb-2 text-faint">
        <span className="text-[10px] uppercase tracking-wide">Scope</span>
        <select
          className="bg-transparent text-[11px] text-txt"
          value={scope}
          onChange={(event) => onScopeChange(event.target.value as EconomicsScope)}
          aria-label="Economics scope"
        >
          {SCOPES.map((option) => (
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
          <span className="tabular-nums">{fmtUnknownMoney(routingSaved)}</span>
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
          <span className="tabular-nums">{fmtUnknownMoney(codegraphEst)}</span>
        </div>
      ) : null}

      {isFiniteNumber(avoided) ? (
        <div className="flex items-center justify-between mb-1 text-faint">
          <span>
            {referenceId
              ? `Estimated savings vs ${referenceId}`
              : "Estimated savings"}
          </span>
          <span className="tabular-nums">{fmtUnknownMoney(avoided)}</span>
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
            const headlineTotal = owned ? jobHeadlineTotal(job) : null;
            const jobAvoided = owned ? job.counterfactual?.avoided_usd : null;
            return (
              <div key={job.job_id || `${job.source}-${job.status}`} className="mb-2">
                <div className="flex items-center justify-between text-faint">
                  <span className="truncate pr-2">{job.job_id ? `Job ${job.job_id}` : "Job"}</span>
                  <span className="tabular-nums shrink-0">
                    {owned
                      ? `${fmtUnknownMoney(headlineTotal)} vs ${fmtUnknownMoney(jobAvoided)}${costBasisLabel(job.cost_basis)}`
                      : "—"}
                  </span>
                </div>
                {owned ? (
                  <>
                    <div className="flex items-center justify-between text-faint pl-2">
                      <span>Measured</span>
                      <span className="tabular-nums shrink-0">{fmtUnknownMoney(job.measured_cost_usd)}</span>
                    </div>
                    <div className="flex items-center justify-between text-faint pl-2">
                      <span>Estimated</span>
                      <span className="tabular-nums shrink-0">{fmtUnknownMoney(job.estimated_cost_usd)}</span>
                    </div>
                  </>
                ) : null}
                <div className="flex items-center justify-between text-faint">
                  <span className="truncate pr-2">
                    {jobModel(job)}
                    {!owned ? " · visible only" : ""}
                  </span>
                  <span className="tabular-nums shrink-0">
                    {fmtTokens(job.tokens)} · {isFiniteNumber(job.typed_artifacts) ? job.typed_artifacts : "—"} typed
                    {isFiniteNumber(job.tokens_per_typed_artifact) ? ` · ${fmtTokens(job.tokens_per_typed_artifact)}/typed` : ""}
                    {` · ${fmtRate(job.degraded_rate)} degraded`}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
