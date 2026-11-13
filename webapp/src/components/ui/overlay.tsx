/**
 * The one overlay stack. Base UI owns focus, Escape, outside press, and focus
 * return; call sites only paint with tokens. Do not add document listeners for
 * these behaviors anywhere else.
 */
import type { ComponentProps, ReactNode, RefObject } from "react";
import { Dialog } from "@base-ui/react/dialog";
import { Menu } from "@base-ui/react/menu";

type FocusTarget = RefObject<HTMLElement | null> | boolean;

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
  finalFocus?: FocusTarget;
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
            aria-labelledby={labelledBy}
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
  anchor?: ComponentProps<typeof Menu.Positioner>["anchor"];
  finalFocus?: FocusTarget;
};

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
        <Menu.Popup aria-label={label} className={className} finalFocus={finalFocus}>
          {children}
        </Menu.Popup>
      </Menu.Positioner>
    </Menu.Portal>
  );
}

/** Zero-size anchor at a viewport point, for menus opened by right click or IPC. */
export function pointAnchor(x: number, y: number) {
  return {
    getBoundingClientRect: () => DOMRect.fromRect({ x, y, width: 0, height: 0 }),
  };
}
