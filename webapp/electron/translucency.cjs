/**
 * Window glass — one mapping the main process and tests share.
 *
 * Lifted from Hermes Agent's translucency module (NousResearch/hermes-agent,
 * apps/shared/src/translucency.ts + electron/translucency.ts) and rewritten
 * for Marionette: no HUD, no Clear-mode fade UI. Glass is the Cursor-style
 * path — native vibrancy / DWM material under a thinned page, text stays
 * full contrast.
 *
 * One lever, 0–100 (0 = off). Main persists ~/.pmharness/translucency.json so
 * a cold launch can apply the backing at BrowserWindow construction. Runtime
 * setBackgroundColor('#00000000') is silently lost on a fresh compositor
 * (measured by Hermes on macOS 26 / Electron 40), so creation must not rely
 * on a post-create fixup.
 */

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const GLASS_MATERIALS = ["under-window", "popover", "titlebar", "header"];
const DEFAULT_GLASS_MATERIAL = "under-window";
const WINDOWS_BACKGROUND_MATERIALS = ["acrylic", "tabbed", "mica", "none"];
const WINDOWS_MATERIAL_BY_FROST = {
  "under-window": "acrylic",
  popover: "tabbed",
  titlebar: "mica",
  header: "mica",
};
const WINDOWS_GLASS_MATERIALS = GLASS_MATERIALS.filter(
  (material, index) =>
    GLASS_MATERIALS.findIndex(
      (rung) => WINDOWS_MATERIAL_BY_FROST[rung] === WINDOWS_MATERIAL_BY_FROST[material],
    ) === index,
);
const WINDOWS_GLASS_MIN_BUILD = 22621;
const TRANSLUCENCY_MIN = 0;
const TRANSLUCENCY_MAX = 100;
const TRANSLUCENCY_OPACITY_FLOOR = 0.3;
const TRANSLUCENCY_CURVE = 2;
const THEMED_BACKGROUND = "#0f1113";
const ENABLE_INTENSITY = 60;

function clampIntensity(value) {
  const n = Math.round(Number(value));
  return Number.isFinite(n)
    ? Math.min(TRANSLUCENCY_MAX, Math.max(TRANSLUCENCY_MIN, n))
    : TRANSLUCENCY_MIN;
}

function translucencySupportedOn(platform) {
  return platform === "darwin" || platform === "win32";
}

function glassSupportedOn(platform, release) {
  if (platform === "darwin") return true;
  if (platform !== "win32") return false;
  const build = Number.parseInt(String(release || "").split(".")[2] || "", 10);
  return Number.isFinite(build) && build >= WINDOWS_GLASS_MIN_BUILD;
}

function normalizeMode(value, glassSupported, legacyIntensity) {
  if (!glassSupported) return "clear";
  if (value === "glass" || value === "clear") return value;
  return (legacyIntensity || 0) > 0 ? "clear" : "glass";
}

function normalizeMaterial(value) {
  return GLASS_MATERIALS.includes(value) ? value : DEFAULT_GLASS_MATERIAL;
}

function normalizeState(payload, glassSupported) {
  const record = payload && typeof payload === "object" ? payload : {};
  const intensity = clampIntensity(record.intensity);
  return {
    intensity,
    fade: clampIntensity(record.fade),
    mode: normalizeMode(record.mode, glassSupported, intensity),
    material: normalizeMaterial(record.material),
  };
}

function opacityRamp(lever) {
  const ratio = clampIntensity(lever) / TRANSLUCENCY_MAX;
  return 1 - (1 - TRANSLUCENCY_OPACITY_FLOOR) * Math.pow(ratio, TRANSLUCENCY_CURVE);
}

function windowOpacityFor(state) {
  return opacityRamp(state.mode === "glass" ? state.fade : state.intensity);
}

function glassActive(state) {
  return state.mode === "glass" && state.intensity > 0;
}

function glassSurfaceKeep(intensity) {
  return TRANSLUCENCY_MAX - clampIntensity(intensity);
}

function vibrancyFor(state) {
  return glassActive(state) ? state.material : null;
}

function backgroundMaterialFor(state) {
  return glassActive(state) ? WINDOWS_MATERIAL_BY_FROST[state.material] : "none";
}

function glassMaterialsFor(isWindows) {
  return isWindows ? WINDOWS_GLASS_MATERIALS : GLASS_MATERIALS;
}

function glassMaterialForPicker(material, isWindows) {
  if (!isWindows) return normalizeMaterial(material);
  return (
    WINDOWS_GLASS_MATERIALS.find(
      (rung) => WINDOWS_MATERIAL_BY_FROST[rung] === WINDOWS_MATERIAL_BY_FROST[material],
    ) || DEFAULT_GLASS_MATERIAL
  );
}

function windowBackingOptions(state, themedColor) {
  return glassActive(state) ? {} : { backgroundColor: themedColor || THEMED_BACKGROUND };
}

function translucencyDiff(previous, next) {
  return {
    backing: glassActive(previous) !== glassActive(next),
    material:
      vibrancyFor(previous) !== vibrancyFor(next) ||
      backgroundMaterialFor(previous) !== backgroundMaterialFor(next),
    opacity: windowOpacityFor(previous) !== windowOpacityFor(next),
  };
}

function capabilities(platform, release) {
  const glassSupported = glassSupportedOn(platform, release);
  return {
    translucencySupported: translucencySupportedOn(platform),
    glassSupported,
    isWindows: platform === "win32",
    materials: glassMaterialsFor(platform === "win32" && glassSupported),
  };
}

function configPath(homeDir) {
  return path.join(homeDir || os.homedir(), ".pmharness", "translucency.json");
}

function readPersistedTranslucency(opts) {
  const glassSupported = !!(opts && opts.glassSupported);
  try {
    return normalizeState(
      JSON.parse(fs.readFileSync(configPath(opts && opts.homeDir), "utf8")),
      glassSupported,
    );
  } catch {
    return normalizeState(null, glassSupported);
  }
}

function writePersistedTranslucency(state, opts) {
  const dest = configPath(opts && opts.homeDir);
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.writeFileSync(dest, JSON.stringify(state, null, 2), "utf8");
}

function windowSurfaceOptions(state, opts) {
  const platform = (opts && opts.platform) || process.platform;
  const glassSupported = !!(opts && opts.glassSupported);
  const themed = (opts && opts.themedColor) || THEMED_BACKGROUND;
  const isMac = platform === "darwin";
  const isWindows = platform === "win32";
  const options = {
    opacity: windowOpacityFor(state),
    ...windowBackingOptions(state, themed),
  };
  if (isMac) {
    const material = vibrancyFor(state);
    if (material) {
      options.vibrancy = material;
      options.visualEffectState = "active";
    }
  }
  if (isWindows && glassSupported) {
    // Win11 DWM materials only reach the client area on a transparent window
    // (electron#49443). Born transparent so a live toggle does not recreate.
    options.transparent = true;
    options.backgroundMaterial = backgroundMaterialFor(state);
  }
  return options;
}

function applyWindowTranslucency(win, state, changed, opts) {
  if (!win || (typeof win.isDestroyed === "function" && win.isDestroyed())) return;
  const platform = (opts && opts.platform) || process.platform;
  const glassSupported = !!(opts && opts.glassSupported);
  const themed = (opts && opts.themedColor) || THEMED_BACKGROUND;
  const patch = changed || { backing: true, material: true, opacity: true };
  if (patch.backing && typeof win.setBackgroundColor === "function") {
    win.setBackgroundColor(glassActive(state) ? "#00000000" : themed);
  }
  if (patch.material) {
    if (platform === "darwin" && typeof win.setVibrancy === "function") {
      const material = vibrancyFor(state);
      if (material) win.setVibrancy(material, { animationDuration: 150 });
      else win.setVibrancy(null);
    }
    if (
      platform === "win32" &&
      glassSupported &&
      typeof win.setBackgroundMaterial === "function"
    ) {
      win.setBackgroundMaterial(backgroundMaterialFor(state));
    }
  }
  if (patch.opacity && typeof win.setOpacity === "function") {
    win.setOpacity(windowOpacityFor(state));
  }
}

function createTranslucencyController(opts) {
  const platform = (opts && opts.platform) || process.platform;
  const release = (opts && opts.release) || os.release();
  const homeDir = opts && opts.homeDir;
  const getWindow = opts && opts.getWindow;
  const log = (opts && opts.log) || (() => {});
  const caps = capabilities(platform, release);
  let state = readPersistedTranslucency({
    homeDir,
    glassSupported: caps.glassSupported,
  });
  let persistTimer = null;

  const persistSoon = () => {
    if (persistTimer) clearTimeout(persistTimer);
    persistTimer = setTimeout(() => {
      persistTimer = null;
      try {
        writePersistedTranslucency(state, { homeDir });
      } catch (err) {
        log(`[translucency] write failed: ${err && err.message ? err.message : err}`);
      }
    }, 120);
  };

  return {
    capabilities: () => caps,
    getState: () => state,
    surfaceOptions: () =>
      windowSurfaceOptions(state, {
        platform,
        glassSupported: caps.glassSupported,
        themedColor: THEMED_BACKGROUND,
      }),
    applyTo: (win, changed) => {
      try {
        applyWindowTranslucency(win, state, changed, {
          platform,
          glassSupported: caps.glassSupported,
          themedColor: THEMED_BACKGROUND,
        });
      } catch (err) {
        log(`[translucency] apply failed: ${err && err.message ? err.message : err}`);
      }
    },
    setState: (payload) => {
      const next = normalizeState(payload, caps.glassSupported);
      const changed = translucencyDiff(state, next);
      state = next;
      const win = typeof getWindow === "function" ? getWindow() : null;
      if (win && (changed.backing || changed.material || changed.opacity)) {
        try {
          applyWindowTranslucency(win, state, changed, {
            platform,
            glassSupported: caps.glassSupported,
            themedColor: THEMED_BACKGROUND,
          });
        } catch (err) {
          log(`[translucency] apply failed: ${err && err.message ? err.message : err}`);
        }
      }
      persistSoon();
      return state;
    },
    flush: () => {
      if (!persistTimer) return;
      clearTimeout(persistTimer);
      persistTimer = null;
      try {
        writePersistedTranslucency(state, { homeDir });
      } catch (err) {
        log(`[translucency] write failed: ${err && err.message ? err.message : err}`);
      }
    },
  };
}

module.exports = {
  DEFAULT_GLASS_MATERIAL,
  ENABLE_INTENSITY,
  GLASS_MATERIALS,
  THEMED_BACKGROUND,
  TRANSLUCENCY_MAX,
  TRANSLUCENCY_MIN,
  TRANSLUCENCY_OPACITY_FLOOR,
  WINDOWS_BACKGROUND_MATERIALS,
  WINDOWS_GLASS_MIN_BUILD,
  applyWindowTranslucency,
  backgroundMaterialFor,
  capabilities,
  clampIntensity,
  configPath,
  createTranslucencyController,
  glassActive,
  glassMaterialForPicker,
  glassMaterialsFor,
  glassSurfaceKeep,
  glassSupportedOn,
  normalizeMaterial,
  normalizeMode,
  normalizeState,
  readPersistedTranslucency,
  translucencyDiff,
  translucencySupportedOn,
  vibrancyFor,
  windowBackingOptions,
  windowOpacityFor,
  windowSurfaceOptions,
  writePersistedTranslucency,
};
