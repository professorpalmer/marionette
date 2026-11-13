import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import column from "../components/conversation/ConversationChatColumn.tsx?raw";
import spillSource from "../components/conversation/SpillPreviewModal.tsx?raw";
import lightboxSource from "../components/conversation/ImageLightbox.tsx?raw";
import paletteSource from "../components/CommandPalette.tsx?raw";
import overlaySource from "../components/ui/overlay.tsx?raw";
import cssSource from "../index.css?raw";
import SpillPreviewModal from "../components/conversation/SpillPreviewModal";
import ImageLightbox from "../components/conversation/ImageLightbox";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("overlay enter and leave contract", () => {
  it("portals every modal through the one Base UI overlay module", () => {
    expect(overlaySource).toContain("Dialog.Portal");
    for (const src of [spillSource, lightboxSource, paletteSource]) {
      expect(src).toContain("Modal");
      expect(src).not.toContain("createPortal");
    }
  });

  it("uses CSS attribute selectors for enter without StyleX or Motion on overlays", () => {
    for (const src of [spillSource, lightboxSource, overlaySource, paletteSource]) {
      expect(src).not.toMatch(/from\s+["']motion["']|from\s+["']framer-motion["']/);
      expect(src).not.toMatch(/@stylexjs|stylex\./);
      expect(src).not.toMatch(/node:/);
    }
    expect(cssSource).toContain("[data-starting-style]");
    expect(cssSource).not.toContain("[data-ending-style]");
  });

  it("keeps feed scrollport overflow-anchor:auto and scroll-padding-bottom", () => {
    expect(column).toContain("feedScrollportStyle");
    expect(column).toContain("feedSeatingReservePx");
    expect(column).not.toContain("scroll-pb-6");
    expect(column).not.toContain("overflow-anchor:none");
    expect(column).not.toContain("justify-end");
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

    await waitFor(() => {
      expect(modal.hasAttribute("data-starting-style")).toBe(false);
    });

    virtualList.remove();
  });

  it("renders ImageLightbox as a body descendant and ends the enter state", async () => {
    const virtualList = document.createElement("div");
    virtualList.setAttribute("data-testid", "transcript-virtual-list");
    document.body.append(virtualList);

    render(
      <ImageLightbox url="blob:test-image" onClose={() => {}} />,
    );

    const backdrop = document.body.querySelector(".fixed.inset-0.z-50");
    expect(backdrop).toBeTruthy();
    expect(virtualList.contains(backdrop!)).toBe(false);

    await waitFor(() => {
      expect(backdrop!.hasAttribute("data-starting-style")).toBe(false);
    });

    virtualList.remove();
  });

  it("unmounts on close with no exit animation", async () => {
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

    await waitFor(() => {
      expect(screen.queryByTestId("spill-preview-modal")).toBeNull();
    });
    expect(document.body.querySelector("[data-ending-style]")).toBeNull();
  });
});
