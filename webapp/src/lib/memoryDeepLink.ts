/**
 * Deep-link for Settings → Advanced → Agent Memory.
 * Latches when SettingsPane is not mounted yet (Cmd-K / /memory before overlay opens).
 */

let pendingExpandMemory = false;

/** Expand Agent Memory accordion; safe if SettingsPane mounts later in the same turn. */
export function expandAgentMemory(): void {
  pendingExpandMemory = true;
  window.dispatchEvent(new Event("harness-expand-memory"));
}

/** Consume latched expand request (SettingsPane initial state). */
export function takePendingExpandMemory(): boolean {
  const pending = pendingExpandMemory;
  pendingExpandMemory = false;
  return pending;
}
