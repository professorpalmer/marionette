import { afterEach, describe, expect, it, vi } from "vitest";
import {
  getHarnessIpc,
  isDesktop,
  isTransientHarnessConnError,
  stream,
} from "../lib/transport";

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

describe("web fetch SSE stream terminal settle", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function sseResponse(chunks: string[]): Response {
    const encoder = new TextEncoder();
    let i = 0;
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (i < chunks.length) {
          controller.enqueue(encoder.encode(chunks[i++]));
          return;
        }
        controller.close();
      },
    });
    return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
  }

  it("calls onDone when the body ends without a framing done event", async () => {
    const w = window as any;
    const prevIpc = w.harnessIPC;
    delete w.harnessIPC;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => sseResponse(['data: {"kind":"message_delta","data":{"text":"hi"}}\n\n'])),
    );
    const events: string[] = [];
    let done = false;
    let err: unknown = null;
    stream(
      "/api/chat",
      (ev) => { events.push(ev.kind); },
      () => { done = true; },
      (e) => { err = e; },
    );
    await vi.waitFor(() => expect(done).toBe(true));
    expect(events).toEqual(["message_delta"]);
    expect(err).toBeNull();
    if (prevIpc === undefined) delete w.harnessIPC;
    else w.harnessIPC = prevIpc;
  });

  it("calls onDone once for a framing done event (no double settle)", async () => {
    const w = window as any;
    const prevIpc = w.harnessIPC;
    delete w.harnessIPC;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        sseResponse([
          'data: {"kind":"assistant_done","data":{}}\n\n',
          'data: {"kind":"done"}\n\n',
        ]),
      ),
    );
    let doneCount = 0;
    stream(
      "/api/chat",
      () => {},
      () => { doneCount += 1; },
      () => {},
    );
    await vi.waitFor(() => expect(doneCount).toBe(1));
    if (prevIpc === undefined) delete w.harnessIPC;
    else w.harnessIPC = prevIpc;
  });
});
