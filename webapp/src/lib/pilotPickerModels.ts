/** Pure helpers for PilotPicker filter + provider grouping. */

export type PilotProviderGroup = { provider: string; items: string[] };

/** Cold/offline Ox fallback. Prefer ``config.model_labels`` when present. */
export const KNOWN_MODEL_LABELS: Record<string, string> = {
  "ox-alpha-free": "Ox Alpha Free",
  "x-preview-f-free": "Ox Alpha Free",
};

/** Provider prefix before ':' (or the whole id when unscoped). */
export function providerOf(spec: string): string {
  if (!spec) return "other";
  const idx = spec.indexOf(":");
  if (idx <= 0) return spec || "other";
  return spec.slice(0, idx);
}

/** Match model id, provider prefix, and/or friendly display name. */
export function filterPilotModels(
  models: string[],
  query: string,
  labels?: Record<string, string>,
): string[] {
  const q = query.trim().toLowerCase();
  if (!q) return models.slice();
  return models.filter((m) => {
    const lower = m.toLowerCase();
    const provider = providerOf(m).toLowerCase();
    const label = modelLabelOf(m, labels).toLowerCase();
    return lower.includes(q) || provider.includes(q) || label.includes(q);
  });
}

/** Bare model id from a `provider:model` spec (or the whole string). */
export function modelIdOf(spec: string): string {
  if (!spec) return "";
  const idx = spec.indexOf(":");
  return idx <= 0 ? spec : spec.slice(idx + 1);
}

/** Friendly picker/Settings label; wire spec is unchanged. */
export function modelLabelOf(spec: string, labels?: Record<string, string>): string {
  const id = modelIdOf(spec);
  const fromMap = labels?.[spec] || labels?.[id] || KNOWN_MODEL_LABELS[id];
  return (fromMap || id || spec).trim();
}

/** Pin the current driver at the front when present; leave others in order. */
export function pinCurrentPilot(models: string[], current: string): string[] {
  if (!current) return models.slice();
  const rest = models.filter((m) => m !== current);
  if (models.includes(current)) return [current, ...rest];
  return rest;
}

/**
 * Keep the current spec when it is still in the picker; otherwise the first
 * live model. A disconnected OpenRouter DeepSeek must not stay selected when
 * the list is only Z.AI.
 */
export function fallbackPilot(models: string[], current: string): string {
  if (!models.length) return current || "";
  if (!current) return models[0];
  if (models.includes(current)) return current;
  const curModel = modelIdOf(current);
  const alias = models.find((m) => m === curModel || modelIdOf(m) === curModel);
  return alias || models[0];
}

/** Group specs by provider prefix, preserving first-seen provider order. */
export function groupPilotModelsByProvider(models: string[]): PilotProviderGroup[] {
  const out: PilotProviderGroup[] = [];
  const byProvider = new Map<string, PilotProviderGroup>();
  for (const m of models) {
    const provider = providerOf(m) || "other";
    let g = byProvider.get(provider);
    if (!g) {
      g = { provider, items: [] };
      byProvider.set(provider, g);
      out.push(g);
    }
    g.items.push(m);
  }
  return out;
}

/**
 * Filter → pin current → group remainder by provider.
 * Current (when present after filter) is returned separately so the UI can
 * render it above provider sections.
 */
export function organizePilotModels(
  models: string[],
  current: string,
  query: string,
  labels?: Record<string, string>,
): { current: string | null; groups: PilotProviderGroup[] } {
  const filtered = filterPilotModels(models, query, labels);
  const pinned = pinCurrentPilot(filtered, current);
  const currentPinned =
    current && pinned[0] === current ? current : null;
  const remainder = currentPinned ? pinned.slice(1) : pinned;
  return { current: currentPinned, groups: groupPilotModelsByProvider(remainder) };
}
