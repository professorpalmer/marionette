import { describe, expect, it } from "vitest";
import {
  LOCAL_STREAM_QUIET_MS,
  quietLocalStreamDecision,
} from "../components/conversation/runnersBusy";

const live = { localStreamActive: true, turnSettled: false, userStopped: false };

describe("quiet local stream watchdog", () => {
  it("never checks a stream that is still talking, settled or stopped", () => {
    expect(quietLocalStreamDecision({ ...live, quietMs: 1000, idleSamples: 0 }).kind).toBe("skip");
    expect(quietLocalStreamDecision({ ...live, turnSettled: true, quietMs: 60_000, idleSamples: 0 }).kind).toBe("skip");
    expect(quietLocalStreamDecision({ ...live, userStopped: true, quietMs: 60_000, idleSamples: 0 }).kind).toBe("skip");
  });

  it("samples a quiet stream and abandons only after two idle answers", () => {
    const quietMs = LOCAL_STREAM_QUIET_MS + 1;
    expect(quietLocalStreamDecision({ ...live, quietMs, idleSamples: 0 })).toEqual({ kind: "sample" });
    expect(quietLocalStreamDecision({ ...live, quietMs, idleSamples: 0, backendIdle: true }))
      .toEqual({ kind: "count", idleSamples: 1 });
    expect(quietLocalStreamDecision({ ...live, quietMs, idleSamples: 1, backendIdle: true }))
      .toEqual({ kind: "abandon" });
  });

  it("a busy backend (slow model, long tool) resets the count", () => {
    expect(quietLocalStreamDecision({ ...live, quietMs: 120_000, idleSamples: 1, backendIdle: false }))
      .toEqual({ kind: "skip", idleSamples: 0 });
  });
});
