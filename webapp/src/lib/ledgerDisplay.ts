import type { LedgerBucket, LedgerView } from "./api";

/**
 * Words for the session's money, derived only from the usage ledger.
 *
 * - Spend is cash that left the wallet. It is exact ("$1.42") when every
 *   contributing call carried the provider's reported cost, "~$1.42" when any
 *   was priced from the provider's published rate card.
 * - Plan usage is never spend: it shows its tokens and what it would cost at
 *   list price.
 * - Calls with no reported cost and no published rate are counted as
 *   unpriced, never folded in as $0.
 */

export function formatTokenCount(num: number): string {
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (num >= 1_000) return `${(num / 1_000).toFixed(1).replace(/\.0$/, "")}k`;
  return String(num);
}

export function formatUsd(num: number): string {
  if (num === 0) return "$0.00";
  if (num < 0.001) return `$${num.toFixed(4)}`;
  if (num < 0.01) return `$${num.toFixed(3)}`;
  return `$${num.toFixed(2)}`;
}

export type LedgerFooter = {
  /** Session total; null when the plan or local part already is the whole total. */
  tokens: string | null;
  spend: string;
  spendTitle: string;
  unpriced: string | null;
  plan: string | null;
  planTitle: string | null;
  local: string | null;
  cache: string | null;
};

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function listPart(bucket: LedgerBucket): string {
  if (bucket.list_usd <= 0) return "";
  const partial = bucket.list_unpriced_calls > 0 ? " (partial)" : "";
  return ` · ${formatUsd(bucket.list_usd)} at list${partial}`;
}

export function ledgerFooter(view: LedgerView): LedgerFooter {
  const approx = view.spent_confidence === "computed" || view.spent_confidence === "mixed";
  const spend = `${approx ? "~" : ""}${formatUsd(view.spent_usd)}`;
  const spendTitle = view.spent_confidence === "none"
    ? "No pay-as-you-go spend recorded for this session."
    : view.spent_exact
      ? "Cash spent this session, from the providers' reported costs."
      : "Cash spent this session: provider-reported costs where given, otherwise the provider's published rates.";
  const unpriced = view.unpriced_calls > 0 ? `+ ${plural(view.unpriced_calls, "unpriced call")}` : null;
  const plan = view.plan.calls > 0 ? `${formatTokenCount(view.plan.tokens)} tok on plan${listPart(view.plan)}` : null;
  const planTitle = plan
    ? "Included in your subscription: $0 extra cash. List price is what the same usage would cost pay-as-you-go."
    : null;
  const local = view.local.calls > 0 ? `${formatTokenCount(view.local.tokens)} tok local` : null;
  const wholeTotalShown = (plan !== null && view.plan.tokens === view.tokens)
    || (local !== null && view.local.tokens === view.tokens);
  const tokens = wholeTotalShown ? null : `${formatTokenCount(view.tokens)} tok`;
  // Local models never cache, so only non-local tokens could have hit one.
  const cacheable = view.tokens - view.local.tokens;
  const cache = view.cache_hit != null && cacheable > 0 ? `${Math.round(view.cache_hit * 100)}% cache` : null;
  return { tokens, spend, spendTitle, unpriced, plan, planTitle, local, cache };
}

/** A session with ledger rows is shown from the ledger; older sessions are not. */
export function hasLedger(view: LedgerView | undefined | null): view is LedgerView {
  return Boolean(view && view.calls > 0);
}
