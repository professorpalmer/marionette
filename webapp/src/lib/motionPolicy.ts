/** Root classes that pause decorative CSS motion when the window is idle. */
export const APP_IDLE_CLASS = "app-idle";
export const APP_HIDDEN_CLASS = "app-hidden";

export type MotionViewport = {
  hidden: boolean;
  focused: boolean;
};

export type MotionRootState = {
  idle: boolean;
  hidden: boolean;
};

/** Decorative motion pauses on blur; everything pauses when hidden/minimized. */
export function motionRootClasses(viewport: MotionViewport): MotionRootState {
  return {
    idle: viewport.hidden || !viewport.focused,
    hidden: viewport.hidden,
  };
}

export function applyMotionRootClasses(
  root: Pick<Element, "classList">,
  viewport: MotionViewport,
): void {
  const next = motionRootClasses(viewport);
  root.classList.toggle(APP_IDLE_CLASS, next.idle);
  root.classList.toggle(APP_HIDDEN_CLASS, next.hidden);
}

export function clearMotionRootClasses(root: Pick<Element, "classList">): void {
  root.classList.remove(APP_IDLE_CLASS, APP_HIDDEN_CLASS);
}

/** Bind html.app-idle / html.app-hidden; unsubscribe always clears both classes. */
export function subscribeDocumentMotionPolicy(): () => void {
  const root = document.documentElement;
  const sync = () => {
    applyMotionRootClasses(root, {
      hidden: document.hidden,
      focused: document.hasFocus(),
    });
  };
  sync();
  window.addEventListener("blur", sync);
  window.addEventListener("focus", sync);
  document.addEventListener("visibilitychange", sync);
  return () => {
    window.removeEventListener("blur", sync);
    window.removeEventListener("focus", sync);
    document.removeEventListener("visibilitychange", sync);
    clearMotionRootClasses(root);
  };
}
