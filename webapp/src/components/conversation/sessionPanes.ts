/**
 * Keep-alive session panes: last N transcripts stay mounted (hidden)
 * so a high-frequency session click does not teardown markdown/virtualizer.
 *
 * Cap dormant panes. Never evict the active id or a busy mid-turn session.
 */

export const MAX_DORMANT_SESSION_PANES = 3;

export function retainSessionPanes(args: {
  prev: readonly string[];
  activeId: string | null | undefined;
  busyIds?: readonly string[];
  maxDormant?: number;
}): string[] {
  const maxDormant = args.maxDormant ?? MAX_DORMANT_SESSION_PANES;
  const active = String(args.activeId || "").trim();
  // Explicit clear / no session: tear down keep-alive panes so deleted
  // history cannot stay queryable in a hidden list.
  if (!active) return [];
  const busy = new Set(
    (args.busyIds || []).map((id) => String(id || "").trim()).filter(Boolean),
  );
  const seen = new Set<string>();
  const out: string[] = [];
  const push = (id: string) => {
    const next = String(id || "").trim();
    if (!next || seen.has(next)) return;
    seen.add(next);
    out.push(next);
  };
  if (active) push(active);
  for (const id of args.prev) {
    if (id === active) continue;
    push(id);
  }
  for (const id of busy) {
    if (id === active) continue;
    push(id);
  }
  while (out.length > 1 + maxDormant) {
    let evict = -1;
    for (let i = out.length - 1; i >= 0; i -= 1) {
      const id = out[i];
      if (id !== active && !busy.has(id)) {
        evict = i;
        break;
      }
    }
    if (evict < 0) break;
    out.splice(evict, 1);
  }
  return out;
}

export function shouldReleaseOutgoingSessionChrome(args: {
  outgoingId: string;
  retainedIds: readonly string[];
}): boolean {
  const outgoing = String(args.outgoingId || "").trim();
  if (!outgoing) return false;
  return !args.retainedIds.includes(outgoing);
}
