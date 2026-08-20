import { useEffect, useState } from "react";
import {
  ENABLE_INTENSITY,
  FROST_LABELS,
  defaultTranslucencyState,
  hydrateWindowGlass,
  loadTranslucencyCapabilities,
  resetTranslucencyPeek,
  setWindowGlass,
  type GlassMaterial,
  type TranslucencyCapabilities,
  type TranslucencyState,
} from "../lib/windowGlass";

export default function WindowGlassSettings() {
  const [caps, setCaps] = useState<TranslucencyCapabilities | null>(null);
  const [state, setState] = useState<TranslucencyState>(() => defaultTranslucencyState());

  useEffect(() => {
    setState(hydrateWindowGlass());
    void loadTranslucencyCapabilities().then(setCaps);
    return () => resetTranslucencyPeek();
  }, []);

  if (!caps?.translucencySupported || !caps.glassSupported) return null;

  const on = state.mode === "glass" && state.intensity > 0;

  const commit = async (partial: Partial<TranslucencyState>) => {
    const next = await setWindowGlass(partial);
    setState(next);
  };

  return (
    <div className="space-y-1.5" data-testid="window-glass-settings">
      <label className="block uppercase tracking-wider text-[10px] text-faint font-semibold">
        Transparent background
      </label>
      <button
        type="button"
        onClick={() => {
          void commit({
            mode: "glass",
            intensity: on ? 0 : Math.max(state.intensity, ENABLE_INTENSITY),
          });
        }}
        className={`w-full flex items-center justify-between px-3 py-2 rounded border transition text-left ${
          on ? "bg-accent/10 border-accent/30 text-accent" : "bg-panel2 border-edge text-muted"
        }`}
      >
        <span className="font-medium text-[11px]">Glass window (Cursor-style frost)</span>
        <span className="text-[10px] uppercase font-bold tracking-wider">{on ? "on" : "off"}</span>
      </button>
      {on && (
        <>
          <div className="flex items-center gap-2 pt-1">
            <span className="text-[10px] text-muted w-10 shrink-0">Tint</span>
            <input
              type="range"
              min={1}
              max={100}
              value={state.intensity}
              aria-label="Glass tint"
              onChange={(e) => {
                void commit({ intensity: Number(e.target.value) });
              }}
              className="flex-1 accent-accent"
            />
            <span className="text-[10px] font-mono text-faint w-8 text-right">{state.intensity}</span>
          </div>
          <div className="flex flex-wrap gap-1 pt-0.5">
            {caps.materials.map((material) => {
              const selected = caps.isWindows && state.material === "header" ? "titlebar" : state.material;
              const active = selected === material;
              return (
                <button
                  key={material}
                  type="button"
                  onClick={() => {
                    void commit({ material: material as GlassMaterial });
                  }}
                  className={`px-2 py-1 rounded border text-[10px] uppercase tracking-wider font-semibold transition ${
                    active
                      ? "bg-accent/15 border-accent/40 text-accent"
                      : "bg-panel2 border-edge text-muted hover:text-txt"
                  }`}
                >
                  {FROST_LABELS[material]}
                </button>
              );
            })}
          </div>
        </>
      )}
      <p className="text-[10px] text-muted">
        Lets the desktop show through a matte blur. Raise Tint toward bare glass;
        Frost picks the native material (macOS vibrancy or Windows acrylic/mica).
      </p>
    </div>
  );
}
