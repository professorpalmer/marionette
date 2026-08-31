import {
  api,
  type PendingReview,
  type PendingReviewFile,
  type PendingReviewHunk,
} from "./api";
import { pathsReferToSameFile } from "./workspaceMutationEvents";
import {
  forEachReviewHunkDecision,
  reviewHunkDecisionKey,
  seedApplyDecisions,
} from "./reviewDecisions";

export type HunkLineKind = "context" | "add" | "del" | "meta";

export type ParsedHunkGeometry = {
  oldStart: number;
  oldCount: number;
  newStart: number;
  newCount: number;
  /** 1-based old-file lines covered by context/delete rows (empty for pure inserts). */
  oldLines: number[];
  /** 1-based old-file line to anchor widgets (insert-after when oldCount is 0). */
  anchorOldLine: number;
  lineKinds: { oldLine: number | null; kind: HunkLineKind; text: string }[];
};

const HUNK_HEADER_RE =
  /^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s@@/;

/** Parse a unified-diff hunk header into old/new ranges. */
export function parseHunkHeader(header: string): {
  oldStart: number;
  oldCount: number;
  newStart: number;
  newCount: number;
} | null {
  const m = header.trim().match(HUNK_HEADER_RE);
  if (!m) return null;
  const oldStart = Number(m[1]);
  const oldCount = m[2] != null ? Number(m[2]) : 1;
  const newStart = Number(m[3]);
  const newCount = m[4] != null ? Number(m[4]) : 1;
  if (![oldStart, oldCount, newStart, newCount].every((n) => Number.isFinite(n))) {
    return null;
  }
  return { oldStart, oldCount, newStart, newCount };
}

/** Map hunk body lines onto old-file geometry for in-editor paint. */
export function parseHunkGeometry(hunk: PendingReviewHunk): ParsedHunkGeometry | null {
  const ranges = parseHunkHeader(hunk.header || "");
  if (!ranges) return null;

  const oldLines: number[] = [];
  const lineKinds: ParsedHunkGeometry["lineKinds"] = [];
  let oldCursor = ranges.oldStart;

  for (const raw of hunk.lines || []) {
    const line = raw.replace(/\n$/, "");
    if (line.startsWith("\\")) {
      lineKinds.push({ oldLine: null, kind: "meta", text: line });
      continue;
    }
    if (line.startsWith("+")) {
      lineKinds.push({ oldLine: null, kind: "add", text: line.slice(1) });
      continue;
    }
    if (line.startsWith("-")) {
      oldLines.push(oldCursor);
      lineKinds.push({ oldLine: oldCursor, kind: "del", text: line.slice(1) });
      oldCursor += 1;
      continue;
    }
    // Context (leading space) or bare context.
    const text = line.startsWith(" ") ? line.slice(1) : line;
    oldLines.push(oldCursor);
    lineKinds.push({ oldLine: oldCursor, kind: "context", text });
    oldCursor += 1;
  }

  const anchorOldLine =
    ranges.oldCount === 0
      ? Math.max(1, ranges.oldStart)
      : oldLines[0] ?? Math.max(1, ranges.oldStart);

  return {
    ...ranges,
    oldLines,
    anchorOldLine,
    lineKinds,
  };
}

export type InFilePendingHunk = {
  review: PendingReview;
  file: PendingReviewFile;
  hunk: PendingReviewHunk;
  geometry: ParsedHunkGeometry;
  decisionKey: string;
  decisionId: string;
};

/** Collect pending hunks whose file path matches the open editor path. */
export function collectInFilePendingHunks(
  reviews: PendingReview[],
  editorPath: string,
): InFilePendingHunk[] {
  const out: InFilePendingHunk[] = [];
  if (!editorPath) return out;

  for (const review of reviews) {
    forEachReviewHunkDecision(review, (hunk, decisionId, fileIndex) => {
      const file = review.files[fileIndex];
      if (!file || !pathsReferToSameFile(file.path, editorPath)) return;
      if (hunk.status && hunk.status !== "pending") return;
      const geometry = parseHunkGeometry(hunk);
      if (!geometry) return;
      out.push({
        review,
        file,
        hunk,
        geometry,
        decisionId,
        decisionKey: reviewHunkDecisionKey(review.id, decisionId),
      });
    });
  }

  out.sort((a, b) => a.geometry.anchorOldLine - b.geometry.anchorOldLine);
  return out;
}

/**
 * Build a fully seeded apply_review payload for one in-file Accept/Reject.
 * Other hunks keep the pane default (accept) so harness reject-default cannot
 * silently drop them.
 */
export function buildInFileApplyDecisions(
  review: PendingReview,
  decisionId: string,
  decision: "accept" | "reject",
): Record<string, "accept" | "reject"> {
  const namespaced: Record<string, "accept" | "reject"> = {
    [reviewHunkDecisionKey(review.id, decisionId)]: decision,
  };
  return seedApplyDecisions(review, namespaced);
}

export type InFileApplyResult = {
  ok: boolean;
  message: string;
};

/** Apply one in-file hunk decision via the shared apply_review API. */
export async function applyInFileHunkDecision(
  review: PendingReview,
  decisionId: string,
  decision: "accept" | "reject",
): Promise<InFileApplyResult> {
  const payload = buildInFileApplyDecisions(review, decisionId, decision);
  const res = await api.applyReview(review.id, payload);
  if (!res.ok) {
    return { ok: false, message: res.message || "Failed to apply" };
  }
  window.dispatchEvent(new Event("harness-repo-mutated"));
  window.dispatchEvent(new Event("harness-config-changed"));
  window.dispatchEvent(new Event("harness-reviews-refresh"));
  return { ok: true, message: res.message || "Applied successfully" };
}
