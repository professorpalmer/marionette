import { useEffect, useState } from "react";
import {
  ENABLE_INTENSITY,
  FROST_LABELS,
  defaultTranslucencyState,
  hydrateWindowGlass,
  loadTranslucencyCapabilities,
  resetTranslucencyPeek,
  setWindowGlass,
  type TranslucencyCapabilities,
  type TranslucencyState,
} from "../lib/windowGlass";
import { SettingsGroup, SettingsRow } from "./SettingsLayout";
import SettingsSwitch from "./SettingsSwitch";

/** General > Window. Shows only in the desktop app, where the window can be glass. */
export default function WindowGlassSettings() {
  const [caps, setCaps] = useState<TranslucencyCapabilities | null>(null);
  const [state, setState] = useState<TranslucencyState>(() => defaultTranslucencyState());

  useEffect(() => {
    setState(hydrateWindowGlass(setState));
    void loadTranslucencyCapabilities().then(setCaps);
    return () => resetTranslucencyPeek();
  }, []);

  if (!caps?.translucencySupported) return null;

  if (!caps.glassSupported) {
    const build = caps.windowsBuild && caps.windowsBuild > 0 ? ` This PC has build ${caps.windowsBuild}.` : "";
    return (
      <SettingsGroup title="Window" testId="window-glass-settings">
        <SettingsRow
          label="Glass window"
          description={`Glass needs Windows 11 22H2 or later.${build}`}
          state="Not available"
        />
      </SettingsGroup>
    );
  }

  const on = state.mode === "glass" && state.intensity > 0;
  const selected = caps.isWindows && state.material === "header" ? "titlebar" : state.material;

  const commit = async (partial: Partial<TranslucencyState>) => {
    setState(await setWindowGlass(partial));
  };

  return (
    <SettingsGroup title="Window" testId="window-glass-settings">
      <SettingsRow
        label="Glass window"
        description="The desktop shows through a matte blur."
        control={
          <SettingsSwitch
            on={on}
            label="Glass window"
            onChange={(next) => {
              void commit({ mode: "glass", intensity: next ? Math.max(state.intensity, ENABLE_INTENSITY) : 0 });
            }}
          />
        }
      />
      {on ? (
        <>
          <SettingsRow
            label="Tint"
            description="Higher values show more of the desktop."
            state={<span className="font-mono">{state.intensity}</span>}
            control={
              <input
                type="range"
                min={1}
                max={100}
                value={state.intensity}
                aria-label="Glass tint"
                onChange={(e) => {
                  void commit({ intensity: Number(e.target.value) });
                }}
                className="w-32 accent-accent"
              />
            }
          />
          <SettingsRow
            label="Frost"
            description="The native material: macOS vibrancy, or Windows acrylic and mica."
            control={
              <div className="flex gap-1" role="radiogroup" aria-label="Frost">
                {caps.materials.map((material) => (
                  <button
                    key={material}
                    type="button"
                    role="radio"
                    aria-checked={selected === material}
                    onClick={() => {
                      void commit({ material });
                    }}
                    className={`rounded px-2 py-0.5 text-ui-10 border transition-colors ${
                      selected === material
                        ? "bg-accent/15 border-accent/30 text-accent"
                        : "border-edge text-muted hover:text-txt"
                    }`}
                  >
                    {FROST_LABELS[material]}
                  </button>
                ))}
              </div>
            }
          />
        </>
      ) : null}
    </SettingsGroup>
  );
}
