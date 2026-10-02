import { beforeEach, describe, expect, it, vi } from "vitest";

const getJSON = vi.fn();
vi.mock("../lib/transport", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/transport")>()),
  getJSON: (...args: unknown[]) => getJSON(...args),
}));

import { api } from "../lib/api";

describe("identical concurrent reads share one request", () => {
  beforeEach(() => { getJSON.mockReset(); });

  it("a session switch's simultaneous state reads become one request", async () => {
    let release: (v: unknown) => void = () => {};
    getJSON.mockImplementation(() => new Promise((r) => { release = r; }));
    // Six components asked for the same state in the same millisecond.
    const calls = Array.from({ length: 6 }, () => api.getSessionState({ sessionId: "s1" }));
    expect(getJSON).toHaveBeenCalledTimes(1);
    const reply = { state: "idle", pending_swarms: false };
    release(reply);
    expect(await Promise.all(calls)).toEqual(Array(6).fill(reply));
    // A later read is a fresh request, never a cached answer.
    void api.getSessionState({ sessionId: "s1" });
    expect(getJSON).toHaveBeenCalledTimes(2);
  });

  it("does not merge different sessions or state-changing reads", () => {
    getJSON.mockImplementation(() => new Promise(() => {}));
    void api.getSessionState({ sessionId: "a" });
    void api.getSessionState({ sessionId: "b" });
    void api.getSessionState({ sessionId: "a", consumeResume: true });
    void api.getSessionState({ sessionId: "a", consumeResume: true });
    expect(getJSON).toHaveBeenCalledTimes(4);
  });

  it("shares config and workspace reads the same way", () => {
    getJSON.mockImplementation(() => new Promise(() => {}));
    void api.config("s1"); void api.config("s1"); void api.getWorkspace(); void api.getWorkspace();
    expect(getJSON).toHaveBeenCalledTimes(2);
  });
});
