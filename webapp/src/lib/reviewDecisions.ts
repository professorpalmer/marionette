import type { PendingReview } from "./api";

/** Namespace hunk decisions per review so concurrent pending reviews cannot collide. */
export function reviewHunkDecisionKey(reviewId: string, hunkId: string): string {
  return `${reviewId}::${hunkId}`;
}

/**
 * Build the apply_review payload: every hunk id is seeded to match the painted
 * UI default (accept). Harness defaults missing keys to reject — omitting keys
 * would silently drop hunks the pane still shows as accepted.
 */
export function seedApplyDecisions(
  review: PendingReview,
  decisions: Record<string, "accept" | "reject">,
): Record<string, "accept" | "reject"> {
  const out: Record<string, "accept" | "reject"> = {};
  for (const file of review.files) {
    for (const hunk of file.hunks) {
      const key = reviewHunkDecisionKey(review.id, hunk.id);
      out[hunk.id] = decisions[key] ?? "accept";
    }
  }
  return out;
}
