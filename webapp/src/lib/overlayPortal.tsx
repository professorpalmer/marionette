import {
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { useOverlayFocus } from "./overlayFocus";

type OverlayPhase = "closed" | "enter" | "open" | "leave";

export type OverlayPortalProps = {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
  className?: string;
  testId?: string;
  /** Focus-trap root; defaults to the backdrop element. */
  focusRootRef?: RefObject<HTMLElement | null>;
  initialFocusRef?: RefObject<HTMLElement | null>;
  restoreFocus?: boolean;
  onBackdropClick?: (e: React.MouseEvent<HTMLDivElement>) => void;
  onBackdropMouseDown?: (e: React.MouseEvent<HTMLDivElement>) => void;
  style?: CSSProperties;
};

/**
 * Body-portaled overlay shell with first-paint and instant-leave attributes.
 * Enter: data-starting-style for one frame so CSS can transition from a
 * stable starting state (no pop). Leave: data-instant for one frame so close
 * does not animate. No Motion — feed-only layout motion stays in the list.
 */
export function OverlayPortal({
  open,
  onClose,
  children,
  className,
  testId,
  focusRootRef,
  initialFocusRef,
  restoreFocus = true,
  onBackdropClick,
  onBackdropMouseDown,
  style,
}: OverlayPortalProps) {
  const backdropRef = useRef<HTMLDivElement>(null);
  const rootRef = focusRootRef ?? backdropRef;
  const [phase, setPhase] = useState<OverlayPhase>(open ? "enter" : "closed");

  useOverlayFocus(open, rootRef, {
    initialFocusRef,
    onClose,
    restoreFocus,
  });

  useLayoutEffect(() => {
    if (open) {
      setPhase("enter");
      const id = requestAnimationFrame(() => setPhase("open"));
      return () => cancelAnimationFrame(id);
    }
    setPhase((prev) => (prev === "closed" ? "closed" : "leave"));
  }, [open]);

  useLayoutEffect(() => {
    if (phase !== "leave") return;
    const id = requestAnimationFrame(() => setPhase("closed"));
    return () => cancelAnimationFrame(id);
  }, [phase]);

  if (phase === "closed") return null;

  return createPortal(
    <div
      ref={backdropRef}
      className={className}
      data-testid={testId}
      data-starting-style={phase === "enter" ? "" : undefined}
      data-instant={phase === "leave" ? "" : undefined}
      style={style}
      onClick={onBackdropClick}
      onMouseDown={onBackdropMouseDown}
    >
      {children}
    </div>,
    document.body,
  );
}
