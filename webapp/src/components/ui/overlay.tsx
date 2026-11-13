/**
 * The one overlay stack. Base UI owns focus, Escape, outside press, and focus
 * return; call sites only paint with tokens. Do not add document listeners for
 * these behaviors anywhere else.
 */
import type { ComponentProps, KeyboardEvent, ReactNode, RefObject } from "react";
import { Dialog } from "@base-ui/react/dialog";
import { Menu } from "@base-ui/react/menu";

type FocusTarget = RefObject<HTMLElement | null> | boolean;
/** A ref, a boolean, or a function of the close type (Base UI `finalFocus`). */
type FinalFocus = ComponentProps<typeof Dialog.Popup>["finalFocus"];

export type ModalProps = {
  open: boolean;
  onClose: () => void;
  /** Accessible name. Omit only when `labelledBy` or a `Dialog.Title` names the popup. */
  label?: string;
  labelledBy?: string;
  /** False: a press outside the panel does not close it (Escape still does). */
  dismissOnOutsidePress?: boolean;
  children: ReactNode;
  /** The panel. */
  className?: string;
  /** The scrim. Omit for a full-screen panel with no scrim. */
  backdropClassName?: string;
  /** The positioning layer around the panel (for example flex centering). */
  viewportClassName?: string;
  initialFocus?: FocusTarget;
  finalFocus?: FinalFocus;
  testId?: string;
};

/** Modal dialog: focus trap, Escape, outside press, focus return, inert page. */
export function Modal({
  open,
  onClose,
  label,
  labelledBy,
  dismissOnOutsidePress = true,
  children,
  className,
  backdropClassName,
  viewportClassName,
  initialFocus,
  finalFocus,
  testId,
}: ModalProps) {
  return (
    <Dialog.Root
      open={open}
      disablePointerDismissal={!dismissOnOutsidePress}
      onOpenChange={(next) => { if (!next) onClose(); }}
    >
      <Dialog.Portal>
        {backdropClassName !== undefined && <Dialog.Backdrop className={backdropClassName} />}
        <Dialog.Viewport className={viewportClassName}>
          <Dialog.Popup
            aria-label={label}
            {...(labelledBy ? { "aria-labelledby": labelledBy } : {})}
            className={className}
            data-testid={testId}
            initialFocus={initialFocus}
            finalFocus={finalFocus}
          >
            {children}
          </Dialog.Popup>
        </Dialog.Viewport>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** Menu root. Not modal: no scroll lock, and a press outside goes through. */
export function MenuRoot(props: Omit<ComponentProps<typeof Menu.Root>, "modal">) {
  return <Menu.Root modal={false} {...props} />;
}

export type MenuPanelProps = {
  children: ReactNode;
  label?: string;
  className?: string;
  side?: "top" | "bottom" | "left" | "right";
  align?: "start" | "center" | "end";
  sideOffset?: number;
  /** A trigger element or a `pointAnchor` for a context menu. */
  anchor?: MenuAnchor;
  finalFocus?: FocusTarget;
};

type MenuAnchor = ComponentProps<typeof Menu.Positioner>["anchor"];

/** True for a `pointAnchor`: a virtual element, not a DOM node, ref, or getter. */
function isVirtualAnchor(anchor: MenuAnchor): boolean {
  return anchor != null && typeof anchor === "object" && !("current" in anchor) && !(anchor instanceof Element);
}

/** Portal + positioner + popup for a menu. The popup carries `role="menu"`. */
export function MenuPanel({
  children,
  label,
  className,
  side = "bottom",
  align = "start",
  sideOffset = 4,
  anchor,
  finalFocus,
}: MenuPanelProps) {
  return (
    <Menu.Portal>
      <Menu.Positioner className="z-50 outline-none" side={side} align={align} sideOffset={sideOffset} anchor={anchor}>
        <Menu.Popup
          aria-label={label}
          className={className}
          finalFocus={finalFocus}
          onKeyDown={isVirtualAnchor(anchor) ? closeOnTab : undefined}
        >
          {children}
        </Menu.Popup>
      </Menu.Positioner>
    </Menu.Portal>
  );
}

/**
 * A menu at a virtual point has no DOM trigger, so Base UI cannot find the next
 * tabbable element and keeps the menu open on Tab. Send Tab through the Escape
 * path: the menu closes and focus goes to its final focus target.
 */
function closeOnTab(event: KeyboardEvent<HTMLDivElement>) {
  if (event.key !== "Tab") return;
  event.preventDefault();
  event.currentTarget.dispatchEvent(new globalThis.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
}

/** Zero-size anchor at a viewport point, for menus opened by right click or IPC. */
export function pointAnchor(x: number, y: number) {
  return {
    getBoundingClientRect: () => DOMRect.fromRect({ x, y, width: 0, height: 0 }),
  };
}
