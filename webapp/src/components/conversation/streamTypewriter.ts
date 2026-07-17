/**
 * Typewriter pump helpers for streaming assistant deltas.
 * Conversation owns the rAF refs; this module owns the per-frame math.
 */

import { typewriterCharsPerFrame } from "./streamBubbles";

export type TypewriterRefs = {
  typeBufRef: { current: string };
  typeRafRef: { current: number | null };
  typeDoneRef: { current: boolean };
};

/** One animation frame: reveal backlog chars and schedule the next pump. */
export function pumpTypewriterFrame(
  refs: TypewriterRefs,
  appendStreamingText: (chunk: string) => void,
  schedule: (cb: () => void) => number,
): void {
  refs.typeRafRef.current = null;
  const buf = refs.typeBufRef.current;
  if (!buf) {
    if (!refs.typeDoneRef.current) {
      refs.typeRafRef.current = schedule(() =>
        pumpTypewriterFrame(refs, appendStreamingText, schedule),
      );
    }
    return;
  }
  const perFrame = typewriterCharsPerFrame(buf.length, refs.typeDoneRef.current);
  const take = buf.slice(0, perFrame);
  refs.typeBufRef.current = buf.slice(perFrame);
  appendStreamingText(take);
  if (refs.typeBufRef.current || !refs.typeDoneRef.current) {
    refs.typeRafRef.current = schedule(() =>
      pumpTypewriterFrame(refs, appendStreamingText, schedule),
    );
  }
}

export function startTypewriterLoop(
  refs: TypewriterRefs,
  appendStreamingText: (chunk: string) => void,
  schedule: (cb: () => void) => number,
): void {
  refs.typeDoneRef.current = false;
  if (refs.typeRafRef.current == null) {
    refs.typeRafRef.current = schedule(() =>
      pumpTypewriterFrame(refs, appendStreamingText, schedule),
    );
  }
}

export function flushTypewriterBuffer(
  refs: TypewriterRefs,
  appendStreamingText: (chunk: string) => void,
  cancel: (id: number) => void,
): void {
  refs.typeDoneRef.current = true;
  if (refs.typeBufRef.current) {
    appendStreamingText(refs.typeBufRef.current);
    refs.typeBufRef.current = "";
  }
  if (refs.typeRafRef.current != null) {
    cancel(refs.typeRafRef.current);
    refs.typeRafRef.current = null;
  }
}

/** Cancel the loop without flushing (session switch — hydrate owns the text). */
export function cancelTypewriterWithoutFlush(
  refs: TypewriterRefs,
  cancel: (id: number) => void,
): void {
  if (refs.typeRafRef.current != null) {
    cancel(refs.typeRafRef.current);
    refs.typeRafRef.current = null;
  }
  refs.typeBufRef.current = "";
  refs.typeDoneRef.current = false;
}
