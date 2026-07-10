import { beforeEach, describe, expect, it, vi } from "vitest";

describe("project selection ownership", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.resetModules();
  });

  it("does not restore a stale project root from localStorage", async () => {
    localStorage.setItem(
      "pmharness.lastProjectRoot",
      "C:\\Users\\pwall\\.marionette\\marionette",
    );

    const { lastSelectedProjectRoot } = await import("../lib/panelTransition");

    expect(lastSelectedProjectRoot()).toBe("");
  });

  it("seeds late-mounted panes from this renderer's authoritative event", async () => {
    const { dispatchProjectSelected, lastSelectedProjectRoot } = await import(
      "../lib/panelTransition"
    );
    const root = "C:\\Users\\pwall\\portable-llm-wiki";
    let announced = "";
    window.addEventListener(
      "harness-project-selected",
      (event) => {
        announced = (event as CustomEvent<string>).detail;
      },
      { once: true },
    );

    dispatchProjectSelected(root);

    expect(lastSelectedProjectRoot()).toBe(root);
    expect(announced).toBe(root);
    expect(localStorage.getItem("pmharness.lastProjectRoot")).toBeNull();
  });
});
