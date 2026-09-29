import { useEffect, useRef } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

const overlayStack: number[] = [];
let overlaySeq = 0;

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
  const onCloseRef = useRef(opts?.onClose);
  useEffect(() => { onCloseRef.current = opts?.onClose; }, [opts?.onClose]);
  const triggerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const id = ++overlaySeq;
    overlayStack.push(id);
    const isTop = () => overlayStack[overlayStack.length - 1] === id;

    triggerRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;

    const root = rootRef.current;
    const initial = opts?.initialFocusRef?.current;
    const t = window.setTimeout(() => {
      if (!isTop()) return;
      if (initial) {
        initial.focus();
      } else if (root) {
        const nodes = focusableElements(root);
        nodes[0]?.focus();
      }
    }, 0);

    const onKeyDown = (e: KeyboardEvent) => {
      if (!isTop()) return;
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onCloseRef.current?.();
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
      const idx = overlayStack.lastIndexOf(id);
      if (idx >= 0) overlayStack.splice(idx, 1);
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
  }, [open, opts?.initialFocusRef, opts?.restoreFocus, rootRef]);
}

/** Arrow/Home/End roving focus across a menu's enabled items. True when handled. */
export function moveMenuFocus(root: HTMLElement | null, key: string): boolean {
  if (!root) return false;
  const items = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE))
    .filter((el) => !el.hasAttribute("disabled") && el.tabIndex !== -1);
  const n = items.length;
  if (!n) return false;
  const at = items.indexOf(document.activeElement as HTMLElement);
  const next = key === "ArrowDown" ? (at + 1) % n
    : key === "ArrowUp" ? (at - 1 + n) % n
      : key === "Home" ? 0
        : key === "End" ? n - 1
          : -1;
  if (next < 0) return false;
  items[next].focus();
  return true;
}
