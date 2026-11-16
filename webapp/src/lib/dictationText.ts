/**
 * Pure text rules for voice dictation: where the spoken text goes in the
 * message box, and the spoken "send" command.
 */

/** The text before and after the cursor when dictation started. */
export type DictationAnchor = { before: string; after: string };

export function anchorAt(value: string, selectionStart: number, selectionEnd: number): DictationAnchor {
  const start = Math.max(0, Math.min(selectionStart, value.length));
  const end = Math.max(start, Math.min(selectionEnd, value.length));
  return { before: value.slice(0, start), after: value.slice(end) };
}

/**
 * The message with the spoken text at the anchor. A space separates it from
 * words on either side; the caret goes after the spoken text.
 */
export function spliceDictation(anchor: DictationAnchor, spoken: string): { value: string; caret: number } {
  const text = spoken.trim();
  if (!text) return { value: anchor.before + anchor.after, caret: anchor.before.length };
  const lead = anchor.before && !/\s$/.test(anchor.before) ? " " : "";
  const tail = anchor.after && !/^[\s.,;:!?)]/.test(anchor.after) ? " " : "";
  const before = anchor.before + lead + text;
  return { value: before + tail + anchor.after, caret: before.length };
}

const SEND_PHRASE = /(?:^|[\s,.;:!?])(?:and\s+)?send(?:\s+it)?[\s.!?]*$/i;

/** When the spoken text ends with "send" (or "send it"), the text without it. */
export function stripSendPhrase(spoken: string): { text: string; send: boolean } {
  const match = SEND_PHRASE.exec(spoken);
  if (!match) return { text: spoken, send: false };
  const text = spoken.slice(0, match.index).replace(/[\s,;:]+$/, "");
  return { text, send: true };
}
