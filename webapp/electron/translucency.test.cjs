const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  ENABLE_INTENSITY,
  GLASS_MATERIALS,
  THEMED_BACKGROUND,
  applyWindowTranslucency,
  backgroundMaterialFor,
  capabilities,
  clampIntensity,
  createTranslucencyController,
  glassActive,
  glassMaterialForPicker,
  glassMaterialsFor,
  glassSurfaceKeep,
  glassSupportedOn,
  normalizeMode,
  normalizeState,
  translucencyDiff,
  translucencySupportedOn,
  vibrancyFor,
  windowBackingOptions,
  windowOpacityFor,
  windowSurfaceOptions,
} = require("./translucency.cjs");

function glass(intensity, material) {
  return normalizeState(
    { intensity, mode: "glass", material, fade: 0 },
    true,
  );
}

function clear(intensity) {
  return normalizeState({ intensity, mode: "clear" }, true);
}

describe("clampIntensity", () => {
  it("floors junk and clamps the lever", () => {
    assert.equal(clampIntensity("nope"), 0);
    assert.equal(clampIntensity(-4), 0);
    assert.equal(clampIntensity(140), 100);
    assert.equal(clampIntensity(55.6), 56);
  });
});

describe("platform support", () => {
  it("offers translucency on macOS and Windows only", () => {
    assert.equal(translucencySupportedOn("darwin"), true);
    assert.equal(translucencySupportedOn("win32"), true);
    assert.equal(translucencySupportedOn("linux"), false);
  });

  it("requires Windows 11 22H2 for glass", () => {
    assert.equal(glassSupportedOn("darwin"), true);
    assert.equal(glassSupportedOn("linux"), false);
    assert.equal(glassSupportedOn("win32", "10.0.22621"), true);
    assert.equal(glassSupportedOn("win32", "10.0.19045"), false);
    assert.equal(glassSupportedOn("win32", ""), false);
  });
});

describe("normalizeMode", () => {
  it("pre-selects glass on a capable OS when nothing is saved", () => {
    assert.equal(normalizeMode(undefined, true, 0), "glass");
  });

  it("keeps a legacy non-zero intensity on clear", () => {
    assert.equal(normalizeMode(undefined, true, 40), "clear");
  });

  it("forces clear when glass is unsupported", () => {
    assert.equal(normalizeMode("glass", false, 0), "clear");
  });
});

describe("glassActive / keep / opacity", () => {
  it("is live only for glass with a raised lever", () => {
    assert.equal(glassActive(glass(0, "header")), false);
    assert.equal(glassActive(glass(60, "header")), true);
    assert.equal(glassActive(clear(60)), false);
  });

  it("thins the tint linearly to zero", () => {
    assert.equal(glassSurfaceKeep(0), 100);
    assert.equal(glassSurfaceKeep(60), 40);
    assert.equal(glassSurfaceKeep(100), 0);
  });

  it("fades native opacity from fade under glass, intensity under clear", () => {
    assert.equal(windowOpacityFor(glass(80, "header")), 1);
    assert.ok(windowOpacityFor(clear(80)) < 1);
    assert.ok(windowOpacityFor(normalizeState({ intensity: 80, fade: 80, mode: "glass" }, true)) < 1);
  });
});

describe("vibrancyFor / backgroundMaterialFor", () => {
  it("turns native material off when glass is off", () => {
    assert.equal(vibrancyFor(glass(0, "header")), null);
    assert.equal(vibrancyFor(clear(60)), null);
    assert.equal(backgroundMaterialFor(glass(0, "header")), "none");
  });

  it("maps the sheer to heavy frost ladder", () => {
    assert.equal(vibrancyFor(glass(60, "header")), "header");
    assert.equal(backgroundMaterialFor(glass(60, "under-window")), "acrylic");
    assert.equal(backgroundMaterialFor(glass(60, "popover")), "tabbed");
    assert.equal(backgroundMaterialFor(glass(60, "titlebar")), "mica");
    assert.equal(backgroundMaterialFor(glass(60, "header")), "mica");
  });

  it("drops the duplicate mica rung from the Windows picker", () => {
    assert.deepEqual(glassMaterialsFor(false), GLASS_MATERIALS);
    assert.deepEqual(glassMaterialsFor(true), ["under-window", "popover", "titlebar"]);
    assert.equal(glassMaterialForPicker("header", true), "titlebar");
    assert.equal(glassMaterialForPicker("header", false), "header");
  });
});

describe("windowBackingOptions / surfaceOptions", () => {
  it("omits backgroundColor while glass is live", () => {
    assert.deepEqual(windowBackingOptions(glass(60, "header"), THEMED_BACKGROUND), {});
    assert.deepEqual(windowBackingOptions(glass(0, "header"), THEMED_BACKGROUND), {
      backgroundColor: THEMED_BACKGROUND,
    });
  });

  it("makes macOS WebContents transparent over pinned vibrancy", () => {
    const on = windowSurfaceOptions(glass(60, "popover"), {
      platform: "darwin",
      glassSupported: true,
    });
    assert.equal(on.transparent, true);
    assert.equal(on.vibrancy, "popover");
    assert.equal(on.visualEffectState, "active");
    assert.equal(on.backgroundColor, undefined);

    const off = windowSurfaceOptions(glass(0, "popover"), {
      platform: "darwin",
      glassSupported: true,
    });
    assert.equal(off.transparent, true);
    assert.equal(off.vibrancy, "popover");
    assert.equal(off.visualEffectState, "active");
    assert.equal(off.backgroundColor, undefined);
  });

  it("marks glass-capable Windows windows transparent for DWM", () => {
    const on = windowSurfaceOptions(glass(60, "under-window"), {
      platform: "win32",
      glassSupported: true,
    });
    assert.equal(on.transparent, true);
    assert.equal(on.backgroundMaterial, "acrylic");

    const oldWin = windowSurfaceOptions(glass(60, "under-window"), {
      platform: "win32",
      glassSupported: false,
    });
    assert.equal(oldWin.transparent, undefined);
    assert.equal(oldWin.backgroundMaterial, undefined);
  });
});

describe("translucencyDiff", () => {
  it("ignores tint-only glass drags so setVibrancy is not reissued", () => {
    const changed = translucencyDiff(glass(20, "header"), glass(80, "header"));
    assert.deepEqual(changed, { backing: false, material: false, opacity: false });
  });

  it("flags a frost hop and a glass on/off flip", () => {
    assert.equal(translucencyDiff(glass(60, "header"), glass(60, "popover")).material, true);
    assert.equal(translucencyDiff(glass(0, "header"), glass(60, "header")).backing, true);
    assert.equal(translucencyDiff(glass(0, "header"), glass(60, "header")).material, true);
  });
});

describe("applyWindowTranslucency", () => {
  it("does not restamp a transparent backgroundColor on macOS", () => {
    const calls = [];
    const win = {
      setVibrancy: (material) => calls.push(["vibrancy", material]),
      setBackgroundColor: (color) => calls.push(["bg", color]),
      setOpacity: (n) => calls.push(["opacity", n]),
    };
    applyWindowTranslucency(win, glass(60, "popover"), { backing: true, material: true }, {
      platform: "darwin",
      glassSupported: true,
    });
    assert.deepEqual(calls, [["vibrancy", "popover"]]);
  });

  it("sets vibrancy only on a material change", () => {
    const calls = [];
    const win = {
      setVibrancy: (material) => calls.push(["vibrancy", material]),
      setBackgroundColor: (color) => calls.push(["bg", color]),
      setOpacity: (n) => calls.push(["opacity", n]),
    };
    applyWindowTranslucency(win, glass(60, "popover"), { material: true }, {
      platform: "darwin",
      glassSupported: true,
    });
    assert.deepEqual(calls, [["vibrancy", "popover"]]);

    calls.length = 0;
    applyWindowTranslucency(win, glass(0, "popover"), { material: true }, {
      platform: "darwin",
      glassSupported: true,
    });
    assert.deepEqual(calls, [["vibrancy", "popover"]]);
  });
});

describe("createTranslucencyController", () => {
  it("persists a set and reapplies on the next controller", () => {
    const homeDir = fs.mkdtempSync(path.join(os.tmpdir(), "marionette-glass-"));
    const first = createTranslucencyController({
      platform: "darwin",
      release: "25.4.0",
      homeDir,
    });
    assert.equal(first.getState().intensity, 0);
    first.setState({ intensity: ENABLE_INTENSITY, mode: "glass", material: "titlebar" });
    first.flush();

    const second = createTranslucencyController({
      platform: "darwin",
      release: "25.4.0",
      homeDir,
    });
    assert.equal(second.getState().intensity, ENABLE_INTENSITY);
    assert.equal(second.getState().material, "titlebar");
    assert.equal(second.surfaceOptions().vibrancy, "titlebar");
    const off = createTranslucencyController({
      platform: "darwin",
      release: "25.4.0",
      homeDir,
    });
    off.setState({ intensity: 0, mode: "glass", material: "titlebar" });
    assert.equal(off.surfaceOptions().vibrancy, "titlebar");
    assert.equal(off.surfaceOptions().backgroundColor, undefined);
  });

  it("reports Linux as unsupported", () => {
    const caps = capabilities("linux", "6.8.0");
    assert.equal(caps.translucencySupported, false);
    assert.equal(caps.glassSupported, false);
  });
});
