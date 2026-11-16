import { useSyncExternalStore } from "react";

/**
 * Voice dictation preferences. They belong to this window's user, not to the
 * backend, so they live in localStorage like the sound setting.
 */

export type VoiceEngine = "macos" | "whisper";

export type VoiceSettings = {
  engine: VoiceEngine;
  /** BCP 47 tag, for example en-US. */
  language: string;
  /** Send the message when a dictation ends with the word "send". */
  sendOnPhrase: boolean;
};

export const VOICE_SETTINGS_KEY = "pmharness.voice";

export const VOICE_ENGINES: readonly { value: VoiceEngine; label: string; help: string }[] = [
  {
    value: "macos",
    label: "On-device (macOS)",
    help: "Apple speech on this Mac. Live text while you talk. Needs macOS 26.",
  },
  {
    value: "whisper",
    label: "Whisper (local)",
    help: "whisper.cpp on this Mac. Most accurate, text updates about each second.",
  },
];

export const VOICE_LANGUAGES: readonly { value: string; label: string }[] = [
  { value: "en-US", label: "English (US)" },
  { value: "en-GB", label: "English (UK)" },
  { value: "de-DE", label: "German" },
  { value: "es-ES", label: "Spanish" },
  { value: "fr-FR", label: "French" },
  { value: "it-IT", label: "Italian" },
  { value: "ja-JP", label: "Japanese" },
  { value: "pt-BR", label: "Portuguese (Brazil)" },
  { value: "zh-CN", label: "Chinese (Simplified)" },
];

function defaultEngine(): VoiceEngine {
  const platform = typeof navigator === "undefined" ? "" : navigator.userAgent;
  return /Mac OS X|Macintosh/.test(platform) ? "macos" : "whisper";
}

export function defaultVoiceSettings(): VoiceSettings {
  return { engine: defaultEngine(), language: "en-US", sendOnPhrase: false };
}

export function parseVoiceSettings(raw: unknown): VoiceSettings {
  const base = defaultVoiceSettings();
  if (!raw || typeof raw !== "object") return base;
  const record = raw as Record<string, unknown>;
  const engine = VOICE_ENGINES.find((e) => e.value === record.engine)?.value ?? base.engine;
  const language = VOICE_LANGUAGES.find((l) => l.value === record.language)?.value ?? base.language;
  return { engine, language, sendOnPhrase: record.sendOnPhrase === true };
}

let cached: VoiceSettings | null = null;
const listeners = new Set<() => void>();

export function readVoiceSettings(): VoiceSettings {
  if (cached) return cached;
  try {
    const raw = localStorage.getItem(VOICE_SETTINGS_KEY);
    cached = parseVoiceSettings(raw ? JSON.parse(raw) : null);
  } catch {
    cached = defaultVoiceSettings();
  }
  return cached;
}

export function writeVoiceSettings(patch: Partial<VoiceSettings>): VoiceSettings {
  const next = parseVoiceSettings({ ...readVoiceSettings(), ...patch });
  cached = next;
  try {
    localStorage.setItem(VOICE_SETTINGS_KEY, JSON.stringify(next));
  } catch {
    // Private windows can refuse storage. The choice holds until reload.
  }
  for (const listener of listeners) listener();
  return next;
}

/** Test seam: forget the cached copy so the next read parses storage again. */
export function resetVoiceSettingsCache(): void {
  cached = null;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useVoiceSettings(): VoiceSettings {
  return useSyncExternalStore(subscribe, readVoiceSettings, readVoiceSettings);
}
