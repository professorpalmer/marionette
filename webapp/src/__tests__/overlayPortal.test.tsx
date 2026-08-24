import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createPortal } from "react-dom";
import settingsShell from "../components/SettingsShell.tsx?raw";
import commandPalette from "../components/CommandPalette.tsx?raw";
import spillPreview from "../components/conversation/SpillPreviewModal.tsx?raw";
import imageLightbox from "../components/conversation/ImageLightbox.tsx?raw";
import column from "../components/conversation/ConversationChatColumn.tsx?raw";
import helpers from "../components/conversation/feedMotion.tsx?raw";
import list from "../components/TranscriptList.tsx?raw";
import overlayPortal from "../lib/overlayPortal.tsx?raw";
import css from "../index.css?raw";
import pkgJson from "../../package.json?raw";
import ImageLightbox from "../components/conversation/ImageLightbox";
import {
  FeedOverlayHost,
  overlayDataAttrs,
  useOverlayEnterLeave,
} from "../lib/overlayPortal";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function OverlayFixture({
  open,
  onExited,
  onInstant,
}: {
  open: boolean;
  onExited?: () => void;
  onInstant?: (instant: boolean) => void;
}) {
  const overlay = useOverlayEnterLeave(open, { onExited, exitMs: 50 });
  onInstant?.(overlay.dataInstant);
  if (!overlay.mounted) return null;
  return createPortal(
    <div
      data-testid="overlay-fixture"
      className="overlay-root transition-opacity duration-200"
      ref={overlay.rootRef}
      {...overlayDataAttrs(overlay)}
    />,
    document.body,
  );
}

describe("overlay portal polish (v0.9.320)", () => {
  it("bumps the product to 0.9.320 and keeps the official motion pin", () => {
    const pkg = JSON.parse(pkgJson) as {
      version?: string;
      dependencies?: Record<string, string>;
    };
    expect(pkg.version).toBe("0.9.320");
    expect(pkg.dependencies?.motion).toBeTruthy();
    expect(pkg.dependencies?.["framer-motion"]).toBeUndefined();
  });

  it("portals conversation/app overlays with createPortal and enter/leave attrs", () => {
    expect(overlayPortal).toContain("createPortal");
    expect(overlayPortal).toContain("data-starting-style");
    expect(overlayPortal).toContain("data-instant");
    expect(overlayPortal).toContain('data-testid="feed-overlay-portal"');
    for (const src of [settingsShell, commandPalette, spillPreview, imageLightbox]) {
      expect(src).toContain("createPortal");
      expect(src).toContain("useOverlayEnterLeave");
    }
    expect(settingsShell).toContain("document.body");
    expect(commandPalette).toContain("document.body");
    expect(spillPreview).toContain("useOverlayPortalHost");
    expect(imageLightbox).toContain("useOverlayPortalHost");
    expect(column).toContain("FeedOverlayHost");
  });

  it("uses data-starting-style / data-instant and CSS @starting-style", () => {
    expect(css).toContain("@starting-style");
    expect(css).toContain("[data-starting-style]");
    expect(css).toContain("[data-instant]");
    expect(overlayDataAttrs({ dataStartingStyle: true, dataInstant: false })["data-starting-style"]).toBe("");
    expect(overlayDataAttrs({ dataStartingStyle: false, dataInstant: true })["data-instant"]).toBe("");
  });

  it("keeps 314/317/318 feed contracts in chat column and motion helpers", () => {
    expect(column).toContain("layoutScroll");
    expect(column).toContain("[overflow-anchor:auto]");
    expect(column).toContain("[scroll-padding-bottom:var(--feed-chrome-clearance");
    expect(column).not.toContain("overflow-anchor:none");
    expect(helpers).toContain('mode="popLayout"');
    expect(list).toContain("FeedMotionPresence");
    expect(list).toContain("useVirtualizer");
    expect(column).not.toMatch(/@stylexjs|stylex\./);
    expect(helpers).not.toMatch(/@stylexjs|stylex\./);
    expect(list).not.toMatch(/@stylexjs|stylex\./);
    expect(imageLightbox).not.toMatch(/node:|@stylexjs|stylex\./);
    expect(spillPreview).not.toMatch(/node:|@stylexjs|stylex\./);
  });

  it("portals the lightbox into the host above the virtualizer, not the scrollport", () => {
    render(
      <div>
        <div data-testid="fake-scrollport" />
        <FeedOverlayHost />
        <ImageLightbox url="https://example.test/shot.png" onClose={vi.fn()} />
      </div>,
    );
    const host = screen.getByTestId("feed-overlay-portal");
    const overlay = screen.getByTestId("image-lightbox");
    expect(host.contains(overlay)).toBe(true);
    expect(screen.getByTestId("fake-scrollport").contains(overlay)).toBe(false);
    expect(overlay.hasAttribute("data-starting-style")).toBe(true);
  });

  it("mounts portaled overlay on document.body outside a scrollport fixture", async () => {
    const scrollport = document.createElement("div");
    scrollport.className = "overflow-y-auto";
    document.body.append(scrollport);

    const { rerender } = render(
      <div data-testid="scrollport-fixture" className="overflow-y-auto">
        <OverlayFixture open />
      </div>,
      { container: scrollport },
    );

    const overlay = await screen.findByTestId("overlay-fixture");
    expect(document.body.contains(overlay)).toBe(true);
    expect(scrollport.contains(overlay)).toBe(false);
    expect(overlay.hasAttribute("data-starting-style")).toBe(true);

    await act(async () => {
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    });
    expect(overlay.hasAttribute("data-starting-style")).toBe(false);

    const onExited = vi.fn();
    rerender(
      <div data-testid="scrollport-fixture" className="overflow-y-auto">
        <OverlayFixture open={false} onExited={onExited} />
      </div>,
    );

    await act(async () => {
      await new Promise((r) => setTimeout(r, 80));
    });
    expect(onExited).toHaveBeenCalled();
    expect(screen.queryByTestId("overlay-fixture")).toBeNull();

    scrollport.remove();
  });

  it("uses data-instant on leave when prefers-reduced-motion is set", async () => {
    const mql = {
      matches: true,
      media: "(prefers-reduced-motion: reduce)",
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
      onchange: null,
    };
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: vi.fn().mockReturnValue(mql),
    });

    let sawInstant = false;
    const { rerender } = render(
      <OverlayFixture open onInstant={(instant) => { if (instant) sawInstant = true; }} />,
    );
    await screen.findByTestId("overlay-fixture");

    rerender(
      <OverlayFixture open={false} onInstant={(instant) => { if (instant) sawInstant = true; }} />,
    );

    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(sawInstant).toBe(true);
    expect(screen.queryByTestId("overlay-fixture")).toBeNull();
  });
});
