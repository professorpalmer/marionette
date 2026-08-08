import { beforeEach, describe, expect, it } from "vitest";
import {
  DEFAULT_RIGHT_PANE_TAB_VISIBILITY,
  loadRightPaneTabVisibility,
  normalizeRightPaneTabVisibility,
  RIGHT_PANE_TAB_VISIBILITY_KEY,
  saveRightPaneTabVisibility,
  toggleRightPaneTabVisibility,
} from "../lib/rightPaneTabVisibility";

describe("rightPaneTabVisibility", () => {
  beforeEach(() => localStorage.clear());

  it("uses a calm default while keeping Settings pinned", () => {
    expect(DEFAULT_RIGHT_PANE_TAB_VISIBILITY).toMatchObject({
      state: true,
      swarm: true,
      files: true,
      git: true,
      terminal: true,
      browser: true,
      settings: true,
      worktrees: false,
      review: false,
      checkpoints: false,
    });
    expect(loadRightPaneTabVisibility()).toEqual(DEFAULT_RIGHT_PANE_TAB_VISIBILITY);
  });

  it("persists optional tab choices under the versioned key", () => {
    const next = toggleRightPaneTabVisibility(DEFAULT_RIGHT_PANE_TAB_VISIBILITY, "review");
    saveRightPaneTabVisibility(next);

    expect(localStorage.getItem(RIGHT_PANE_TAB_VISIBILITY_KEY)).toContain('"review":true');
    expect(loadRightPaneTabVisibility()).toEqual(next);
  });

  it("ignores malformed values and attempts to override required tabs", () => {
    expect(normalizeRightPaneTabVisibility(null)).toEqual(DEFAULT_RIGHT_PANE_TAB_VISIBILITY);
    expect(
      normalizeRightPaneTabVisibility({
        state: false,
        settings: false,
        worktrees: true,
        review: "yes",
        unknown: true,
      }),
    ).toEqual({
      ...DEFAULT_RIGHT_PANE_TAB_VISIBILITY,
      worktrees: true,
    });

    localStorage.setItem(RIGHT_PANE_TAB_VISIBILITY_KEY, "{broken");
    expect(loadRightPaneTabVisibility()).toEqual(DEFAULT_RIGHT_PANE_TAB_VISIBILITY);
  });
});
