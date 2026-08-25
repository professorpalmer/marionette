/** Display-side model identity helpers for SwarmPane badges.

Mirrors the *display* half of `harness/model_identity.py` without re-implementing
envelope stamping: collapse repeated `agentic/` / `native/` prefixes, then always
strip engine segments so pin and auto-route badges show the same worker model.
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

/** True when id is empty or only an adapter label (agentic/native). */
export function isEngineOnlyModelId(modelId: string): boolean {
  const raw = (modelId || "").trim();
  if (!raw) return true;
  const body = stripEnginePrefixes(raw);
  if (!body || ENGINE_LABELS.has(body.toLowerCase())) return true;
  const collapsed = collapseEnginePrefixes(raw);
  return !collapsed || ENGINE_LABELS.has(collapsed.toLowerCase());
}

/**
 * Badge text for a routed/job model id.
 * Always strip engine prefixes so the same worker model looks the same
 * whether ROUTING stamped explicit_pin or balanced. Adapter chip stays
 * separate; never surface bare agentic/native.
 */
export function displayModelId(
  modelId: string,
  opts?: { policy?: string; adapterFallback?: string },
): string {
  const raw = (modelId || "").trim();
  const adapter = (opts?.adapterFallback || "").trim();
  const safeAdapter = isEngineOnlyModelId(adapter) ? "" : adapter;
  if (!raw || isEngineOnlyModelId(raw)) return safeAdapter;
  return stripEnginePrefixes(collapseEnginePrefixes(raw)).trim() || safeAdapter;
}

/** Identity equality after stripping engine prefixes (case-insensitive). */
export function modelIdsEqual(left: string, right: string): boolean {
  const a = stripEnginePrefixes(left).trim().toLowerCase();
  const b = stripEnginePrefixes(right).trim().toLowerCase();
  if (!a || !b) return false;
  return a === b;
}
