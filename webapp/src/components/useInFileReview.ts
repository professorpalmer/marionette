import { usePolling } from "../lib/usePolling";
import { JOB_SCOPE_CHANGED_EVENT } from "../lib/jobScope";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
 * Accept/Reject CodeMirror extension with explicit selected-hunk scope.
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

  const [scopeEpoch, setScopeEpoch] = useState(0);
  const scope = useMemo(() => ({ editorPath, scopeEpoch }), [editorPath, scopeEpoch]);
  const currentScope = useRef<typeof scope | null>(scope);
  currentScope.current = scope;
  const request = useRef(0);
  const pending = useRef<Promise<unknown> | undefined>(undefined);
  const refresh = useCallback(() => {
    const fetchReviews = api.getReviews;
    if (typeof fetchReviews !== "function" || currentScope.current !== scope) return;
    const generation = ++request.current;
    const promise = fetchReviews()
      .then((data) => {
        if (currentScope.current === scope && generation === request.current && Array.isArray(data)) setReviews(data);
      })
      .catch(() => {
        /* keep last-known; editor paint is best-effort */
      });
    pending.current = promise;
    void promise.finally(() => {
      if (pending.current === promise) pending.current = undefined;
    });
    return promise;
  }, [scope]);

  useEffect(() => {
    const invalidate = () => {
      currentScope.current = null;
      setScopeEpoch(epoch => epoch + 1);
    };
    const events = ["harness-session-changed", "harness-project-selected", JOB_SCOPE_CHANGED_EVENT];
    events.forEach(event => window.addEventListener(event, invalidate));
    return () => events.forEach(event => window.removeEventListener(event, invalidate));
  }, []);

  useEffect(() => {
    currentScope.current = scope;
    setReviews([]);
    setApplyError(null);
    setApplyingKey(null);
    pending.current = undefined;
    const onRefresh = () => { void refresh(); };
    window.addEventListener("harness-reviews-refresh", onRefresh);
    return () => {
      currentScope.current = null;
      window.removeEventListener("harness-reviews-refresh", onRefresh);
    };
  }, [refresh, scope]);
  usePolling(() => pending.current ?? refresh(), 4000, {
    scopeKey: JSON.stringify([editorPath, scopeEpoch]),
  });

  const hunks = collectInFilePendingHunks(reviews, editorPath);

  const onAccept = useCallback(async (item: InFilePendingHunk) => {
    setApplyError(null);
    setApplyingKey(item.decisionKey);
    try {
      const res = await applyInFileHunkDecision(item.review, item.decisionId, "accept");
      if (currentScope.current !== scope) return;
      if (!res.ok) setApplyError(res.message);
    } catch (err: unknown) {
      if (currentScope.current === scope) setApplyError(err instanceof Error ? err.message : "Error applying review");
    } finally {
      if (currentScope.current === scope) setApplyingKey(null);
    }
  }, [refresh, scope]);

  const onReject = useCallback(async (item: InFilePendingHunk) => {
    setApplyError(null);
    setApplyingKey(item.decisionKey);
    try {
      const res = await applyInFileHunkDecision(item.review, item.decisionId, "reject");
      if (currentScope.current !== scope) return;
      if (!res.ok) setApplyError(res.message);
    } catch (err: unknown) {
      if (currentScope.current === scope) setApplyError(err instanceof Error ? err.message : "Error applying review");
    } finally {
      if (currentScope.current === scope) setApplyingKey(null);
    }
  }, [refresh, scope]);

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
