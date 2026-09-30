import { useEffect, useState } from "react";

/** True once `on` has held for `delayMs`; false as soon as it drops. */
export function useDelayedFlag(on: boolean, delayMs: number): boolean {
  const [elapsed, setElapsed] = useState(false);
  useEffect(() => {
    if (!on) return;
    const timer = setTimeout(() => setElapsed(true), delayMs);
    return () => {
      clearTimeout(timer);
      setElapsed(false);
    };
  }, [on, delayMs]);
  return on && elapsed;
}
