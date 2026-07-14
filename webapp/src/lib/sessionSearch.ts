/** Pure helpers for LeftRail FTS session search (GET /api/sessions/search). */

export type SessionSearchHit = {
  session_id: string;
  snippet: string;
  rank: number;
};

export type SessionSearchRow = {
  id: string;
  title: string;
  snippet: string;
  rank: number;
};

/** Build the query string for `/api/sessions/search`, or null when empty. */
export function buildSessionSearchQuery(raw: string, limit = 20): string | null {
  const q = (raw || "").trim();
  if (!q) return null;
  const params = new URLSearchParams({ q });
  const capped = Math.max(1, Math.min(Number(limit) || 20, 50));
  if (capped !== 20) params.set("limit", String(capped));
  return params.toString();
}

/** Coerce API JSON into hit records; ignores malformed entries. */
export function normalizeSessionSearchHits(raw: unknown): SessionSearchHit[] {
  if (!Array.isArray(raw)) return [];
  const out: SessionSearchHit[] = [];
  for (const row of raw) {
    if (!row || typeof row !== "object") continue;
    const session_id = String((row as SessionSearchHit).session_id || "").trim();
    if (!session_id) continue;
    const snippet = String((row as SessionSearchHit).snippet || "").trim();
    let rank = 0;
    try {
      rank = Number((row as SessionSearchHit).rank);
      if (!Number.isFinite(rank)) rank = 0;
    } catch {
      rank = 0;
    }
    out.push({ session_id, snippet, rank });
  }
  return out;
}

/** Map FTS hits to display rows, resolving titles from a known-session map. */
export function mapSessionSearchHits(
  hits: SessionSearchHit[] | null | undefined,
  titleById: Record<string, string>,
): SessionSearchRow[] {
  if (!hits?.length) return [];
  return hits.map((h) => {
    const known = (titleById[h.session_id] || "").trim();
    return {
      id: h.session_id,
      title: known || "Untitled",
      snippet: h.snippet || "",
      rank: h.rank,
    };
  });
}
