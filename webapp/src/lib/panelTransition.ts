import { useEffect, useState } from "react";

/** Shared panel crossfade styling (Hermes-style easing). */
export const PANEL_TRANSITION =
  "transition-opacity duration-200 ease-[cubic-bezier(0.23,1,0.32,1)]";

export function panelOpacityClass(switching: boolean, stale = false): string {
  return `${PANEL_TRANSITION} ${switching || stale ? "opacity-60" : "opacity-100"}`;
}

export function dispatchProjectSwitching(switching: boolean): void {
  window.dispatchEvent(
    new CustomEvent("harness-project-switching", { detail: { switching } }),
  );
}

/** Subscribe to coordinated project-switch transitions from LeftRail. */
export function useProjectSwitching(): boolean {
  const [switching, setSwitching] = useState(false);
  useEffect(() => {
    const onSwitch = (e: Event) => {
      const detail = (e as CustomEvent<{ switching?: boolean }>).detail;
      if (typeof detail?.switching === "boolean") setSwitching(detail.switching);
    };
    window.addEventListener("harness-project-switching", onSwitch);
    return () => window.removeEventListener("harness-project-switching", onSwitch);
  }, []);
  return switching;
}
