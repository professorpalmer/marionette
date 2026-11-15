/**
 * Per-model facts for the pilot picker rows: price, context window, image
 * input, and routing tier. Every fact comes from /api/models/catalog; a fact
 * the catalog does not carry stays unknown and the row leaves it out.
 */
import { api } from "./api";

/** What a call costs on this account. A plan (subscription) has no per-token price. */
export type ModelPrice =
  | { kind: "plan" }
  | { kind: "rate"; /** USD per million input tokens. */ input: number; /** USD per million output tokens. */ output: number }
  | { kind: "unknown" };

export type ModelFacts = {
  price: ModelPrice;
  contextWindow: number | null;
  /** true: takes images; false: text only; null: no source knows. */
  imageInput: boolean | null;
  /** Routing tier named by the model id itself (OpenRouter variant suffix, local). */
  tier: string | null;
};

export type ModelFactsState =
  | { kind: "loading" }
  | { kind: "ready"; facts: ReadonlyMap<string, ModelFacts> }
  | { kind: "unavailable" };

/** OpenRouter variant suffixes (`model:free`) and the tier each names. */
const OPENROUTER_VARIANT_TIER: Record<string, string> = {
  free: "Free tier",
  nitro: "Fast routing",
  floor: "Lowest-price routing",
  batch: "Batch",
  thinking: "Thinking",
  online: "Web search",
  exacto: "Exacto routing",
  extended: "Extended context",
};

const finiteOrNull = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;

export function tierForSpec(spec: string): string | null {
  const colon = spec.indexOf(":");
  const provider = colon >= 0 ? spec.slice(0, colon) : "";
  const model = colon >= 0 ? spec.slice(colon + 1) : spec;
  if (provider === "local") return "Local";
  if (provider !== "openrouter") return null;
  const variant = model.includes(":") ? model.slice(model.lastIndexOf(":") + 1) : "";
  return OPENROUTER_VARIANT_TIER[variant] ?? null;
}

/** The row's billing (the provider's, from the catalog) wins over any listed price. */
function priceOf(r: Record<string, unknown>): ModelPrice {
  if (r.billing === "plan") return { kind: "plan" };
  const pricing = r.pricing && typeof r.pricing === "object" ? (r.pricing as Record<string, unknown>) : {};
  const input = finiteOrNull(r.price_in ?? pricing.prompt);
  const output = finiteOrNull(r.price_out ?? pricing.completion);
  return input !== null && output !== null ? { kind: "rate", input, output } : { kind: "unknown" };
}

/** Parse the catalog payload at the boundary; rows without a spec are dropped. */
export function parseModelFacts(raw: unknown): Map<string, ModelFacts> {
  const out = new Map<string, ModelFacts>();
  if (!raw || typeof raw !== "object") return out;
  const rows = (raw as { all?: unknown }).all;
  if (!Array.isArray(rows)) return out;
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const r = row as Record<string, unknown>;
    if (typeof r.spec !== "string" || !r.spec) continue;
    out.set(r.spec, {
      price: priceOf(r),
      contextWindow: finiteOrNull(r.context_window),
      imageInput: typeof r.image_input === "boolean" ? r.image_input : null,
      tier: tierForSpec(r.spec),
    });
  }
  return out;
}

const CACHE_MS = 5 * 60 * 1000;
let cached: { at: number; request: Promise<Map<string, ModelFacts>> } | null = null;

/** One shared catalog read per five minutes for every picker instance. */
export function loadModelFacts(now: number = Date.now()): Promise<Map<string, ModelFacts>> {
  if (cached && now - cached.at < CACHE_MS) return cached.request;
  const request = Promise.resolve()
    .then(() => api.modelCatalog())
    .then(parseModelFacts);
  cached = { at: now, request };
  request.catch(() => {
    if (cached?.request === request) cached = null;
  });
  return request;
}

export function resetModelFactsCacheForTests(): void {
  cached = null;
}

/** "$0", "$0.038", "$0.30", "$12.5", "$150". Catalog floats such as
 * 0.09999999 or 0.435 round to the cent a person reads ($0.10, $0.44). */
export function formatPrice(usd: number): string {
  if (usd === 0) return "$0";
  if (usd < 0.095) return `$${Number(usd.toPrecision(2))}`;
  if (usd < 10) return `$${(Math.round(usd * 100 + 1e-7) / 100).toFixed(2)}`;
  if (usd < 100) return `$${Number(usd.toFixed(1))}`;
  return `$${Math.round(usd)}`;
}

/** "1M", "1.05M", "262k", "8k". */
export function formatContextWindow(tokens: number): string {
  if (tokens >= 1_000_000) return `${Number((tokens / 1_000_000).toFixed(2))}M`;
  if (tokens >= 1000) return `${Math.round(tokens / 1000)}k`;
  return String(Math.round(tokens));
}

function priceText(price: ModelPrice): string | null {
  switch (price.kind) {
    case "plan":
      return "Plan";
    case "rate":
      return `${formatPrice(price.input)} / ${formatPrice(price.output)} per MTok`;
    case "unknown":
      return null;
    default: {
      const _exhaustive: never = price;
      return _exhaustive;
    }
  }
}

/** The row's known facts, in display order. An unknown fact is left out. */
export function modelFactParts(facts: ModelFacts): string[] {
  const parts = [
    priceText(facts.price),
    facts.contextWindow !== null ? `${formatContextWindow(facts.contextWindow)} context` : null,
    facts.imageInput === null ? null : facts.imageInput ? "Vision" : "Text only",
    facts.tier,
  ];
  return parts.filter((part): part is string => part !== null);
}
