/**
 * Deep-links for Settings → Advanced → Agent Memory and Skills & Rules.
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

let pendingExpandSkills = false;

/** Expand Skills & Rules; safe if SettingsPane mounts later in the same turn. */
export function expandSkills(): void {
  pendingExpandSkills = true;
  window.dispatchEvent(new Event("harness-expand-skills"));
}

/** Consume latched Skills expand request (SettingsPane initial state). */
export function takePendingExpandSkills(): boolean {
  const pending = pendingExpandSkills;
  pendingExpandSkills = false;
  return pending;
}
