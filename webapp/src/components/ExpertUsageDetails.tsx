import { useState } from 'react';
import type { ExpertHeader } from '../lib/expertMetadata';
import { expertAge, expertDollars, expertTokens, expertWorkerCost } from '../lib/expertEconomicsFacts';
import type { ExpertEconomicsHeader, ExpertUsageFacts } from '../lib/expertEconomicsFacts';

export type ExpertCostProps = { header: ExpertHeader | ExpertEconomicsHeader | null; now?: number; compact?: boolean };
export type ExpertWorkerUsageProps = { usage: ExpertUsageFacts; planBilled?: boolean; compact?: boolean };
const pill = 'min-w-0 rounded border border-edge px-2 py-1 text-xs text-muted break-words [overflow-wrap:anywhere]';
const usageLink = 'text-ui-10 text-faint hover:text-muted focus-visible:outline focus-visible:outline-accent';

export function ExpertWorkerUsage({ usage, planBilled = false, compact = false }: ExpertWorkerUsageProps) {
  const [open, setOpen] = useState(false);
  const tokens = expertTokens(usage), cost = expertWorkerCost(usage, planBilled || usage.plan_billed === true);
  const forecast = usage.route_forecast_usd;
  if (tokens === null && cost === null && forecast == null) return null;
  return <div className="min-w-0">
    <button type="button" className={compact ? usageLink : pill} aria-label="Show tokens and cost" aria-expanded={open}
      onClick={event => { event.stopPropagation(); setOpen(!open); }}>Usage{compact ? ' >' : ''}</button>
    {open && <div className="text-xs break-words [overflow-wrap:anywhere]">
      {tokens !== null && <p>{tokens.toLocaleString('en-US')}t</p>}
      <p>Input {usage.tokens_in?.toLocaleString('en-US') ?? 'unknown'} · Output {usage.tokens_out?.toLocaleString('en-US') ?? 'unknown'}</p>
      {cost && <p>{cost}</p>}
      {forecast != null && <p>Route forecast ~${forecast.toFixed(4)} (not spend)</p>}
      {usage.cost_provenance && <p>Source: {usage.cost_provenance}</p>}
    </div>}
  </div>;
}

export function ExpertCost({ header, now = Date.now(), compact = false }: ExpertCostProps) {
  const [costOpen, setCostOpen] = useState(false), [savingsOpen, setSavingsOpen] = useState(false);
  if (!header) return null;
  const cost = header.cost, live = header.usage ? header : null;
  const savings = live?.savings, age = expertAge(header.created_at, now);
  const partialCost = live !== null && !live.cost.complete;
  const meter = compact ? usageLink : pill;
  return <div className={`min-w-0 flex flex-wrap items-start gap-1 ${compact ? 'text-ui-10 text-muted' : 'text-xs'}`}>
    {!compact && live && <span>{live.completed_workers}/{live.selected_workers} workers completed{live.workers_complete ? '' : ' (selected)'}</span>}
    {!compact && age && <time dateTime={header.created_at ?? undefined} title={`Created ${header.created_at}`}>{age}</time>}
    {live?.usage?.tokens !== null && live?.usage?.tokens !== undefined && <span>{live.usage?.tokens.toLocaleString('en-US')}t{!live.usage?.complete || live.usage?.tokens_known_workers !== live.usage?.selected_workers ? ' (partial)' : ''}</span>}
    {!compact && savings?.compact_tokens !== null && savings?.compact_tokens !== undefined && <span>{savings.compact_tokens.toLocaleString('en-US')} compact</span>}
    {cost.selected_usd !== null && <div className="min-w-0">
      <button type="button" className={meter} aria-label="Job cost" aria-expanded={costOpen}
        onClick={event => { event.stopPropagation(); setCostOpen(!costOpen); }}>
        {cost.basis === 'plan' ? 'Plan-billed' : expertDollars(cost.selected_usd)}{partialCost ? ' (partial)' : ''}{compact ? ' >' : ''}
      </button>
      {costOpen && <div>
        {cost.measured_cost_usd !== null && <p><span>Measured</span> <span>{expertDollars(cost.measured_cost_usd)}</span></p>}
        {cost.estimated_cost_usd !== null && <p><span>Estimated</span> <span>{expertDollars(cost.estimated_cost_usd)}</span></p>}
        {live && (live.cost.plan_workers ?? 0) > 0 && <p>{live.cost.plan_workers} plan-billed workers</p>}
        <p>Source: {cost.source === 'terminal_cost_receipt' ? 'terminal cost receipt' : 'selected current records'}</p>
        {live && <p>Cost coverage: {live.usage?.cost_known_workers}/{live.usage?.selected_workers} selected workers</p>}
        {live?.updated_at && <p>Job updated <time dateTime={live.updated_at}>{live.updated_at}</time></p>}
        {live?.latest_task_updated_at && <p>Latest selected task update <time dateTime={live.latest_task_updated_at}>{live.latest_task_updated_at}</time></p>}
        {header.completed_at && <p>Completed <time dateTime={header.completed_at}>{header.completed_at}</time></p>}
      </div>}
    </div>}
    {savings?.selected_usd !== null && savings?.selected_usd !== undefined && savings.selected_usd > 0 && <div className="min-w-0">
      <button type="button" className={meter} aria-expanded={savingsOpen} onClick={event => { event.stopPropagation(); setSavingsOpen(!savingsOpen); }}>
        Estimated savings ~${savings.selected_usd.toFixed(4)}{compact ? ' >' : ''}
      </button>
      {savingsOpen && <div><p>Selected list-price value; not billed savings. Missing components are excluded.</p>
        {savings.routing_usd !== null && <p title="model selection value vs frontier-equivalent list price">Routing ~${savings.routing_usd.toFixed(4)}</p>}
        {savings.cache_usd !== null && <p title="prompt-cache value">Cache ~${savings.cache_usd.toFixed(4)}</p>}
        {savings.compaction_usd !== null && <p title="tool-output compaction">Compaction ~${savings.compaction_usd.toFixed(4)}</p>}
      </div>}
    </div>}
  </div>;
}
