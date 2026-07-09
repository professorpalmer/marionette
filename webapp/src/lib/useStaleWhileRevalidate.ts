import { useCallback, useEffect, useRef, useState } from "react";

type CacheEntry = { data: unknown; key: string };

/** Module-level cache so revisiting a project can show last-known data instantly. */
const cache = new Map<string, CacheEntry>();

/** Soft-persist selected SWR keys across remounts (panel close / soft reload). */
const PERSIST_PREFIX = "swr.persist.v1:";
const PERSIST_KEYS = new Set<string>();

function shouldPersist(key: string): boolean {
  return key.startsWith("swarm:");
}

function readPersisted<T>(key: string): T | undefined {
  if (!shouldPersist(key)) return undefined;
  try {
    const raw = sessionStorage.getItem(PERSIST_PREFIX + key);
    if (!raw) return undefined;
    return JSON.parse(raw) as T;
  } catch {
    return undefined;
  }
}

function writePersisted(key: string, data: unknown): void {
  if (!shouldPersist(key)) return;
  try {
    sessionStorage.setItem(PERSIST_PREFIX + key, JSON.stringify(data));
    PERSIST_KEYS.add(key);
  } catch {
    // sessionStorage full/unavailable -- in-memory cache still works.
  }
}

export interface UseSWRResult<T> {
  data: T | undefined;
  isValidating: boolean;
  /** Displayed data belongs to a previous cache key (kept visible while revalidating). */
  isShowingStale: boolean;
  /**
   * True only while the view genuinely lacks current data: first load for a key,
   * or a key change still showing the previous key's data. Background
   * revalidations of already-current data are NOT transitions -- use this (not
   * isValidating) to drive dim/fade effects, or every poll cycle flickers.
   */
  isTransitioning: boolean;
  error: unknown;
  revalidate: () => Promise<T | undefined>;
  mutate: (value: T | undefined) => void;
}

export function useStaleWhileRevalidate<T>(
  key: string | null | undefined,
  fetcher: (signal: AbortSignal) => Promise<T>,
  options: {
    enabled?: boolean;
    initialData?: T;
    onSuccess?: (data: T) => void;
  } = {},
): UseSWRResult<T> {
  const enabled = options.enabled ?? true;
  const keyStr = key ?? "";

  const [data, setData] = useState<T | undefined>(() => {
    if (options.initialData !== undefined) return options.initialData;
    const hit = keyStr ? cache.get(keyStr) : undefined;
    if (hit) return hit.data as T;
    return keyStr ? readPersisted<T>(keyStr) : undefined;
  });
  const [displayKey, setDisplayKey] = useState(keyStr);
  const [isValidating, setIsValidating] = useState(false);
  const [error, setError] = useState<unknown>(undefined);
  const hasEverLoadedKey = displayKey === keyStr && data !== undefined;

  const requestIdRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const onSuccessRef = useRef(options.onSuccess);
  onSuccessRef.current = options.onSuccess;

  const commit = useCallback((value: T, forKey: string) => {
    cache.set(forKey, { data: value, key: forKey });
    writePersisted(forKey, value);
    setData(value);
    setDisplayKey(forKey);
    setError(undefined);
  }, []);

  const mutate = useCallback(
    (value: T | undefined) => {
      setData(value);
      if (value !== undefined && keyStr) {
        cache.set(keyStr, { data: value, key: keyStr });
        writePersisted(keyStr, value);
        setDisplayKey(keyStr);
      }
    },
    [keyStr],
  );

  const revalidate = useCallback(async (): Promise<T | undefined> => {
    if (!keyStr || !enabled) return undefined;
    const reqId = ++requestIdRef.current;
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    setIsValidating(true);
    try {
      const result = await fetcherRef.current(ac.signal);
      if (reqId !== requestIdRef.current) return undefined;
      commit(result, keyStr);
      onSuccessRef.current?.(result);
      return result;
    } catch (err) {
      if (reqId !== requestIdRef.current) return undefined;
      if ((err as Error).name === "AbortError") return undefined;
      setError(err);
      return undefined;
    } finally {
      if (reqId === requestIdRef.current) setIsValidating(false);
    }
  }, [keyStr, enabled, commit]);

  useEffect(() => {
    if (!keyStr || !enabled) return;
    const hit = cache.get(keyStr);
    if (hit) {
      setData(hit.data as T);
      setDisplayKey(keyStr);
    } else {
      const persisted = readPersisted<T>(keyStr);
      if (persisted !== undefined) {
        cache.set(keyStr, { data: persisted, key: keyStr });
        setData(persisted);
        setDisplayKey(keyStr);
      }
    }
    void revalidate();
    return () => {
      abortRef.current?.abort();
    };
  }, [keyStr, enabled, revalidate]);

  const isShowingStale = !!data && displayKey !== keyStr && keyStr !== "";
  const isTransitioning = isValidating && !hasEverLoadedKey;

  return { data, isValidating, isShowingStale, isTransitioning, error, revalidate, mutate };
}

/** Read cached data for a key without subscribing (used for per-project session counts). */
export function readSWRCache<T>(key: string): T | undefined {
  const hit = cache.get(key);
  return hit ? (hit.data as T) : undefined;
}

/** Seed or overwrite the module cache (prefetch without a hook subscription). */
export function writeSWRCache<T>(key: string, data: T): void {
  cache.set(key, { data, key });
  writePersisted(key, data);
}

/** Test helper: drop all cached entries. */
export function clearSWRCache(): void {
  cache.clear();
  for (const key of [...PERSIST_KEYS]) {
    try {
      sessionStorage.removeItem(PERSIST_PREFIX + key);
    } catch {
      // ignore
    }
  }
  PERSIST_KEYS.clear();
}
