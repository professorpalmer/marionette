import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ProviderKeyBanner from "../components/ProviderKeyBanner";
import ConversationHeader from "../components/conversation/ConversationHeader";
import {
  MACOS_TRAFFIC_LIGHT_RIGHT_EDGE_PX,
  TITLEBAR_CHROME_PAD_X_PX,
  TITLEBAR_TRAFFIC_PAD_PX,
} from "../lib/titlebarSafe";

function paddingLeftPx(el: Element): number {
  const style = (el as HTMLElement).style.paddingLeft;
  return parseFloat(style || getComputedStyle(el).paddingLeft || "0");
}

function paddingRightPx(el: Element): number {
  const style = (el as HTMLElement).style.paddingRight;
  return parseFloat(style || getComputedStyle(el).paddingRight || "0");
}

// The smallest root font-size the responsive `html { font-size: clamp(...) }`
// can reach; rem padding written at this size is what used to slide content
// back under the traffic lights.
const SMALLEST_ROOT_FONT_SIZE = "11.5px";

describe("titlebar-safe clearance", () => {
  it("ProviderKeyBanner keeps fixed px traffic pad when root font shrinks", () => {
    document.documentElement.style.fontSize = "16px";
    render(<ProviderKeyBanner onAddKey={vi.fn()} />);
    const banner = screen.getByTestId("provider-key-banner");
    expect(banner.className).not.toMatch(/\bpl-24\b/);
    expect(paddingLeftPx(banner)).toBe(TITLEBAR_TRAFFIC_PAD_PX);

    document.documentElement.style.fontSize = SMALLEST_ROOT_FONT_SIZE;
    expect(paddingLeftPx(banner)).toBe(TITLEBAR_TRAFFIC_PAD_PX);
    expect(paddingLeftPx(banner)).toBeGreaterThan(MACOS_TRAFFIC_LIGHT_RIGHT_EDGE_PX);
  });

  it("ConversationHeader clears the traffic lights at every root font-size", () => {
    document.documentElement.style.fontSize = "16px";
    render(<ConversationHeader pillStatus="idle" />);
    const header = screen.getByTestId("conversation-header");
    expect(header.className).not.toMatch(/\bpx-6\b/);
    // The header can be the topmost row (no banners, rail collapsed), so the
    // narrower chrome pad would leave the brand text under the traffic lights.
    expect(paddingLeftPx(header)).toBe(TITLEBAR_TRAFFIC_PAD_PX);
    expect(paddingRightPx(header)).toBe(TITLEBAR_CHROME_PAD_X_PX);

    document.documentElement.style.fontSize = SMALLEST_ROOT_FONT_SIZE;
    expect(paddingLeftPx(header)).toBe(TITLEBAR_TRAFFIC_PAD_PX);
    expect(paddingLeftPx(header)).toBeGreaterThan(MACOS_TRAFFIC_LIGHT_RIGHT_EDGE_PX);
  });

  it("the traffic-light pads are wider than the traffic lights themselves", () => {
    expect(TITLEBAR_TRAFFIC_PAD_PX).toBeGreaterThan(MACOS_TRAFFIC_LIGHT_RIGHT_EDGE_PX);
    expect(TITLEBAR_CHROME_PAD_X_PX).toBeLessThan(MACOS_TRAFFIC_LIGHT_RIGHT_EDGE_PX);
  });
});

describe("ProviderKeyBanner actions", () => {
  it("invokes onAddKey when Add key is clicked", () => {
    const onAddKey = vi.fn();
    render(<ProviderKeyBanner onAddKey={onAddKey} />);
    fireEvent.click(screen.getByRole("button", { name: /Add key/i }));
    expect(onAddKey).toHaveBeenCalledTimes(1);
  });
});
