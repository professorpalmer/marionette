import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SwitchTrack } from "../components/ui/Switch";

describe("SwitchTrack", () => {
  it("slides a rem-sized knob with transform on the accent token", () => {
    const { container, rerender } = render(<SwitchTrack on={false} />);
    const track = container.querySelector("[data-switch-track]") as HTMLElement;
    const knob = track.firstElementChild as HTMLElement;
    expect(track.getAttribute("aria-hidden")).toBe("true");
    expect(track.className).toContain("bg-edge");
    expect(knob.className).toMatch(/\bw-4\b/);
    expect(knob.className).toMatch(/\bh-4\b/);
    expect(knob.className).toContain("transition-transform");
    expect(knob.className).not.toMatch(/\[\d+px\]|\bleft-\[/);

    rerender(<SwitchTrack on />);
    expect(track.getAttribute("data-switch-track")).toBe("on");
    expect(track.className).toContain("bg-accent/80");
    expect(knob.className).toContain("translate-x-4");
  });

  it.each(["components/SettingsPane.tsx", "components/ModelsSettingsPage.tsx"])(
    "%s renders its role=switch through SwitchTrack",
    (rel) => {
      const text = readFileSync(resolve(__dirname, "..", rel), "utf8");
      expect(text).toContain("<SwitchTrack on=");
      expect(text).not.toMatch(/left-\[18px\]|translate-x-2|bg-white/);
    },
  );
});
