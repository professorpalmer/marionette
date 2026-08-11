/**
 * Per-session composer draft cache (Cursor-style per-chat drafts).
 * Survives activeSessionId switches so mid-type text is restored on return.
 */

const composerDraftBySessionId = new Map<string, string>();

/** Test helper: drop all draft entries. */
export function clearComposerDraftCache() {
  composerDraftBySessionId.clear();
}

/** Read cached draft for a session (undefined on miss). */
export function peekComposerDraft(sessionId: string): string | undefined {
  return composerDraftBySessionId.get(sessionId);
}

/** Seed or overwrite the draft for a session. */
export function writeComposerDraft(sessionId: string, draft: string) {
  composerDraftBySessionId.set(sessionId, draft);
}

/**
 * Cache the outgoing session's draft and restore the incoming session's.
 * Empty / missing cache → empty composer (never leave A's text painted on B).
 */
export function resolveComposerDraftOnSwitch(opts: {
  prevId: string | null;
  nextId: string | null;
  currentDraft: string;
}): string {
  if (opts.prevId && opts.prevId !== opts.nextId) {
    writeComposerDraft(opts.prevId, opts.currentDraft);
  }
  if (!opts.nextId) return "";
  return peekComposerDraft(opts.nextId) ?? "";
}
