import { describe, expect, it } from "vitest";
import { getHarnessIpc, isDesktop, isTransientHarnessConnError } from "../lib/transport";

describe("isDesktop", () => {
  it("reflects late preload injection instead of import-time snapshot", () => {
    const w = window as any;
    const prev = w.harnessIPC;
    delete w.harnessIPC;
    expect(isDesktop()).toBe(false);
    w.harnessIPC = { getJSON: () => Promise.resolve({}) };
    expect(isDesktop()).toBe(true);
    if (prev === undefined) delete w.harnessIPC;
    else w.harnessIPC = prev;
  });
});

describe("getHarnessIpc", () => {
  it("returns the live window.harnessIPC reference", () => {
    const w = window as any;
    const prev = w.harnessIPC;
    const bridge = { stream: () => () => {} };
    w.harnessIPC = bridge;
    expect(getHarnessIpc()).toBe(bridge);
    if (prev === undefined) delete w.harnessIPC;
    else w.harnessIPC = prev;
  });
});

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
