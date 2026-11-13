import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ProviderKeyBanner from "../components/ProviderKeyBanner";
import { TITLEBAR_CHROME_PAD_X_PX, TITLEBAR_TRAFFIC_PAD_PX } from "../lib/titlebarSafe";

const css = readFileSync(resolve(__dirname, "../index.css"), "utf8");

function glassBlock(): string {
  const start = css.indexOf(":root[data-marionette-glass] {");
  expect(start).toBeGreaterThan(-1);
  return css.slice(start, css.indexOf("}", start));
}

describe("glass is a frame material", () => {
  it("thins only frame tokens; content surfaces stay opaque", () => {
    const block = glassBlock();
    expect(block).toMatch(/--shell-frame:\s*color-mix\([^;]*--translucency-frame-keep/);
    expect(block).toMatch(/--shell-sidebar:\s*transparent/);
    // The chat card, tool cards, composer family and flyouts must not thin.
    for (const token of ["--shell-chat", "--shell-panel:", "--shell-overlay"]) {
      expect(block).not.toContain(token);
    }
  });

  it("paints the chat card from the opaque chat token", () => {
    const card = css.slice(css.indexOf(".shell-chat-card {"));
    expect(card.slice(0, card.indexOf("}"))).toContain("background-color: var(--shell-chat)");
  });
});

describe("top strip clearance", () => {
  it("drops the traffic-light pad for a strip right of the open rail", () => {
    const rect = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockReturnValue({ left: 260, top: 0, right: 900, bottom: 30, width: 640, height: 30, x: 260, y: 0, toJSON: () => ({}) });
    render(<ProviderKeyBanner onAddKey={vi.fn()} />);
    const strip = screen.getByTestId("provider-key-banner");
    // A strip right of an open left rail does not need the traffic-light pad.
    expect(strip.style.paddingLeft).toBe(`${TITLEBAR_CHROME_PAD_X_PX}px`);
    expect(TITLEBAR_CHROME_PAD_X_PX).toBeLessThan(TITLEBAR_TRAFFIC_PAD_PX);
    rect.mockRestore();
  });
});
