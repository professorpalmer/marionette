import { useCallback, useEffect, useRef, useState } from "react";

type CacheEntry = { data: unknown; key: string };

/** Module-level cache so revisiting a project can show last-known data instantly. */
const cache = new Map<string, CacheEntry>();

export interface UseSWRResult<T> {
  data: T | undefined;
  isValidating: boolean;
  /** Displayed data belongs to a previous cache key (kept visible while revalidating). */
  isShowingStale: boolean;
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
    return hit ? (hit.data as T) : undefined;
  });
  const [displayKey, setDisplayKey] = useState(keyStr);
  const [isValidating, setIsValidating] = useState(false);
  const [error, setError] = useState<unknown>(undefined);

  const requestIdRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const onSuccessRef = useRef(options.onSuccess);
  onSuccessRef.current = options.onSuccess;

  const commit = useCallback((value: T, forKey: string) => {
    cache.set(forKey, { data: value, key: forKey });
    setData(value);
    setDisplayKey(forKey);
    setError(undefined);
  }, []);

  const mutate = useCallback(
    (value: T | undefined) => {
      setData(value);
      if (value !== undefined && keyStr) {
        cache.set(keyStr, { data: value, key: keyStr });
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
    }
    void revalidate();
    return () => {
      abortRef.current?.abort();
    };
  }, [keyStr, enabled, revalidate]);

  const isShowingStale = !!data && displayKey !== keyStr && keyStr !== "";

  return { data, isValidating, isShowingStale, error, revalidate, mutate };
}

/** Read cached data for a key without subscribing (used for per-project session counts). */
export function readSWRCache<T>(key: string): T | undefined {
  const hit = cache.get(key);
  return hit ? (hit.data as T) : undefined;
}
