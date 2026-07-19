/**
 * Owned blob: preview URLs for newly sent transcript images.
 *
 * Composer creates blob URLs for local preview; after send those URLs ride on
 * the user message until the durable /api/image load succeeds. Ownership is
 * module-scoped so a render-window hide / remount does NOT revoke early
 * (revoke-before-late-mount would permanently break the thumbnail).
 *
 * Release only on:
 *   - durable image load success, or
 *   - owning message/session definitively discarded (session switch / clear).
 */

const ownedBlobByPath = new Map<string, string>();
const ownedBlobUrls = new Set<string>();
/** Paths whose durable /api/image load already succeeded — never re-adopt blobs. */
const durableReadyPaths = new Set<string>();

export function isBlobPreviewUrl(url: string | undefined | null): boolean {
  return typeof url === "string" && url.startsWith("blob:");
}

export function isDurableTranscriptImageReady(durablePath: string): boolean {
  return durableReadyPaths.has(durablePath);
}

/** Register a composer blob as owned by a durable saved path. Idempotent. */
export function adoptTranscriptPreviewBlob(
  durablePath: string,
  previewUrl: string,
): void {
  if (!durablePath || !isBlobPreviewUrl(previewUrl)) return;
  // Parent message rows keep previewUrl after revoke; do not resurrect it.
  if (durableReadyPaths.has(durablePath)) return;
  const existing = ownedBlobByPath.get(durablePath);
  if (existing === previewUrl) {
    ownedBlobUrls.add(previewUrl);
    return;
  }
  if (existing) {
    ownedBlobByPath.delete(durablePath);
    ownedBlobUrls.delete(existing);
    try {
      URL.revokeObjectURL(existing);
    } catch {
      /* ignore */
    }
  }
  ownedBlobByPath.set(durablePath, previewUrl);
  ownedBlobUrls.add(previewUrl);
}

export function peekOwnedTranscriptPreviewBlob(
  durablePath: string,
): string | null {
  return ownedBlobByPath.get(durablePath) || null;
}

/** Revoke the owned blob for a path after durable success (or explicit discard). */
export function releaseTranscriptPreviewBlob(durablePath: string): void {
  if (durablePath) durableReadyPaths.add(durablePath);
  const url = ownedBlobByPath.get(durablePath);
  if (!url) return;
  ownedBlobByPath.delete(durablePath);
  ownedBlobUrls.delete(url);
  try {
    URL.revokeObjectURL(url);
  } catch {
    /* ignore */
  }
}

/** Session discard: drop every owned preview blob (warm cache uses durable paths). */
export function releaseAllTranscriptPreviewBlobs(): void {
  for (const url of ownedBlobUrls) {
    try {
      URL.revokeObjectURL(url);
    } catch {
      /* ignore */
    }
  }
  ownedBlobUrls.clear();
  ownedBlobByPath.clear();
  durableReadyPaths.clear();
}

export function ownedTranscriptPreviewBlobCount(): number {
  return ownedBlobUrls.size;
}

/** Test-only: clear registry without revoking (jsdom URL stubs vary). */
export function resetTranscriptPreviewBlobsForTests(): void {
  ownedBlobUrls.clear();
  ownedBlobByPath.clear();
  durableReadyPaths.clear();
}
