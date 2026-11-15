/**
 * Pure helpers for turn-complete notification / sound prefs.
 * Side effects (Notification / AudioContext) stay in Conversation.tsx.
 */

/** localStorage keys. Settings > Notifications writes them. */
export const NOTIFY_PREF_KEY = "pmharness.notify";
export const SOUND_PREF_KEY = "pmharness.sound";

export function notifyPrefEnabled(
  getItem: (key: string) => string | null = (k) => localStorage.getItem(k),
): boolean {
  const notifyPref = getItem(NOTIFY_PREF_KEY);
  return notifyPref !== null ? notifyPref === "true" : true;
}

export function soundPrefEnabled(
  getItem: (key: string) => string | null = (k) => localStorage.getItem(k),
): boolean {
  const soundPref = getItem(SOUND_PREF_KEY);
  return soundPref !== null ? soundPref === "true" : false;
}

/** Show a desktop notification only when the document is hidden / unfocused. */
export function shouldShowCompletionNotification(opts: {
  notifyEnabled: boolean;
  isHidden: boolean;
}): boolean {
  return opts.notifyEnabled && opts.isHidden;
}
