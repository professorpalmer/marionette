/**
 * Shared workspace mutation bus for Files / SCM / open editors.
 *
 * Agent writes historically fired only `harness-repo-mutated` (via checkpoint)
 * while FileTree listened for `harness-file-edited`. Listeners should subscribe
 * to both; emitters should prefer path-bearing CustomEvents so open buffers can
 * reload the matching tab.
 */

import { normalizeTabPath } from "../components/conversation/tabPaths";

export const HARNESS_REPO_MUTATED = "harness-repo-mutated";
export const HARNESS_FILE_EDITED = "harness-file-edited";

export type WorkspaceMutationDetail = {
  path?: string;
};

function looksAbsolutePath(path: string): boolean {
  return path.startsWith("/") || /^[A-Za-z]:\//.test(path);
}

/** True when two paths name the same file (abs vs repo-relative, slash variants). */
export function pathsReferToSameFile(a: string, b: string): boolean {
  const left = normalizeTabPath(a).replace(/\/+$/, "");
  const right = normalizeTabPath(b).replace(/\/+$/, "");
  if (!left || !right) return false;
  if (left === right) return true;
  // Abs vs repo-relative only — never treat a bare basename as every nested
  // file that shares the name (`a.ts` must not match `pkg/a.ts`).
  if (looksAbsolutePath(left) && left.endsWith("/" + right)) return true;
  if (looksAbsolutePath(right) && right.endsWith("/" + left)) return true;
  return false;
}

export function mutationEventPath(event: Event): string | null {
  const detail = (event as CustomEvent<WorkspaceMutationDetail>).detail;
  if (!detail || typeof detail !== "object") return null;
  const path = detail.path;
  return typeof path === "string" && path.trim() ? path.trim() : null;
}

/** Notify Files + SCM + editors that the workspace (optionally one path) changed. */
export function notifyWorkspaceMutated(path?: string): void {
  const trimmed = typeof path === "string" ? path.trim() : "";
  const detail: WorkspaceMutationDetail | undefined = trimmed
    ? { path: trimmed }
    : undefined;
  window.dispatchEvent(
    detail
      ? new CustomEvent(HARNESS_FILE_EDITED, { detail })
      : new Event(HARNESS_FILE_EDITED),
  );
  window.dispatchEvent(
    detail
      ? new CustomEvent(HARNESS_REPO_MUTATED, { detail })
      : new Event(HARNESS_REPO_MUTATED),
  );
}

/**
 * Subscribe to both mutation event names. Optional debounce coalesces a
 * checkpoint + action_result pair into one refresh.
 */
export function subscribeWorkspaceMutations(
  handler: (event: Event) => void,
  opts?: { debounceMs?: number },
): () => void {
  const debounceMs = opts?.debounceMs;
  let timer: number | null = null;
  let lastEvent: Event | null = null;

  const onEvent = (event: Event) => {
    if (debounceMs == null || debounceMs <= 0) {
      handler(event);
      return;
    }
    lastEvent = event;
    if (timer != null) window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      timer = null;
      const pending = lastEvent;
      lastEvent = null;
      if (pending) handler(pending);
    }, debounceMs);
  };

  window.addEventListener(HARNESS_REPO_MUTATED, onEvent);
  window.addEventListener(HARNESS_FILE_EDITED, onEvent);
  return () => {
    if (timer != null) window.clearTimeout(timer);
    window.removeEventListener(HARNESS_REPO_MUTATED, onEvent);
    window.removeEventListener(HARNESS_FILE_EDITED, onEvent);
  };
}
