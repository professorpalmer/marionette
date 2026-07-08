import { describe, expect, it } from "vitest";
import { looksLikeRawToolOutput, sanitizeUpdateMessage } from "../lib/updateMessages";

describe("looksLikeRawToolOutput", () => {
  it("flags npm warning lines as raw tool output", () => {
    expect(looksLikeRawToolOutput("npm warn deprecated inflight@1.0.6")).toBe(true);
  });

  it("flags git fetch chatter as raw tool output", () => {
    expect(looksLikeRawToolOutput("Receiving objects: 42% (12/28)")).toBe(true);
  });

  it("passes through calm single-line labels", () => {
    expect(looksLikeRawToolOutput("Updating dependencies")).toBe(false);
  });

  it("flags multi-line messages as raw tool output", () => {
    expect(looksLikeRawToolOutput("line one\nline two")).toBe(true);
  });
});

describe("sanitizeUpdateMessage", () => {
  it("maps raw npm noise to the stage fallback label", () => {
    expect(sanitizeUpdateMessage("deps", "npm warn deprecated lodash@4.17.21")).toBe(
      "Updating dependencies",
    );
  });

  it("maps raw git noise to the stage fallback label", () => {
    expect(sanitizeUpdateMessage("pull", "Resolving deltas: 100% (8/8)")).toBe("Updating source");
  });

  it("passes through clean messages unchanged", () => {
    expect(sanitizeUpdateMessage("build", "Rebuilding app")).toBe("Rebuilding app");
  });

  it("uses a generic fallback for unknown stages with empty input", () => {
    expect(sanitizeUpdateMessage("unknown", "")).toBe("Updating");
  });
});
