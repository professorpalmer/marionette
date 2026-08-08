import { beforeEach, describe, expect, it } from "vitest";
import {
  DEFAULT_STATE_PANE_VISIBLE_CARDS,
  loadStatePaneVisibleCards,
  normalizeStatePaneVisibleCards,
  revealStatePaneCard,
  saveStatePaneVisibleCards,
  STATE_PANE_VISIBLE_CARDS_KEY,
  toggleStatePaneCardVisibility,
} from "../lib/statePaneVisibility";

describe("statePaneVisibility", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("uses a calm minimal default (Environment off, primary surfaces on)", () => {
    expect(DEFAULT_STATE_PANE_VISIBLE_CARDS).toEqual({
      codegraph: true,
      wiki: true,
      environment: false,
      mcp: true,
    });
    expect(loadStatePaneVisibleCards()).toEqual(DEFAULT_STATE_PANE_VISIBLE_CARDS);
  });

  it("persists toggles under the versioned key", () => {
    const next = toggleStatePaneCardVisibility(DEFAULT_STATE_PANE_VISIBLE_CARDS, "environment");
    expect(next.environment).toBe(true);
    saveStatePaneVisibleCards(next);
    expect(localStorage.getItem(STATE_PANE_VISIBLE_CARDS_KEY)).toContain('"environment":true');
    expect(loadStatePaneVisibleCards()).toEqual(next);
  });

  it("falls back safely on corrupt or non-object storage", () => {
    localStorage.setItem(STATE_PANE_VISIBLE_CARDS_KEY, "{not-json");
    expect(loadStatePaneVisibleCards()).toEqual(DEFAULT_STATE_PANE_VISIBLE_CARDS);

    expect(normalizeStatePaneVisibleCards(null)).toEqual(DEFAULT_STATE_PANE_VISIBLE_CARDS);
    expect(normalizeStatePaneVisibleCards("wiki")).toEqual(DEFAULT_STATE_PANE_VISIBLE_CARDS);
    expect(normalizeStatePaneVisibleCards([])).toEqual(DEFAULT_STATE_PANE_VISIBLE_CARDS);
  });

  it("ignores unknown keys and non-boolean values while merging known ones", () => {
    expect(
      normalizeStatePaneVisibleCards({
        codegraph: false,
        wiki: "yes",
        environment: true,
        mcp: 1,
        leftover: true,
      }),
    ).toEqual({
      codegraph: false,
      wiki: true,
      environment: true,
      mcp: true,
    });
  });

  it("forces CodeGraph on when storage would hide every card", () => {
    expect(
      normalizeStatePaneVisibleCards({
        codegraph: false,
        wiki: false,
        environment: false,
        mcp: false,
      }),
    ).toEqual({
      codegraph: true,
      wiki: false,
      environment: false,
      mcp: false,
    });
  });

  it("refuses to toggle off the last visible card", () => {
    const onlyCg = {
      codegraph: true,
      wiki: false,
      environment: false,
      mcp: false,
    };
    expect(toggleStatePaneCardVisibility(onlyCg, "codegraph")).toEqual(onlyCg);
  });

  it("revealStatePaneCard is a no-op when already visible", () => {
    const base = { ...DEFAULT_STATE_PANE_VISIBLE_CARDS };
    expect(revealStatePaneCard(base, "mcp")).toBe(base);
    expect(revealStatePaneCard(base, "environment")).toEqual({
      ...base,
      environment: true,
    });
  });
});
