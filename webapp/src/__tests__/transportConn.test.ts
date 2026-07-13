import { describe, expect, it } from "vitest";
import { isTransientHarnessConnError } from "../lib/transport";

describe("isTransientHarnessConnError", () => {
  it("matches Electron harness:getJSON ECONNREFUSED wrappers", () => {
    expect(
      isTransientHarnessConnError(
        new Error(
          "Error invoking remote method 'harness:getJSON': Error: connect ECONNREFUSED 127.0.0.1:49376",
        ),
      ),
    ).toBe(true);
  });

  it("rejects unrelated failures", () => {
    expect(isTransientHarnessConnError(new Error("Failed to get workspace files"))).toBe(false);
  });
});
