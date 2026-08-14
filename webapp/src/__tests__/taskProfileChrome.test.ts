import { describe, expect, it } from "vitest";
import {
  normalizeTaskProfile,
  publishTaskProfile,
  subscribeTaskProfile,
  taskProfileTitle,
} from "../lib/taskProfileChrome";

describe("taskProfileChrome", () => {
  it("normalizes known depths and rejects junk", () => {
    expect(normalizeTaskProfile("micro")).toBe("MICRO");
    expect(normalizeTaskProfile("STANDARD")).toBe("STANDARD");
    expect(normalizeTaskProfile("deep")).toBe("DEEP");
    expect(normalizeTaskProfile("")).toBe("");
    expect(normalizeTaskProfile("turbo")).toBe("");
  });

  it("titles MICRO with the skipped auto-inject stack", () => {
    expect(
      taskProfileTitle({
        profile: "micro",
        source: "classifier",
      }),
    ).toBe("MICRO — source classifier — skipped wiki and CodeGraph auto-inject");
  });

  it("publishes only valid profiles to subscribers", () => {
    const seen: string[] = [];
    const unsub = subscribeTaskProfile((chip) => seen.push(chip.profile));
    publishTaskProfile({ profile: "deep", source: "escalation", escalated_from: "standard" });
    publishTaskProfile({ profile: "nope" });
    unsub();
    expect(seen).toEqual(["DEEP"]);
  });
});
