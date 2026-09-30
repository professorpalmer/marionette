import type { LedgerBucket, LedgerView } from "../lib/api";
import { formatTokenCount, formatUsd, ledgerFooter } from "../lib/ledgerDisplay";

const PURPOSE_LABELS: Record<string, string> = {
  pilot: "Pilot",
  swarm: "Swarm workers",
  compaction: "Compaction",
  vision: "Image reading",
  cache_keep_warm: "Cache keep-warm",
  advisor: "Advisor",
  inline_edit: "Inline edits",
  wiki_synthesis: "Wiki answers",
  wiki_ingest: "Wiki ingest",
  skill_distill: "Skill distilling",
  doctor: "Doctor",
  repair: "Repair",
  unattributed: "Other",
};

const BILLING_LABELS = { metered: "pay-as-you-go", plan: "plan", local: "local" } as const;

function cachePercent(b: LedgerBucket): string {
  return b.cache_hit == null ? "" : `${Math.round(b.cache_hit * 100)}%`;
}

function money(b: LedgerBucket, billing?: string): string {
  if (billing === "plan") return b.list_usd > 0 ? `$0 · ${formatUsd(b.list_usd)} list` : "$0";
  if (billing === "local") return "$0";
  const known = formatUsd(b.cash_usd);
  return b.unpriced_calls > 0 ? `${known} + ${b.unpriced_calls} unpriced` : known;
}

function Stat({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className="min-w-0" title={title}>
      <div className="text-[10px] text-muted">{label}</div>
      <div className="mt-0.5 text-[15px] font-medium tabular-nums text-txt">{value}</div>
    </div>
  );
}

/** The session's accounting, from the usage ledger only. */
export default function LedgerBreakdown({ view }: { view: LedgerView }) {
  const words = ledgerFooter(view);
  const purposes = Object.entries(view.by_purpose);
  const jobs = Object.entries(view.by_job);
  const openrouter = view.reconcile?.openrouter;
  return (
    <div className="w-full min-h-0 px-3 py-3 text-[11px] text-txt" data-testid="ledger-breakdown">
      <p className="text-[10px] text-muted mb-2 leading-snug">
        Every model call this session, recorded once and priced when it was made.
      </p>
      <div className="mb-3 rounded-md border border-edge/50 bg-panel2/20 px-2.5 py-2.5">
        <div className="grid grid-cols-2 gap-x-3 gap-y-2.5">
          <Stat label="Spent" value={words.spend} title={words.spendTitle} />
          <Stat
            label="On plans (list price)"
            value={view.plan.calls ? formatUsd(view.plan.list_usd) : "—"}
            title={words.planTitle ?? "No subscription usage this session."}
          />
          <Stat label="Tokens" value={formatTokenCount(view.tokens)} />
          <Stat label="Prompt cache" value={view.cache_hit == null ? "—" : `${Math.round(view.cache_hit * 100)}%`} />
        </div>
        {words.unpriced ? (
          <p className="mt-2 text-[10px] text-warn/80 leading-snug">
            {words.unpriced}: no reported cost and no published rate for that model, so they are not in the total.
          </p>
        ) : null}
      </div>

      <div className="text-[10px] text-faint mb-1">By model</div>
      <table className="w-full mb-3 tabular-nums">
        <tbody>
          {view.by_route.map((row) => (
            <tr key={`${row.billing}:${row.provider}:${row.model}`} className="align-top">
              <td className="py-0.5 pr-2 min-w-0">
                <div className="truncate text-txt/90" title={`${row.provider} · ${row.model}`}>{row.model}</div>
                <div className="text-[10px] text-faint">
                  {row.provider} · {BILLING_LABELS[row.billing]} · {row.calls} calls
                  {row.cache_hit != null ? ` · ${cachePercent(row)} cache` : ""}
                </div>
              </td>
              <td className="py-0.5 text-right whitespace-nowrap">{money(row, row.billing)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {purposes.length > 1 ? (
        <>
          <div className="text-[10px] text-faint mb-1">By purpose</div>
          <div className="mb-3">
            {purposes.map(([purpose, b]) => (
              <div key={purpose} className="flex justify-between mb-0.5">
                <span className="text-muted">{PURPOSE_LABELS[purpose] ?? purpose} · {b.calls} calls</span>
                <span className="tabular-nums">{money(b)}</span>
              </div>
            ))}
          </div>
        </>
      ) : null}

      {jobs.length ? (
        <>
          <div className="text-[10px] text-faint mb-1">Swarm jobs</div>
          <div className="mb-3">
            {jobs.map(([jobId, b]) => (
              <div key={jobId} className="flex justify-between mb-0.5">
                <span className="text-muted truncate" title={jobId}>{jobId.slice(0, 12)} · {b.calls} workers</span>
                <span className="tabular-nums">{money(b)}</span>
              </div>
            ))}
          </div>
        </>
      ) : null}

      {openrouter ? (
        <p className="text-[10px] text-muted leading-snug" data-testid="ledger-reconcile">
          OpenRouter today: {formatUsd(openrouter.day.ledger_usd)} recorded here,{" "}
          {formatUsd(openrouter.day.account_usd)} on the key
          {openrouter.day.unrecorded_usd > 0.005
            ? ` (${formatUsd(openrouter.day.unrecorded_usd)} from other apps or unrecorded calls)`
            : ""}
          .
        </p>
      ) : null}
    </div>
  );
}
