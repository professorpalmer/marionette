import { describe, expect, it } from "vitest";
import type { LedgerBucket, LedgerView } from "../lib/api";
import { hasLedger, ledgerFooter } from "../lib/ledgerDisplay";

const empty: LedgerBucket = {
  calls: 0, tokens: 0, input_uncached: 0, cache_read: 0, cache_write: 0, output: 0,
  cache_hit: null, cash_usd: 0, list_usd: 0, list_unpriced_calls: 0, unpriced_calls: 0, reported_calls: 0,
};

function view(over: Partial<LedgerView>): LedgerView {
  return {
    session_id: "s", calls: 1, spent_usd: 0, spent_exact: false, spent_confidence: "none",
    unpriced_calls: 0, tokens: 0, cache_hit: null, plan: empty, local: empty,
    by_route: [], by_purpose: {}, by_job: {}, since: 1, ...over,
  };
}

describe("ledger footer", () => {
  it("shows a subscription session as $0 spend with its list-price value, never as spend", () => {
    const f = ledgerFooter(view({
      tokens: 42_400_000, cache_hit: 0.95,
      plan: { ...empty, calls: 300, tokens: 42_400_000, list_usd: 1.33 },
    }));
    expect(f.spend).toBe("$0.00");
    expect(f.plan).toBe("42.4M tok on plan · $1.33 at list");
    expect(f.cache).toBe("95% cache");
    expect(f.unpriced).toBeNull();
  });

  it("marks spend exact only when every call carried a provider-reported cost", () => {
    expect(ledgerFooter(view({ spent_usd: 1.42, spent_exact: true, spent_confidence: "reported" })).spend).toBe("$1.42");
    expect(ledgerFooter(view({ spent_usd: 1.42, spent_confidence: "mixed" })).spend).toBe("~$1.42");
  });

  it("surfaces unpriced calls instead of counting them as $0", () => {
    const f = ledgerFooter(view({ spent_usd: 0.2, spent_confidence: "computed", unpriced_calls: 2 }));
    expect(f.unpriced).toBe("+ 2 unpriced calls");
  });

  it("flags a partial list price when some plan calls had no published rate", () => {
    const f = ledgerFooter(view({ plan: { ...empty, calls: 3, tokens: 9_000, list_usd: 0.5, list_unpriced_calls: 1 } }));
    expect(f.plan).toBe("9k tok on plan · $0.50 at list (partial)");
  });

  it("shows an all-local session's tokens once and no cache rate", () => {
    const f = ledgerFooter(view({
      tokens: 960, cache_hit: 0,
      local: { ...empty, calls: 2, tokens: 960, cache_hit: 0 },
    }));
    expect(f.tokens).toBeNull();
    expect(f.local).toBe("960 tok local");
    expect(f.cache).toBeNull();
  });

  it("shows an all-plan session's tokens once", () => {
    const f = ledgerFooter(view({ tokens: 9_000, plan: { ...empty, calls: 3, tokens: 9_000 } }));
    expect(f.tokens).toBeNull();
    expect(f.plan).toBe("9k tok on plan");
  });

  it("keeps the total and cache rate for a mixed session", () => {
    const f = ledgerFooter(view({
      tokens: 10_000, cache_hit: 0.5,
      local: { ...empty, calls: 1, tokens: 960 },
    }));
    expect(f.tokens).toBe("10k tok");
    expect(f.local).toBe("960 tok local");
    expect(f.cache).toBe("50% cache");
  });

  it("only sessions with recorded calls are shown from the ledger", () => {
    expect(hasLedger(view({ calls: 0 }))).toBe(false);
    expect(hasLedger(undefined)).toBe(false);
    expect(hasLedger(view({ calls: 4 }))).toBe(true);
  });
});
