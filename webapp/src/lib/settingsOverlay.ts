/** Latch so Settings survives RightPane remounts and right-rail collapse. */

let settingsOverlayOpen = false;

export function isSettingsOverlayOpen(): boolean {
  return settingsOverlayOpen;
}

export function setSettingsOverlayOpen(open: boolean): void {
  settingsOverlayOpen = open;
}

export function resetSettingsOverlay(): void {
  settingsOverlayOpen = false;
}
