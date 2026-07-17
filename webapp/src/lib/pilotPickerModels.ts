/** Pure helpers for PilotPicker filter + provider grouping. */

export type PilotProviderGroup = { provider: string; items: string[] };

/** Provider prefix before ':' (or the whole id when unscoped). */
export function providerOf(spec: string): string {
  if (!spec) return "other";
  const idx = spec.indexOf(":");
  if (idx <= 0) return spec || "other";
  return spec.slice(0, idx);
}

/** Match model id and/or provider prefix (case-insensitive substring). */
export function filterPilotModels(models: string[], query: string): string[] {
  const q = query.trim().toLowerCase();
  if (!q) return models.slice();
  return models.filter((m) => {
    const lower = m.toLowerCase();
    const provider = providerOf(m).toLowerCase();
    return lower.includes(q) || provider.includes(q);
  });
}

/** Pin the current driver at the front when present; leave others in order. */
export function pinCurrentPilot(models: string[], current: string): string[] {
  if (!current) return models.slice();
  const rest = models.filter((m) => m !== current);
  if (models.includes(current)) return [current, ...rest];
  return rest;
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
): { current: string | null; groups: PilotProviderGroup[] } {
  const filtered = filterPilotModels(models, query);
  const pinned = pinCurrentPilot(filtered, current);
  const currentPinned =
    current && pinned[0] === current ? current : null;
  const remainder = currentPinned ? pinned.slice(1) : pinned;
  return { current: currentPinned, groups: groupPilotModelsByProvider(remainder) };
}
