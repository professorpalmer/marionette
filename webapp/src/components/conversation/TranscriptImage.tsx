import { useEffect, useRef, useState } from "react";
import { api } from "../../lib/api";
import {
  adoptTranscriptPreviewBlob,
  isBlobPreviewUrl,
  isDurableTranscriptImageReady,
  peekOwnedTranscriptPreviewBlob,
  releaseTranscriptPreviewBlob,
} from "./transcriptImageBlobs";
import {
  delayAfterDurableFailure,
  isDurableImagePath,
  MAX_DURABLE_IMAGE_RETRIES,
  shouldRetryDurableImage,
} from "./transcriptImageRetry";

export type TranscriptImageProps = {
  path: string;
  name: string;
  previewUrl?: string;
  onImageClick?: (url: string) => void;
};

function buildDurableSrc(path: string, retryGeneration: number): string {
  return api.imageUrl(path, { retry: retryGeneration });
}

function resolveBlobFallback(
  path: string,
  previewUrl: string | undefined,
): string | null {
  if (isDurableTranscriptImageReady(path)) return null;
  if (isBlobPreviewUrl(previewUrl)) return previewUrl!;
  return peekOwnedTranscriptPreviewBlob(path);
}

/**
 * Resilient sent-transcript thumbnail.
 *
 * Durable saved path is source of truth. On load error, retries with bounded
 * backoff, recomputing api.imageUrl from the current injected backend port and
 * a harmless cache-buster. Subscribes to harnessIPC.onBackendRespawned so a
 * stale-port URL refreshes immediately. Newly sent images may show an owned
 * blob previewUrl as a temporary fallback; that blob is revoked only after
 * durable success (or session discard elsewhere) — never on unmount alone.
 */
export function TranscriptImage({
  path,
  name,
  previewUrl,
  onImageClick,
}: TranscriptImageProps) {
  const hasDurable = isDurableImagePath(path);

  const [retryGeneration, setRetryGeneration] = useState(0);
  const [failureCount, setFailureCount] = useState(0);
  const [successfulSrc, setSuccessfulSrc] = useState<string | null>(null);
  const [durableLoaded, setDurableLoaded] = useState(false);
  // When true, <img> shows the owned blob while durable retries in parallel.
  const [showingBlobFallback, setShowingBlobFallback] = useState(() => {
    return hasDurable && !!resolveBlobFallback(path, previewUrl);
  });

  const retryTimerRef = useRef<number | null>(null);
  const mountedRef = useRef(true);
  const failureCountRef = useRef(0);
  const durableLoadedRef = useRef(false);
  const retryGenerationRef = useRef(0);

  const durableSrc = hasDurable
    ? buildDurableSrc(path, retryGeneration)
    : (previewUrl || "");
  const blobFallback = resolveBlobFallback(path, previewUrl);
  const displaySrc =
    hasDurable
      ? (showingBlobFallback && blobFallback ? blobFallback : durableSrc)
      : (previewUrl || "");

  function clearRetryTimer() {
    if (retryTimerRef.current != null) {
      window.clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
  }

  function markDurableSuccess(src: string) {
    if (durableLoadedRef.current) return;
    durableLoadedRef.current = true;
    setDurableLoaded(true);
    setShowingBlobFallback(false);
    setSuccessfulSrc(src);
    releaseTranscriptPreviewBlob(path);
    clearRetryTimer();
  }

  function scheduleDurableRetry() {
    if (durableLoadedRef.current) return;
    const blob = resolveBlobFallback(path, previewUrl);
    if (blob) setShowingBlobFallback(true);

    // Already at the cap — do not increment forever on repeated onError.
    if (failureCountRef.current >= MAX_DURABLE_IMAGE_RETRIES) return;

    const nextFailures = failureCountRef.current + 1;
    failureCountRef.current = nextFailures;
    setFailureCount(nextFailures);

    // Record the final failure without scheduling another attempt.
    if (!shouldRetryDurableImage({ durablePath: path, failureCount: nextFailures })) {
      return;
    }
    const delay = delayAfterDurableFailure(nextFailures);
    if (delay == null) return;
    clearRetryTimer();
    retryTimerRef.current = window.setTimeout(() => {
      retryTimerRef.current = null;
      if (!mountedRef.current || durableLoadedRef.current) return;
      retryGenerationRef.current += 1;
      setRetryGeneration(retryGenerationRef.current);
      // Leave blob fallback up until the new durable attempt succeeds.
    }, delay);
  }

  // Adopt composer blob so render-window hide / remount cannot revoke early.
  useEffect(() => {
    if (hasDurable && isBlobPreviewUrl(previewUrl)) {
      adoptTranscriptPreviewBlob(path, previewUrl!);
      setShowingBlobFallback(true);
    }
  }, [hasDurable, path, previewUrl]);

  // Parallel durable probe while blob is shown for immediate paint.
  useEffect(() => {
    if (!hasDurable || durableLoadedRef.current) return;
    if (!showingBlobFallback || !blobFallback) return;

    let cancelled = false;
    const gen = retryGeneration;
    const probeSrc = buildDurableSrc(path, gen);
    const probe = new Image();
    probe.onload = () => {
      if (cancelled || !mountedRef.current) return;
      if (retryGenerationRef.current !== gen) return;
      markDurableSuccess(probeSrc);
    };
    probe.onerror = () => {
      if (cancelled || !mountedRef.current) return;
      if (retryGenerationRef.current !== gen) return;
      scheduleDurableRetry();
    };
    probe.src = probeSrc;
    return () => {
      cancelled = true;
      probe.onload = null;
      probe.onerror = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasDurable, path, showingBlobFallback, blobFallback, retryGeneration]);

  // Backend respawn / port flip: recompute durable URL immediately.
  useEffect(() => {
    if (!hasDurable) return;
    const ipc: any =
      (typeof window !== "undefined" && (window as any).harnessIPC) || null;
    const unsub =
      typeof ipc?.onBackendRespawned === "function"
        ? ipc.onBackendRespawned(() => {
            if (durableLoadedRef.current) return;
            clearRetryTimer();
            retryGenerationRef.current += 1;
            setRetryGeneration(retryGenerationRef.current);
          })
        : null;
    return () => {
      try {
        unsub?.();
      } catch {
        /* ignore */
      }
    };
  }, [hasDurable, path]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      clearRetryTimer();
      // Do NOT revoke owned blobs on unmount — late remount must still work.
    };
  }, []);

  // Reset when the durable path identity changes.
  useEffect(() => {
    failureCountRef.current = 0;
    durableLoadedRef.current = false;
    retryGenerationRef.current = 0;
    setFailureCount(0);
    setRetryGeneration(0);
    setDurableLoaded(false);
    setSuccessfulSrc(null);
    setShowingBlobFallback(!!resolveBlobFallback(path, previewUrl));
  }, [path, previewUrl]);

  const clickSrc = successfulSrc || displaySrc;

  return (
    <div className="relative w-11 h-11 rounded overflow-hidden border border-edge bg-panel flex-shrink-0">
      <img
        src={displaySrc}
        alt={name}
        data-durable-src={hasDurable ? durableSrc : undefined}
        data-durable-loaded={durableLoaded ? "1" : "0"}
        data-failure-count={String(failureCount)}
        data-blob-fallback={showingBlobFallback && blobFallback ? "1" : "0"}
        onLoad={() => {
          if (!displaySrc) return;
          setSuccessfulSrc(displaySrc);
          if (hasDurable && displaySrc === durableSrc) {
            markDurableSuccess(displaySrc);
          }
        }}
        onError={() => {
          if (!hasDurable) return;
          // Visible durable attempt failed (no blob, or blob already abandoned).
          if (displaySrc === durableSrc) {
            scheduleDurableRetry();
          }
        }}
        onClick={() => onImageClick?.(clickSrc)}
        className="w-full h-full object-cover rounded cursor-pointer hover:opacity-85 transition-opacity"
      />
    </div>
  );
}
