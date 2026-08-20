/**
 * Renderer half of window glass. Main owns the native material; this module
 * thins field surfaces and mirrors the lever over IPC / localStorage so a
 * reload paints before React.
 */

export type GlassMaterial = "under-window" | "popover" | "titlebar" | "header";

export type TranslucencyState = {
  intensity: number;
  fade: number;
  mode: "clear" | "glass";
  material: GlassMaterial;
};

export type TranslucencyCapabilities = {
  translucencySupported: boolean;
  glassSupported: boolean;
  isWindows: boolean;
  materials: GlassMaterial[];
};

export const TRANSLUCENCY_STORAGE_KEY = "pmharness.translucency.v1";
export const ENABLE_INTENSITY = 60;
export const FROST_LABELS: Record<GlassMaterial, string> = {
  "under-window": "Deep",
  popover: "Soft",
  titlebar: "Bright",
  header: "Glare",
};

const PEEK_ATTR = "data-marionette-glass-peek";
const GLASS_ATTR = "data-marionette-glass";

function clampIntensity(value: unknown): number {
  const n = Math.round(Number(value));
  return Number.isFinite(n) ? Math.min(100, Math.max(0, n)) : 0;
}

export function glassSurfaceKeep(intensity: number): number {
  return 100 - clampIntensity(intensity);
}

export function glassActive(state: TranslucencyState): boolean {
  return state.mode === "glass" && state.intensity > 0;
}

export function defaultTranslucencyState(): TranslucencyState {
  return { intensity: 0, fade: 0, mode: "glass", material: "under-window" };
}

export function normalizeRendererState(payload: unknown): TranslucencyState {
  const record = payload && typeof payload === "object" ? (payload as Record<string, unknown>) : {};
  const material = record.material;
  return {
    intensity: clampIntensity(record.intensity),
    fade: clampIntensity(record.fade),
    mode: record.mode === "clear" ? "clear" : "glass",
    material:
      material === "popover" || material === "titlebar" || material === "header"
        ? material
        : "under-window",
  };
}

function translucencyIpc(): {
  get?: () => Promise<{ state: TranslucencyState; capabilities: TranslucencyCapabilities }>;
  set?: (state: TranslucencyState) => Promise<TranslucencyState>;
  capabilities?: () => Promise<TranslucencyCapabilities>;
} | null {
  if (typeof window === "undefined") return null;
  return (window as { harnessIPC?: { translucency?: ReturnType<typeof translucencyIpc> } }).harnessIPC
    ?.translucency || null;
}

export function readStoredTranslucency(): TranslucencyState {
  try {
    const raw = localStorage.getItem(TRANSLUCENCY_STORAGE_KEY);
    return normalizeRendererState(raw ? JSON.parse(raw) : null);
  } catch {
    return defaultTranslucencyState();
  }
}

export function writeStoredTranslucency(state: TranslucencyState): void {
  try {
    localStorage.setItem(TRANSLUCENCY_STORAGE_KEY, JSON.stringify(state));
  } catch {
    /* storage pressure must not break Settings */
  }
}

export function applyGlassSurfaces(
  state: TranslucencyState,
  root: HTMLElement | null = typeof document === "undefined" ? null : document.documentElement,
): void {
  if (!root) return;
  const live = glassActive(state);
  root.toggleAttribute(GLASS_ATTR, live);
  if (live) {
    root.style.setProperty("--translucency-glass-keep", `${glassSurfaceKeep(state.intensity)}%`);
    root.style.backgroundColor = "transparent";
  } else {
    root.style.removeProperty("--translucency-glass-keep");
    root.style.removeProperty("background-color");
  }
}

let peekCount = 0;

export function beginTranslucencyPeek(
  root: HTMLElement | null = typeof document === "undefined" ? null : document.documentElement,
): void {
  peekCount += 1;
  if (root && peekCount > 0) root.toggleAttribute(PEEK_ATTR, true);
}

export function endTranslucencyPeek(
  root: HTMLElement | null = typeof document === "undefined" ? null : document.documentElement,
): void {
  peekCount = Math.max(0, peekCount - 1);
  if (root) root.toggleAttribute(PEEK_ATTR, peekCount > 0);
}

export function resetTranslucencyPeek(
  root: HTMLElement | null = typeof document === "undefined" ? null : document.documentElement,
): void {
  peekCount = 0;
  if (root) root.removeAttribute(PEEK_ATTR);
}

export function pulseTranslucencyPeek(ms = 900): void {
  beginTranslucencyPeek();
  if (typeof window === "undefined") {
    endTranslucencyPeek();
    return;
  }
  window.setTimeout(endTranslucencyPeek, ms);
}

export function hydrateWindowGlass(): TranslucencyState {
  const stored = readStoredTranslucency();
  applyGlassSurfaces(stored);
  const ipc = translucencyIpc();
  if (ipc?.get) {
    void ipc.get().then((payload) => {
      const next = normalizeRendererState(payload?.state);
      writeStoredTranslucency(next);
      applyGlassSurfaces(next);
    }).catch(() => {});
  }
  return stored;
}

export async function setWindowGlass(partial: Partial<TranslucencyState>): Promise<TranslucencyState> {
  const next = normalizeRendererState({ ...readStoredTranslucency(), ...partial });
  writeStoredTranslucency(next);
  applyGlassSurfaces(next);
  const ipc = translucencyIpc();
  if (ipc?.set) {
    try {
      const applied = normalizeRendererState(await ipc.set(next));
      writeStoredTranslucency(applied);
      applyGlassSurfaces(applied);
      return applied;
    } catch {
      return next;
    }
  }
  return next;
}

export async function loadTranslucencyCapabilities(): Promise<TranslucencyCapabilities> {
  const ipc = translucencyIpc();
  if (ipc?.capabilities) {
    try {
      return await ipc.capabilities();
    } catch {
      /* fall through */
    }
  }
  if (ipc?.get) {
    try {
      const payload = await ipc.get();
      if (payload?.capabilities) return payload.capabilities;
    } catch {
      /* fall through */
    }
  }
  return {
    translucencySupported: false,
    glassSupported: false,
    isWindows: false,
    materials: ["under-window", "popover", "titlebar", "header"],
  };
}
