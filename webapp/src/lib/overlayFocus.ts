import { useEffect, useRef } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusableElements(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => !el.hasAttribute("disabled") && el.tabIndex !== -1 && el.offsetParent !== null,
  );
}

/** Trap Tab within ``root`` and restore focus to ``trigger`` on close. */
export function useOverlayFocus(
  open: boolean,
  rootRef: React.RefObject<HTMLElement | null>,
  opts?: {
    initialFocusRef?: React.RefObject<HTMLElement | null>;
    onClose?: () => void;
    restoreFocus?: boolean;
  },
) {
  const triggerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    triggerRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;

    const root = rootRef.current;
    const initial = opts?.initialFocusRef?.current;
    const t = window.setTimeout(() => {
      if (initial) {
        initial.focus();
      } else if (root) {
        const nodes = focusableElements(root);
        nodes[0]?.focus();
      }
    }, 0);

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        opts?.onClose?.();
        return;
      }
      if (e.key !== "Tab" || !root) return;
      const nodes = focusableElements(root);
      if (nodes.length === 0) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      const active = document.activeElement;
      if (e.shiftKey) {
        if (active === first || !root.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else if (active === last) {
        e.preventDefault();
        first.focus();
      }
    };

    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      window.clearTimeout(t);
      window.removeEventListener("keydown", onKeyDown, true);
      if (opts?.restoreFocus !== false && triggerRef.current) {
        try {
          triggerRef.current.focus();
        } catch {
          /* ignore */
        }
      }
    };
  }, [open, opts?.initialFocusRef, opts?.onClose, opts?.restoreFocus, rootRef]);
}
