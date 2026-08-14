import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ProviderKeyBanner from "../components/ProviderKeyBanner";
import { TITLEBAR_TRAFFIC_PAD_PX } from "../lib/titlebarSafe";

describe("ProviderKeyBanner", () => {
  it("clears macOS traffic lights with fixed px titlebar inset padding", () => {
    document.documentElement.style.fontSize = "11.5px";
    render(<ProviderKeyBanner onAddKey={vi.fn()} />);
    const banner = screen.getByTestId("provider-key-banner");
    expect(banner.className).not.toMatch(/\bpl-24\b/);
    expect(banner.className).toMatch(/\bpr-4\b/);
    expect(banner.className).not.toMatch(/\bpx-4\b/);
    expect((banner as HTMLElement).style.paddingLeft).toBe(`${TITLEBAR_TRAFFIC_PAD_PX}px`);
    expect(screen.getByText(/Add a provider key to run real analysis/i)).toBeTruthy();
  });

  it("tells a pilot-only user to add a Full stack key for swarms", () => {
    render(<ProviderKeyBanner onAddKey={vi.fn()} variant="workers" />);
    const banner = screen.getByTestId("provider-key-banner");
    expect(banner.getAttribute("data-variant")).toBe("workers");
    expect(screen.getByText(/Chat works. Add an API key for swarms/i)).toBeTruthy();
  });

  it("invokes onAddKey when Add key is clicked", () => {
    const onAddKey = vi.fn();
    render(<ProviderKeyBanner onAddKey={onAddKey} />);
    fireEvent.click(screen.getByRole("button", { name: /Add key/i }));
    expect(onAddKey).toHaveBeenCalledTimes(1);
  });
});
