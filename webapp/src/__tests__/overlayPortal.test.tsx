import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import column from "../components/conversation/ConversationChatColumn.tsx?raw";
import spillSource from "../components/conversation/SpillPreviewModal.tsx?raw";
import lightboxSource from "../components/conversation/ImageLightbox.tsx?raw";
import paletteSource from "../components/CommandPalette.tsx?raw";
import helperSource from "../lib/overlayPortal.tsx?raw";
import cssSource from "../index.css?raw";
import SpillPreviewModal from "../components/conversation/SpillPreviewModal";
import ImageLightbox from "../components/conversation/ImageLightbox";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("overlay portal contract (v0.9.322)", () => {
  it("portals conversation overlays to document.body via createPortal", () => {
    expect(helperSource).toContain("createPortal");
    expect(helperSource).toContain("document.body");
    expect(spillSource).toContain("OverlayPortal");
    expect(lightboxSource).toContain("OverlayPortal");
    expect(paletteSource).toContain("OverlayPortal");
    expect(helperSource).toContain("data-starting-style");
    expect(helperSource).toContain("data-instant");
  });

  it("uses CSS attribute selectors for enter/leave without StyleX or Motion on overlays", () => {
    for (const src of [spillSource, lightboxSource, helperSource, paletteSource]) {
      expect(src).not.toMatch(/from\s+["']motion["']|from\s+["']framer-motion["']/);
      expect(src).not.toMatch(/@stylexjs|stylex\./);
      expect(src).not.toMatch(/node:/);
    }
    expect(cssSource).toContain("[data-starting-style]");
    expect(cssSource).toContain("[data-instant]");
  });

  it("keeps feed scrollport overflow-anchor:auto and scroll-padding-bottom", () => {
    expect(column).toContain("[overflow-anchor:auto]");
    expect(column).toContain("[scroll-padding-bottom:var(--feed-chrome-clearance");
    expect(column).not.toContain("overflow-anchor:none");
  });

  it("renders SpillPreviewModal backdrop as a document.body descendant outside the virtual list", async () => {
    const virtualList = document.createElement("div");
    virtualList.setAttribute("data-testid", "transcript-virtual-list");
    document.body.append(virtualList);

    render(
      <SpillPreviewModal
        preview={{
          uri: "spill://stdout/abc",
          content: "hello spill",
          chars: 11,
          truncated: false,
        }}
        onClose={() => {}}
      />,
    );

    const modal = screen.getByTestId("spill-preview-modal");
    expect(document.body.contains(modal)).toBe(true);
    expect(virtualList.contains(modal)).toBe(false);
    expect(modal).toHaveAttribute("data-starting-style");

    await waitFor(() => {
      expect(modal.hasAttribute("data-starting-style")).toBe(false);
    });

    virtualList.remove();
  });

  it("renders ImageLightbox as a body descendant and clears data-starting-style after enter", async () => {
    const virtualList = document.createElement("div");
    virtualList.setAttribute("data-testid", "transcript-virtual-list");
    document.body.append(virtualList);

    render(
      <ImageLightbox url="blob:test-image" onClose={() => {}} />,
    );

    const backdrop = document.body.querySelector(".fixed.inset-0.z-50");
    expect(backdrop).toBeTruthy();
    expect(virtualList.contains(backdrop!)).toBe(false);
    expect(backdrop).toHaveAttribute("data-starting-style");

    await waitFor(() => {
      expect(backdrop!.hasAttribute("data-starting-style")).toBe(false);
    });

    virtualList.remove();
  });

  it("sets data-instant on close so leave is not animated", async () => {
    const onClose = vi.fn();
    const { rerender } = render(
      <SpillPreviewModal
        preview={{
          uri: "spill://stdout/xyz",
          content: "body",
          chars: 4,
          truncated: false,
        }}
        onClose={onClose}
      />,
    );

    expect(screen.getByTestId("spill-preview-modal")).toBeTruthy();

    await act(async () => {
      rerender(
        <SpillPreviewModal preview={null} onClose={onClose} />,
      );
    });

    const instant = document.body.querySelector("[data-instant]");
    expect(instant).toBeTruthy();

    await waitFor(() => {
      expect(screen.queryByTestId("spill-preview-modal")).toBeNull();
    });
  });
});
