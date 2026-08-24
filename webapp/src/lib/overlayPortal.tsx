import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
  type Ref,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";

const DEFAULT_EXIT_MS = 220;

export const FEED_OVERLAY_PORTAL_ID = "feed-overlay-portal";
export const FEED_OVERLAY_PORTAL_TESTID = "feed-overlay-portal";

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return false;
  }
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function getFeedOverlayPortalHost(): HTMLElement | null {
  if (typeof document === "undefined") return null;
  return document.getElementById(FEED_OVERLAY_PORTAL_ID) ?? document.body;
}

export type OverlayEnterLeave = {
  mounted: boolean;
  dataStartingStyle: boolean;
  dataInstant: boolean;
  rootRef: RefObject<HTMLElement | null>;
  requestInstantLeave: () => void;
};

export type OverlayEnterLeaveOptions = {
  onExited?: () => void;
  exitMs?: number;
};

/**
 * Overlay enter/leave: data-starting-style on first paint, cleared after a
 * frame so CSS @starting-style / transitions can run; data-instant when
 * reduced motion or requestInstantLeave skips the leave transition.
 */
export function useOverlayEnterLeave(
  open: boolean,
  opts?: OverlayEnterLeaveOptions,
): OverlayEnterLeave {
  const [mounted, setMounted] = useState(open);
  const [startingStyle, setStartingStyle] = useState(open);
  const [instant, setInstant] = useState(false);
  const rootRef = useRef<HTMLElement | null>(null);
  const instantLeaveRef = useRef(false);
  const onExitedRef = useRef(opts?.onExited);
  onExitedRef.current = opts?.onExited;
  const exitMs = opts?.exitMs ?? DEFAULT_EXIT_MS;

  const requestInstantLeave = () => {
    instantLeaveRef.current = true;
  };

  useEffect(() => {
    if (open) {
      instantLeaveRef.current = false;
      setInstant(false);
      setMounted(true);
      setStartingStyle(true);
    }
  }, [open]);

  useLayoutEffect(() => {
    if (!mounted || !startingStyle || !open) return;
    const id = requestAnimationFrame(() => {
      setStartingStyle(false);
    });
    return () => cancelAnimationFrame(id);
  }, [mounted, startingStyle, open]);

  useEffect(() => {
    if (open || !mounted) return;

    const finish = () => {
      setMounted(false);
      onExitedRef.current?.();
    };

    if (instantLeaveRef.current || prefersReducedMotion()) {
      setInstant(true);
      setStartingStyle(false);
      finish();
      return;
    }

    setStartingStyle(true);
    const root = rootRef.current;
    if (!root) {
      const t = window.setTimeout(finish, exitMs);
      return () => window.clearTimeout(t);
    }

    const onEnd = (e: TransitionEvent) => {
      if (e.target !== root) return;
      finish();
    };
    root.addEventListener("transitionend", onEnd);
    const fallback = window.setTimeout(finish, exitMs);
    return () => {
      root.removeEventListener("transitionend", onEnd);
      window.clearTimeout(fallback);
    };
  }, [open, mounted, exitMs]);

  return {
    mounted,
    dataStartingStyle: startingStyle,
    dataInstant: instant,
    rootRef,
    requestInstantLeave,
  };
}

export function mergeOverlayRootRef<T extends HTMLElement>(
  overlay: Pick<OverlayEnterLeave, "rootRef">,
  localRef: RefObject<T | null>,
): Ref<T> {
  return (el: T | null) => {
    overlay.rootRef.current = el;
    localRef.current = el;
  };
}

export function overlayDataAttrs(
  state: Pick<OverlayEnterLeave, "dataStartingStyle" | "dataInstant">,
): Record<string, string> {
  return {
    ...(state.dataStartingStyle ? { "data-starting-style": "" } : {}),
    ...(state.dataInstant ? { "data-instant": "" } : {}),
  };
}

/** Sibling of the layoutScroll scrollport — not inside useVirtualizer. */
export function FeedOverlayHost() {
  return (
    <div
      id={FEED_OVERLAY_PORTAL_ID}
      data-testid="feed-overlay-portal"
      className="pointer-events-none absolute inset-0 z-20"
    />
  );
}


/** Resolve the feed overlay host after layout (avoids same-pass missing id). */
export function useOverlayPortalHost(): HTMLElement | null {
  const [host, setHost] = useState<HTMLElement | null>(
    () => (typeof document !== "undefined" ? document.body : null),
  );
  useLayoutEffect(() => {
    setHost(getFeedOverlayPortalHost());
  }, []);
  return host;
}

/** Portal children to the feed overlay host (body fallback). */
export function OverlayPortal({
  children,
  container,
}: {
  children: ReactNode;
  container?: HTMLElement | null;
}) {
  const resolved = container ?? getFeedOverlayPortalHost();
  if (!resolved) return null;
  return createPortal(children, resolved);
}
