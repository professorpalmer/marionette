/**
 * Per-session composer attachment cache (parallel to composerDraftCache).
 * Survives activeSessionId switches so mid-compose thumbnails restore on return.
 */

export type ComposerAttachedImage = {
  path: string;
  name: string;
  previewUrl: string;
};

const composerAttachmentsBySessionId = new Map<string, ComposerAttachedImage[]>();

function copyAttachments(images: ComposerAttachedImage[]): ComposerAttachedImage[] {
  return images.map((img) => ({ ...img }));
}

/** Test helper: drop all attachment entries. */
export function clearComposerAttachmentCache() {
  composerAttachmentsBySessionId.clear();
}

/** Read cached attachments for a session (undefined on miss). */
export function peekComposerAttachments(
  sessionId: string,
): ComposerAttachedImage[] | undefined {
  const hit = composerAttachmentsBySessionId.get(sessionId);
  return hit ? copyAttachments(hit) : undefined;
}

/** Seed or overwrite attachments for a session. */
export function writeComposerAttachments(
  sessionId: string,
  images: ComposerAttachedImage[],
) {
  composerAttachmentsBySessionId.set(sessionId, copyAttachments(images));
}

/**
 * Cache the outgoing session's attachments and restore the incoming session's.
 * Empty / missing cache → [] (never leave A's thumbnails painted on B).
 */
export function resolveComposerAttachmentsOnSwitch(opts: {
  prevId: string | null;
  nextId: string | null;
  currentAttachments: ComposerAttachedImage[];
}): ComposerAttachedImage[] {
  if (opts.prevId && opts.prevId !== opts.nextId) {
    writeComposerAttachments(opts.prevId, opts.currentAttachments);
  }
  if (!opts.nextId) return [];
  return peekComposerAttachments(opts.nextId) ?? [];
}

/**
 * Revoke blob preview URLs that leave the composer without being retained
 * (e.g. uncached leftovers when prevId is null). Cached outgoing URLs must be
 * passed in `retained` so return visits still show thumbnails.
 */
export function releaseDroppedComposerAttachmentPreviews(
  previous: ComposerAttachedImage[],
  retained: ComposerAttachedImage[],
): void {
  const keep = new Set(
    retained.map((img) => img.previewUrl).filter((url) => !!url),
  );
  for (const img of previous) {
    const url = img.previewUrl;
    if (!url || keep.has(url) || !url.startsWith("blob:")) continue;
    try {
      URL.revokeObjectURL(url);
    } catch {
      /* ignore */
    }
  }
}
