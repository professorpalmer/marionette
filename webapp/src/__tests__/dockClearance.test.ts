import { describe, expect, it } from "vitest";
import { dockClearancePad } from "../lib/dockClearance";

// Measured in the rig at 1024px: transcript text ran to x=595, pill starts at x=569.
describe("dock clearance", () => {
  it("pads the column just enough to clear an overlapping pill", () => {
    expect(dockClearancePad({ columnRight: 615, basePad: 24, pillLeft: 569 })).toBe(24 + 30);
  });
  it("adds nothing when the pill is clear of the column", () => {
    expect(dockClearancePad({ columnRight: 900, basePad: 24, pillLeft: 1200 })).toBe(24);
  });
  it("adds nothing without a pill", () => {
    expect(dockClearancePad({ columnRight: 900, basePad: 24, pillLeft: null })).toBe(24);
  });
});
