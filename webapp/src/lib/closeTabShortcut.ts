/**
 * Cmd/Ctrl+W: close the focused editor tab, in-app browser tab, or
 * right-pane card. Never treat the default Electron Close Window as the
 * first action while one of those can still close.
 */

export type CloseTabKind = "editor" | "browser-tab" | "right-card" | "none";

export type CloseTabTarget =
  | { kind: "editor"; path: string }
  | { kind: "browser-tab" }
  | { kind: "right-card"; tab: string }
  | { kind: "none" };

export type CloseSurface = "editor" | "browser" | "right-card" | "other";

export function isCloseTabKey(e: {
  key: string;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
}): boolean {
  if (e.altKey || e.shiftKey) return false;
  if (!(e.metaKey || e.ctrlKey)) return false;
  return e.key.toLowerCase() === "w";
}

export function classifyCloseTabTarget(input: {
  activeEditorTab: string;
  focusedSelector: CloseSurface;
  focusedRightCard?: string | null;
  browserTabCount?: number;
}): CloseTabTarget {
  const editorPath = (input.activeEditorTab || "").trim();
  const editorOpen = editorPath !== "" && editorPath !== "chat";

  if (input.focusedSelector === "editor" && editorOpen) {
    return { kind: "editor", path: editorPath };
  }
  if (input.focusedSelector === "browser") {
    if ((input.browserTabCount ?? 1) > 1) return { kind: "browser-tab" };
    return { kind: "right-card", tab: "browser" };
  }
  if (input.focusedSelector === "right-card" && input.focusedRightCard) {
    return { kind: "right-card", tab: input.focusedRightCard };
  }
  if (editorOpen) return { kind: "editor", path: editorPath };
  return { kind: "none" };
}

export function focusedCloseSurface(el: Element | null): {
  selector: CloseSurface;
  rightCard?: string;
  browserTabCount?: number;
} {
  if (!el || typeof el.closest !== "function") return { selector: "other" };
  const editor = el.closest("[data-close-surface='editor']");
  if (editor) return { selector: "editor" };
  const browser = el.closest("[data-close-surface='browser']");
  if (browser) {
    const raw = browser.getAttribute("data-browser-tab-count");
    const n = raw ? Number(raw) : 1;
    return { selector: "browser", browserTabCount: Number.isFinite(n) ? n : 1 };
  }
  const card = el.closest("[id^='right-pane-card-']");
  if (card && card.id) {
    return { selector: "right-card", rightCard: card.id.slice("right-pane-card-".length) };
  }
  return { selector: "other" };
}

export function readActiveEditorTab(root: ParentNode | null = typeof document !== "undefined" ? document : null): string {
  const node = root?.querySelector?.("[data-active-editor-tab]");
  const value = node?.getAttribute?.("data-active-editor-tab") || "";
  return value.trim() || "chat";
}

export function requestCloseFocusedTab(
  root: ParentNode | null = typeof document !== "undefined" ? document : null,
): CloseTabKind {
  const active = typeof document !== "undefined" ? document.activeElement : null;
  const surface = focusedCloseSurface(active instanceof Element ? active : null);
  const target = classifyCloseTabTarget({
    activeEditorTab: readActiveEditorTab(root),
    focusedSelector: surface.selector,
    focusedRightCard: surface.rightCard,
    browserTabCount: surface.browserTabCount,
  });
  if (target.kind === "editor") {
    window.dispatchEvent(new CustomEvent("harness-close-editor-tab", { detail: { path: target.path } }));
    return "editor";
  }
  if (target.kind === "browser-tab") {
    window.dispatchEvent(new Event("harness-close-browser-tab"));
    return "browser-tab";
  }
  if (target.kind === "right-card") {
    window.dispatchEvent(new CustomEvent("harness-close-right-card", { detail: { tab: target.tab } }));
    return "right-card";
  }
  return "none";
}
