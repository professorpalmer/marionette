import { afterEach, describe, expect, it } from "vitest";
import {
  applyGlassSurfaces,
  beginTranslucencyPeek,
  endTranslucencyPeek,
  glassActive,
  glassSurfaceKeep,
  normalizeRendererState,
  resetTranslucencyPeek,
} from "../lib/windowGlass";

afterEach(() => {
  resetTranslucencyPeek(document.documentElement);
  document.documentElement.removeAttribute("data-marionette-glass");
  document.documentElement.style.removeProperty("--translucency-glass-keep");
  document.documentElement.style.removeProperty("background-color");
});

describe("normalizeRendererState", () => {
  it("defaults to glass off on junk", () => {
    expect(normalizeRendererState(null)).toEqual({
      intensity: 0,
      fade: 0,
      mode: "glass",
      material: "under-window",
    });
  });

  it("keeps a saved frost rung", () => {
    expect(normalizeRendererState({ intensity: 60, mode: "glass", material: "titlebar" }).material)
      .toBe("titlebar");
  });
});

describe("applyGlassSurfaces", () => {
  it("marks the root and publishes the keep token while glass is live", () => {
    applyGlassSurfaces({ intensity: 60, fade: 0, mode: "glass", material: "header" });
    expect(document.documentElement.hasAttribute("data-marionette-glass")).toBe(true);
    expect(document.documentElement.style.getPropertyValue("--translucency-glass-keep")).toBe("40%");
    expect(glassActive({ intensity: 60, fade: 0, mode: "glass", material: "header" })).toBe(true);
    expect(glassSurfaceKeep(60)).toBe(40);
  });

  it("clears the mark when the lever is off", () => {
    applyGlassSurfaces({ intensity: 60, fade: 0, mode: "glass", material: "header" });
    applyGlassSurfaces({ intensity: 0, fade: 0, mode: "glass", material: "header" });
    expect(document.documentElement.hasAttribute("data-marionette-glass")).toBe(false);
    expect(document.documentElement.style.getPropertyValue("--translucency-glass-keep")).toBe("");
  });
});

describe("translucency peek", () => {
  it("holds the peek attribute across overlapping begins", () => {
    beginTranslucencyPeek();
    beginTranslucencyPeek();
    expect(document.documentElement.hasAttribute("data-marionette-glass-peek")).toBe(true);
    endTranslucencyPeek();
    expect(document.documentElement.hasAttribute("data-marionette-glass-peek")).toBe(true);
    endTranslucencyPeek();
    expect(document.documentElement.hasAttribute("data-marionette-glass-peek")).toBe(false);
  });
});
