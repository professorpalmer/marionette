import { useCallback, useEffect, useState } from "react";
import { api, type PendingReview } from "../lib/api";
import {
  applyInFileHunkDecision,
  collectInFilePendingHunks,
  type InFilePendingHunk,
} from "../lib/inFileReview";
import { createInFileReviewExtension } from "./inFileReviewExtension";
import type { Extension } from "@codemirror/state";

/**
 * Load pending reviews for the open editor path and build the in-file
 * Accept/Reject CodeMirror extension (same apply_review + seeded keys as the pane).
 */
export function useInFileReview(editorPath: string): {
  extension: Extension;
  pendingCount: number;
  applyError: string | null;
  clearApplyError: () => void;
} {
  const [reviews, setReviews] = useState<PendingReview[]>([]);
  const [applyingKey, setApplyingKey] = useState<string | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    const fetchReviews = api.getReviews;
    if (typeof fetchReviews !== "function") return;
    void fetchReviews()
      .then((data) => {
        if (Array.isArray(data)) setReviews(data);
      })
      .catch(() => {
        /* keep last-known; editor paint is best-effort */
      });
  }, []);

  useEffect(() => {
    refresh();
    const onRefresh = () => refresh();
    window.addEventListener("harness-reviews-refresh", onRefresh);
    const timer = window.setInterval(refresh, 4000);
    return () => {
      window.removeEventListener("harness-reviews-refresh", onRefresh);
      window.clearInterval(timer);
    };
  }, [refresh]);

  const hunks = collectInFilePendingHunks(reviews, editorPath);

  const onAccept = useCallback(async (item: InFilePendingHunk) => {
    setApplyError(null);
    setApplyingKey(item.decisionKey);
    try {
      const res = await applyInFileHunkDecision(item.review, item.decisionId, "accept");
      if (!res.ok) setApplyError(res.message);
      else refresh();
    } catch (err: any) {
      setApplyError(err?.message || "Error applying review");
    } finally {
      setApplyingKey(null);
    }
  }, [refresh]);

  const onReject = useCallback(async (item: InFilePendingHunk) => {
    setApplyError(null);
    setApplyingKey(item.decisionKey);
    try {
      const res = await applyInFileHunkDecision(item.review, item.decisionId, "reject");
      if (!res.ok) setApplyError(res.message);
      else refresh();
    } catch (err: any) {
      setApplyError(err?.message || "Error applying review");
    } finally {
      setApplyingKey(null);
    }
  }, [refresh]);

  const extension = createInFileReviewExtension(hunks, {
    onAccept,
    onReject,
    applyingKey,
  });

  return {
    extension,
    pendingCount: hunks.length,
    applyError,
    clearApplyError: () => setApplyError(null),
  };
}
