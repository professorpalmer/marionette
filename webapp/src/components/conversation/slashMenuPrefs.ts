/**
 * Per-viewer slash menu preferences: expert mode and menu pick counts.
 * Browser storage only: a lost store means the default order, nothing worse.
 */
import { useSyncExternalStore } from "react";
import type { SlashMenuPrefs } from "./slashCommands";

const KEY = "marionette.slashMenu.v1";
const EMPTY: SlashMenuPrefs = { expert: false, uses: {} };

let snapshot: SlashMenuPrefs | null = null;
const listeners = new Set<() => void>();

function parse(raw: string | null): SlashMenuPrefs {
  if (!raw) return EMPTY;
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== "object") return EMPTY;
    const { expert, uses } = value as { expert?: unknown; uses?: unknown };
    const counts: Record<string, number> = {};
    if (uses && typeof uses === "object") {
      for (const [cmd, n] of Object.entries(uses)) {
        if (cmd.startsWith("/") && typeof n === "number" && Number.isFinite(n) && n > 0) counts[cmd] = Math.floor(n);
      }
    }
    return { expert: expert === true, uses: counts };
  } catch {
    return EMPTY;
  }
}

export function readSlashMenuPrefs(): SlashMenuPrefs {
  if (snapshot) return snapshot;
  try {
    snapshot = parse(window.localStorage.getItem(KEY));
  } catch {
    snapshot = EMPTY;
  }
  return snapshot;
}

function write(next: SlashMenuPrefs): void {
  snapshot = next;
  try {
    window.localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    // Storage blocked: keep the in-memory value for this window.
  }
  for (const listener of listeners) listener();
}

export function recordSlashCommandUse(cmd: string): void {
  const prefs = readSlashMenuPrefs();
  write({ ...prefs, uses: { ...prefs.uses, [cmd]: (prefs.uses[cmd] ?? 0) + 1 } });
}

export function setSlashExpertMode(expert: boolean): void {
  write({ ...readSlashMenuPrefs(), expert });
}

export function resetSlashMenuPrefsForTests(): void {
  snapshot = null;
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    // ignore
  }
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useSlashMenuPrefs(): SlashMenuPrefs {
  return useSyncExternalStore(subscribe, readSlashMenuPrefs, readSlashMenuPrefs);
}
