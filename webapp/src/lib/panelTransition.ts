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

// Ephemeral mirror for panes that mount after the selection event. The backend
// workspace endpoint (persisted in workspace.json) is the sole durable owner.
// Do not put project roots in localStorage: a stale app-checkout value survived
// process restarts and made Files/State/Swarm/Checkpoints initialize against
// Marionette even while /api/workspace correctly restored the user's project.
let selectedProjectRoot = "";

/** Announce the selected project root and remember it for this renderer only. */
export function dispatchProjectSelected(root: string): void {
  selectedProjectRoot = root;
  window.dispatchEvent(new CustomEvent("harness-project-selected", { detail: root }));
}

/** Last selection in this renderer -- never restored across app launches. */
export function lastSelectedProjectRoot(): string {
  return selectedProjectRoot;
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
