/**
 * Workspace mutation bus: path matching + dual-event fan-out for Files/SCM/editors.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  HARNESS_FILE_EDITED,
  HARNESS_REPO_MUTATED,
  mutationEventPath,
  notifyWorkspaceMutated,
  pathsReferToSameFile,
  subscribeWorkspaceMutations,
} from "../lib/workspaceMutationEvents";

describe("pathsReferToSameFile", () => {
  it("matches identical and slash-normalized paths", () => {
    expect(pathsReferToSameFile("src/a.ts", "src/a.ts")).toBe(true);
    expect(pathsReferToSameFile("src\\a.ts", "src/a.ts")).toBe(true);
  });

  it("matches abs vs repo-relative forms", () => {
    expect(
      pathsReferToSameFile("/Users/me/proj/src/a.ts", "src/a.ts"),
    ).toBe(true);
    expect(
      pathsReferToSameFile("src/a.ts", "/Users/me/proj/src/a.ts"),
    ).toBe(true);
  });

  it("rejects unrelated paths and bare-basename false friends", () => {
    expect(pathsReferToSameFile("src/a.ts", "src/b.ts")).toBe(false);
    expect(pathsReferToSameFile("a.ts", "src/a.ts")).toBe(false);
  });
});

describe("notifyWorkspaceMutated / subscribeWorkspaceMutations", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("emits both event names with an optional path detail", () => {
    const seen: Array<{ type: string; path: string | null }> = [];
    const onEdited = (e: Event) => {
      seen.push({ type: e.type, path: mutationEventPath(e) });
    };
    const onRepo = (e: Event) => {
      seen.push({ type: e.type, path: mutationEventPath(e) });
    };
    window.addEventListener(HARNESS_FILE_EDITED, onEdited);
    window.addEventListener(HARNESS_REPO_MUTATED, onRepo);
    try {
      notifyWorkspaceMutated("harness/foo.py");
      expect(seen).toEqual([
        { type: HARNESS_FILE_EDITED, path: "harness/foo.py" },
        { type: HARNESS_REPO_MUTATED, path: "harness/foo.py" },
      ]);
    } finally {
      window.removeEventListener(HARNESS_FILE_EDITED, onEdited);
      window.removeEventListener(HARNESS_REPO_MUTATED, onRepo);
    }
  });

  it("debounced subscribe coalesces checkpoint + path-bearing file-edited", () => {
    const handler = vi.fn();
    const unsub = subscribeWorkspaceMutations(handler, { debounceMs: 180 });
    try {
      window.dispatchEvent(new Event(HARNESS_REPO_MUTATED));
      window.dispatchEvent(
        new CustomEvent(HARNESS_FILE_EDITED, { detail: { path: "a.py" } }),
      );
      expect(handler).not.toHaveBeenCalled();
      vi.advanceTimersByTime(180);
      expect(handler).toHaveBeenCalledTimes(1);
      expect(mutationEventPath(handler.mock.calls[0][0])).toBe("a.py");
    } finally {
      unsub();
    }
  });
});
