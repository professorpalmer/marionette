import { getConfig } from "@testing-library/react";
import { describe, expect, it } from "vitest";

describe("async wait budget", () => {
  it("gives findBy*/waitFor room for a loaded CI runner, inside the per-test timeout", () => {
    // CI ran this suite 7-9x slower than a dev machine (warm LeftRail tests
    // 900-946ms vs ~130ms), so the library's 1000ms default failed a cold
    // first render at 1242ms. Vitest's per-test timeout is 5000ms; staying
    // under it keeps the precise "Unable to find" error on real failures.
    const { asyncUtilTimeout } = getConfig();
    expect(asyncUtilTimeout).toBeGreaterThanOrEqual(4000);
    expect(asyncUtilTimeout).toBeLessThan(5000);
  });
});
