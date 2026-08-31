import type { PendingReview, PendingReviewHunk } from "./api";

/** FNV-1a 64-bit — must match harness/diffreview.py hunk_content_fingerprint. */
function fnv1a64Hex(blob: string): string {
  let h = 14695981039346656037n;
  const bytes = new TextEncoder().encode(blob);
  for (const b of bytes) {
    h ^= BigInt(b);
    h = (h * 1099511628211n) & 0xffffffffffffffffn;
  }
  return h.toString(16).padStart(16, "0");
}

/** Deterministic content fingerprint (path + header + body). Never array index. */
export function hunkContentFingerprint(
  path: string,
  header: string,
  lines: readonly string[] | undefined,
): string {
  const parts = [String(path || ""), String(header || "")];
  for (const line of lines || []) {
    parts.push(String(line).replace(/\n$/, ""));
  }
  return fnv1a64Hex(parts.join("\n"));
}

/**
 * Resolve the stable per-hunk decision identity.
 * Prefer server-assigned decision_id; legacy reviews fall back to content fingerprint
 * + same-fingerprint ordinal so exact duplicates stay distinct and reordering
 * unrelated hunks cannot change existing keys.
 */
export function resolveHunkDecisionId(
  hunk: PendingReviewHunk,
  path: string,
  fingerprintCounts: Map<string, number>,
): string {
  const existing = String(hunk.decision_id || "").trim();
  if (existing) return existing;
  const fp = hunkContentFingerprint(path, hunk.header || "", hunk.lines);
  const n = fingerprintCounts.get(fp) || 0;
  fingerprintCounts.set(fp, n + 1);
  return `${fp}#${n}`;
}

/** Namespace decisions per review + stable decision identity. */
export function reviewHunkDecisionKey(reviewId: string, decisionId: string): string {
  return `${reviewId}::${decisionId}`;
}

/** Apply payload key: the stable decision identity (never a bare colliding hunk.id). */
export function hunkDecisionApplyKey(decisionId: string): string {
  return decisionId;
}

/**
 * Build the apply_review payload: every hunk is seeded to match the painted UI
 * default (accept). Keys are stable decision identities so duplicate hunk.id
 * values cannot overwrite one another. Harness defaults missing keys to reject —
 * omitting keys would silently drop hunks the pane still shows as accepted.
 */
export function seedApplyDecisions(
  review: PendingReview,
  decisions: Record<string, "accept" | "reject">,
): Record<string, "accept" | "reject"> {
  const out: Record<string, "accept" | "reject"> = {};
  const fingerprintCounts = new Map<string, number>();
  for (const file of review.files) {
    for (const hunk of file.hunks) {
      const decisionId = resolveHunkDecisionId(hunk, file.path, fingerprintCounts);
      const uiKey = reviewHunkDecisionKey(review.id, decisionId);
      const legacyUiKey = `${review.id}::${hunk.id}`;
      const applyKey = hunkDecisionApplyKey(decisionId);
      out[applyKey] = decisions[uiKey] ?? decisions[legacyUiKey] ?? "accept";
    }
  }
  return out;
}

/** Walk every hunk in review order, yielding its stable decision identity. */
export function forEachReviewHunkDecision(
  review: PendingReview,
  visit: (
    hunk: PendingReview["files"][number]["hunks"][number],
    decisionId: string,
    fileIndex: number,
  ) => void,
): void {
  const fingerprintCounts = new Map<string, number>();
  review.files.forEach((file, fileIndex) => {
    for (const hunk of file.hunks) {
      const decisionId = resolveHunkDecisionId(hunk, file.path, fingerprintCounts);
      visit(hunk, decisionId, fileIndex);
    }
  });
}
