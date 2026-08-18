import { afterEach, describe, expect, it, vi } from "vitest";
import motionCss from "../index.css?raw";
import {
  APP_HIDDEN_CLASS,
  APP_IDLE_CLASS,
  applyMotionRootClasses,
  clearMotionRootClasses,
  motionRootClasses,
  subscribeDocumentMotionPolicy,
} from "../lib/motionPolicy";

function mediaBlock(css: string, query: string): string {
  const start = css.indexOf(`@media (${query})`);
  expect(start).toBeGreaterThan(-1);
  const open = css.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < css.length; i++) {
    if (css[i] === "{") depth += 1;
    else if (css[i] === "}") {
      depth -= 1;
      if (depth === 0) return css.slice(start, i + 1);
    }
  }
  throw new Error(`unclosed @media (${query})`);
}

describe("motionRootClasses", () => {
  it("leaves the root unmarked when the window is focused and visible", () => {
    expect(motionRootClasses({ hidden: false, focused: true })).toEqual({
      idle: false,
      hidden: false,
    });
  });

  it("marks idle-only when visible but unfocused so decorative motion can pause", () => {
    expect(motionRootClasses({ hidden: false, focused: false })).toEqual({
      idle: true,
      hidden: false,
    });
  });

  it("marks idle and hidden when minimized, even if the document still reports focus", () => {
    expect(motionRootClasses({ hidden: true, focused: true })).toEqual({
      idle: true,
      hidden: true,
    });
    expect(motionRootClasses({ hidden: true, focused: false })).toEqual({
      idle: true,
      hidden: true,
    });
  });
});

describe("applyMotionRootClasses / clearMotionRootClasses", () => {
  it("toggles and clears html classes without leaving residue", () => {
    const root = document.createElement("div");
    applyMotionRootClasses(root, { hidden: false, focused: false });
    expect(root.classList.contains(APP_IDLE_CLASS)).toBe(true);
    expect(root.classList.contains(APP_HIDDEN_CLASS)).toBe(false);

    applyMotionRootClasses(root, { hidden: true, focused: false });
    expect(root.classList.contains(APP_IDLE_CLASS)).toBe(true);
    expect(root.classList.contains(APP_HIDDEN_CLASS)).toBe(true);

    applyMotionRootClasses(root, { hidden: false, focused: true });
    expect(root.classList.contains(APP_IDLE_CLASS)).toBe(false);
    expect(root.classList.contains(APP_HIDDEN_CLASS)).toBe(false);

    applyMotionRootClasses(root, { hidden: true, focused: true });
    clearMotionRootClasses(root);
    expect(root.classList.contains(APP_IDLE_CLASS)).toBe(false);
    expect(root.classList.contains(APP_HIDDEN_CLASS)).toBe(false);
  });
});

describe("subscribeDocumentMotionPolicy", () => {
  const viewport = { hidden: false, focused: true };
  let stop: (() => void) | undefined;

  afterEach(() => {
    stop?.();
    stop = undefined;
    vi.restoreAllMocks();
    Reflect.deleteProperty(document, "hidden");
    document.documentElement.classList.remove(APP_IDLE_CLASS, APP_HIDDEN_CLASS);
  });

  function installViewport() {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      enumerable: true,
      get: () => viewport.hidden,
    });
    vi.spyOn(document, "hasFocus").mockImplementation(() => viewport.focused);
  }

  it("applies blur vs hidden class policy and clears both on unsubscribe", () => {
    viewport.hidden = false;
    viewport.focused = true;
    installViewport();
    stop = subscribeDocumentMotionPolicy();
    const root = document.documentElement;
    expect(root.classList.contains(APP_IDLE_CLASS)).toBe(false);
    expect(root.classList.contains(APP_HIDDEN_CLASS)).toBe(false);

    viewport.focused = false;
    window.dispatchEvent(new Event("blur"));
    expect(root.classList.contains(APP_IDLE_CLASS)).toBe(true);
    expect(root.classList.contains(APP_HIDDEN_CLASS)).toBe(false);

    viewport.hidden = true;
    document.dispatchEvent(new Event("visibilitychange"));
    expect(root.classList.contains(APP_IDLE_CLASS)).toBe(true);
    expect(root.classList.contains(APP_HIDDEN_CLASS)).toBe(true);

    const unsub = stop;
    stop = undefined;
    unsub();
    expect(root.classList.contains(APP_IDLE_CLASS)).toBe(false);
    expect(root.classList.contains(APP_HIDDEN_CLASS)).toBe(false);

    window.dispatchEvent(new Event("blur"));
    document.dispatchEvent(new Event("visibilitychange"));
    expect(root.classList.contains(APP_IDLE_CLASS)).toBe(false);
    expect(root.classList.contains(APP_HIDDEN_CLASS)).toBe(false);
  });

  it("adds and removes the same blur, focus, and visibility listeners", () => {
    viewport.hidden = false;
    viewport.focused = true;
    installViewport();
    const addWin = vi.spyOn(window, "addEventListener");
    const remWin = vi.spyOn(window, "removeEventListener");
    const addDoc = vi.spyOn(document, "addEventListener");
    const remDoc = vi.spyOn(document, "removeEventListener");

    stop = subscribeDocumentMotionPolicy();
    const blur = addWin.mock.calls.find((call) => call[0] === "blur")?.[1];
    const focus = addWin.mock.calls.find((call) => call[0] === "focus")?.[1];
    const visibility = addDoc.mock.calls.find((call) => call[0] === "visibilitychange")?.[1];
    expect(blur).toEqual(expect.any(Function));
    expect(focus).toBe(blur);
    expect(visibility).toBe(blur);

    const unsub = stop;
    stop = undefined;
    unsub();
    expect(remWin.mock.calls.some((call) => call[0] === "blur" && call[1] === blur)).toBe(true);
    expect(remWin.mock.calls.some((call) => call[0] === "focus" && call[1] === focus)).toBe(true);
    expect(remDoc.mock.calls.some((call) => call[0] === "visibilitychange" && call[1] === visibility)).toBe(true);
  });
});

describe("index.css motion selector contract", () => {
  it("pauses idle descendants, resumes only marked semantic spinners while visible, and keeps reduced-motion authoritative", () => {
    const reduceAt = motionCss.indexOf("@media (prefers-reduced-motion: reduce)");
    expect(reduceAt).toBeGreaterThan(-1);
    const beforeReduce = motionCss.slice(0, reduceAt);

    expect(beforeReduce).toMatch(
      /html\.app-idle \*,\s*html\.app-idle \*::before,\s*html\.app-idle \*::after\s*\{[^}]*animation-play-state:\s*paused\s*!important/,
    );
    expect(beforeReduce).toMatch(
      /html\.app-idle:not\(\.app-hidden\) \.semantic-activity-spinner\s*\{[^}]*animation-play-state:\s*running\s*!important/,
    );
    expect(beforeReduce.indexOf("html.app-idle *")).toBeLessThan(
      beforeReduce.indexOf("html.app-idle:not(.app-hidden) .semantic-activity-spinner"),
    );

    const runningRules = [...beforeReduce.matchAll(/([^{]+)\{[^}]*animation-play-state:\s*running/g)];
    expect(runningRules).toHaveLength(1);
    expect(runningRules[0][1]).toContain(".semantic-activity-spinner");
    expect(runningRules[0][1]).toContain(":not(.app-hidden)");
    expect(runningRules[0][1]).not.toContain(".animate-pulse");
    expect(runningRules[0][1]).not.toContain(".animate-spin");

    const reduce = mediaBlock(motionCss, "prefers-reduced-motion: reduce");
    expect(reduce).not.toContain("semantic-activity-spinner");
    expect(reduce).not.toContain("animation-play-state");
    expect(reduce).toMatch(/animation-duration:\s*0\.001ms\s*!important/);
    expect(reduce).toMatch(/animation-iteration-count:\s*1\s*!important/);
  });
});
