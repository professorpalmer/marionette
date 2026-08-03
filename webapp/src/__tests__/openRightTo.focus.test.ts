/**
 * openRightTo must always apply harness-focus-tab — including when the right
 * pane is already open (setRightOpen(true) is then a no-op and the rightOpen
 * effect does not re-run).
 *
 * Mirrors the App.tsx openRightTo logic hermetically without mounting App.
 */
import { describe, expect, it, vi } from "vitest";

function openRightToLikeApp(
  tab: string,
  opts: {
    rightOpen: boolean;
    setRightOpen: (updater: (open: boolean) => boolean) => void;
    pendingRightTab: { current: string | null };
  },
) {
  const target = tab || "state";
  opts.pendingRightTab.current = target;
  opts.setRightOpen((open) => {
    if (open) {
      opts.pendingRightTab.current = null;
      window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: target }));
    }
    return true;
  });
}

describe("openRightTo focus when pane already open", () => {
  it("dispatches harness-focus-tab immediately when rightOpen is already true", () => {
    const seen: string[] = [];
    const onFocus = (e: Event) => {
      seen.push(String((e as CustomEvent).detail));
    };
    window.addEventListener("harness-focus-tab", onFocus as EventListener);
    try {
      const pendingRightTab = { current: null as string | null };
      let rightOpen = true;
      openRightToLikeApp("settings", {
        rightOpen,
        pendingRightTab,
        setRightOpen: (updater) => {
          rightOpen = updater(rightOpen);
        },
      });
      expect(rightOpen).toBe(true);
      expect(seen).toEqual(["settings"]);
      expect(pendingRightTab.current).toBeNull();
    } finally {
      window.removeEventListener("harness-focus-tab", onFocus as EventListener);
    }
  });

  it("defers focus via pendingRightTab when the pane is closed", () => {
    const dispatch = vi.spyOn(window, "dispatchEvent");
    const pendingRightTab = { current: null as string | null };
    let rightOpen = false;
    openRightToLikeApp("settings", {
      rightOpen,
      pendingRightTab,
      setRightOpen: (updater) => {
        rightOpen = updater(rightOpen);
      },
    });
    expect(rightOpen).toBe(true);
    expect(pendingRightTab.current).toBe("settings");
    // No immediate focus dispatch — the rightOpen effect applies it after mount.
    expect(
      dispatch.mock.calls.some(
        (c) => c[0] instanceof CustomEvent && c[0].type === "harness-focus-tab",
      ),
    ).toBe(false);
    dispatch.mockRestore();
  });
});
