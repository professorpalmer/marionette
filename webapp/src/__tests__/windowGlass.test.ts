import { afterEach, describe, expect, it } from "vitest";
import {
  applyGlassSurfaces,
  beginTranslucencyPeek,
  DEFAULT_GLASS_MATERIAL,
  DEFAULT_INTENSITY,
  TRANSLUCENCY_DEFAULTS_VERSION,
  TRANSLUCENCY_STORAGE_KEY,
  endTranslucencyPeek,
  glassActive,
  glassSurfaceKeep,
  normalizeRendererState,
  readStoredTranslucency,
  resetTranslucencyPeek,
  writeStoredTranslucency,
} from "../lib/windowGlass";

afterEach(() => {
  resetTranslucencyPeek(document.documentElement);
  document.documentElement.removeAttribute("data-marionette-glass");
  document.documentElement.style.removeProperty("--translucency-glass-keep");
  document.documentElement.style.removeProperty("background-color");
  localStorage.removeItem(TRANSLUCENCY_STORAGE_KEY);
});

describe("normalizeRendererState", () => {
  it("defaults to Soft 40 on junk", () => {
    expect(normalizeRendererState(null)).toEqual({
      intensity: DEFAULT_INTENSITY,
      fade: 0,
      mode: "glass",
      material: DEFAULT_GLASS_MATERIAL,
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

describe("readStoredTranslucency", () => {
  it("forces Soft 40 when the saved blob has no defaults version", () => {
    localStorage.setItem(
      TRANSLUCENCY_STORAGE_KEY,
      JSON.stringify({ intensity: 0, fade: 0, mode: "glass", material: "under-window" }),
    );
    expect(readStoredTranslucency()).toEqual({
      intensity: DEFAULT_INTENSITY,
      fade: 0,
      mode: "glass",
      material: DEFAULT_GLASS_MATERIAL,
    });
    expect(JSON.parse(localStorage.getItem(TRANSLUCENCY_STORAGE_KEY) || "{}")).toMatchObject({
      intensity: DEFAULT_INTENSITY,
      material: DEFAULT_GLASS_MATERIAL,
      defaultsVersion: TRANSLUCENCY_DEFAULTS_VERSION,
    });
  });

  it("keeps a post-update off after the factory flip", () => {
    writeStoredTranslucency({ intensity: 0, fade: 0, mode: "glass", material: "titlebar" });
    expect(readStoredTranslucency()).toEqual({
      intensity: 0,
      fade: 0,
      mode: "glass",
      material: "titlebar",
    });
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
