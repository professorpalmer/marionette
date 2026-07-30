/** Display-side model identity helpers for SwarmPane badges.

Mirrors the *display* half of `harness/model_identity.py` without re-implementing
envelope stamping: collapse repeated `agentic/` / `native/` prefixes, and strip
one engine segment for auto-routed badges. Explicit pins keep the full registry id.
*/

const ENGINE_LABELS = new Set(["agentic", "native"]);

/** Strip every leading `agentic/` or `native/` segment (idempotent). */
export function stripEnginePrefixes(modelId: string): string {
  let mid = (modelId || "").trim();
  while (mid.includes("/")) {
    const slash = mid.indexOf("/");
    const head = mid.slice(0, slash).toLowerCase();
    const rest = mid.slice(slash + 1);
    if (ENGINE_LABELS.has(head) && rest) {
      mid = rest;
      continue;
    }
    break;
  }
  return mid;
}

/** Collapse `agentic/agentic/x` → `agentic/x`; bare ids stay bare. */
export function collapseEnginePrefixes(modelId: string): string {
  const mid = (modelId || "").trim();
  if (!mid) return "";
  const slash = mid.indexOf("/");
  if (slash < 0) return mid;
  const head = mid.slice(0, slash).toLowerCase();
  if (!ENGINE_LABELS.has(head)) return mid;
  const body = stripEnginePrefixes(mid);
  return body ? `${head}/${body}` : head;
}

/**
 * Badge text for a routed/job model id.
 * - explicit_pin: keep full collapsed registry id (`agentic/meta/...`)
 * - otherwise: strip engine prefixes for scannability next to the adapter chip
 */
export function displayModelId(
  modelId: string,
  opts?: { policy?: string; adapterFallback?: string },
): string {
  const raw = (modelId || "").trim();
  const adapter = (opts?.adapterFallback || "").trim();
  if (!raw) return adapter;
  const collapsed = collapseEnginePrefixes(raw);
  if ((opts?.policy || "").trim() === "explicit_pin") return collapsed || adapter;
  return stripEnginePrefixes(collapsed).trim() || adapter;
}

/** Identity equality after stripping engine prefixes (case-insensitive). */
export function modelIdsEqual(left: string, right: string): boolean {
  const a = stripEnginePrefixes(left).trim().toLowerCase();
  const b = stripEnginePrefixes(right).trim().toLowerCase();
  if (!a || !b) return false;
  return a === b;
}
