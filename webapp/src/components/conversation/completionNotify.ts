/**
 * Pure helpers for turn-complete notification / sound prefs.
 * Side effects (Notification / AudioContext) stay in Conversation.tsx.
 */

export function notifyPrefEnabled(
  getItem: (key: string) => string | null = (k) => localStorage.getItem(k),
): boolean {
  const notifyPref = getItem("pmharness.notify");
  return notifyPref !== null ? notifyPref === "true" : true;
}

export function soundPrefEnabled(
  getItem: (key: string) => string | null = (k) => localStorage.getItem(k),
): boolean {
  const soundPref = getItem("pmharness.sound");
  return soundPref !== null ? soundPref === "true" : false;
}

export function queueMessagesPrefEnabled(
  getItem: (key: string) => string | null = (k) => localStorage.getItem(k),
): boolean {
  const queuePrefVal = getItem("pmharness.queueMessages");
  return queuePrefVal !== null ? queuePrefVal === "true" : true;
}

/** Show a desktop notification only when the document is hidden / unfocused. */
export function shouldShowCompletionNotification(opts: {
  notifyEnabled: boolean;
  isHidden: boolean;
}): boolean {
  return opts.notifyEnabled && opts.isHidden;
}
