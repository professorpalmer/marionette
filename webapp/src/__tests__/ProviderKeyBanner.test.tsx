import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ProviderKeyBanner from "../components/ProviderKeyBanner";

describe("ProviderKeyBanner", () => {
  it("clears macOS traffic lights with titlebar inset padding", () => {
    render(<ProviderKeyBanner onAddKey={vi.fn()} />);
    const banner = screen.getByTestId("provider-key-banner");
    expect(banner.className).toMatch(/\bpl-24\b/);
    expect(banner.className).toMatch(/\bpr-4\b/);
    expect(banner.className).not.toMatch(/\bpx-4\b/);
    expect(screen.getByText(/Add a provider key to run real analysis/i)).toBeTruthy();
  });

  it("invokes onAddKey when Add key is clicked", () => {
    const onAddKey = vi.fn();
    render(<ProviderKeyBanner onAddKey={onAddKey} />);
    fireEvent.click(screen.getByRole("button", { name: /Add key/i }));
    expect(onAddKey).toHaveBeenCalledTimes(1);
  });
});
