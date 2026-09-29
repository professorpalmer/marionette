import { useEffect } from "react";
import { api } from "./api";

/**
 * While CodeGraph indexes (or waits on scope), poll its cheap status and call
 * `onChanged` once it moves, so the badge flips without a session switch.
 * /api/codegraph answers from memory while indexing; the full workspace read
 * (three git processes) is left to `onChanged`.
 */
export function useCodegraphIndexPoll(status: string | undefined, onChanged: () => void, intervalMs = 4000): void {
  useEffect(() => {
    if (status !== "indexing" && status !== "needs_scope") return;
    let active = true;
    const poll = () => {
      if (document.hidden) return;
      void api.getCodegraph()
        .then((cg) => { if (active && cg.status !== status) onChanged(); })
        .catch(() => {});
    };
    poll();
    const timer = setInterval(poll, intervalMs);
    return () => { active = false; clearInterval(timer); };
  }, [status, onChanged, intervalMs]);
}
