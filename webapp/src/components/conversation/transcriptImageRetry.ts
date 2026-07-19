/**
 * Pure helpers for resilient transcript-image durable URL retries.
 * Durable saved path remains source of truth; blob/external sources are not
 * retried indefinitely.
 */

export const MAX_DURABLE_IMAGE_RETRIES = 5;

/** Bounded backoff between durable /api/image retries (ms). */
export const DURABLE_IMAGE_RETRY_BACKOFF_MS = [200, 500, 1000, 2000, 4000] as const;

export function isExternalImageSource(src: string | undefined | null): boolean {
  if (!src) return false;
  return /^https?:\/\//i.test(src) && !/\/api\/image(?:\?|$)/i.test(src);
}

/** True when `path` can be rebuilt via api.imageUrl for retry. */
export function isDurableImagePath(path: string | undefined | null): boolean {
  if (!path || typeof path !== "string") return false;
  if (path.startsWith("blob:")) return false;
  if (isExternalImageSource(path)) return false;
  return true;
}

/** Delay after the Nth failure (1-based). Null when retries are exhausted. */
export function delayAfterDurableFailure(failureCount: number): number | null {
  if (failureCount < 1 || failureCount > MAX_DURABLE_IMAGE_RETRIES) return null;
  const idx = Math.min(failureCount - 1, DURABLE_IMAGE_RETRY_BACKOFF_MS.length - 1);
  return DURABLE_IMAGE_RETRY_BACKOFF_MS[idx];
}

export function shouldRetryDurableImage(opts: {
  durablePath: string | undefined | null;
  failureCount: number;
}): boolean {
  if (!isDurableImagePath(opts.durablePath)) return false;
  return opts.failureCount < MAX_DURABLE_IMAGE_RETRIES;
}
