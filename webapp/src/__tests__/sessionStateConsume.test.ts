import { afterEach, describe, expect, it, vi } from "vitest";
import { getJSON, withToken } from "../lib/transport";

vi.mock("../lib/transport", async () => {
  const actual = await vi.importActual<typeof import("../lib/transport")>("../lib/transport");
  return {
    ...actual,
    getJSON: vi.fn().mockResolvedValue({ state: "idle", pending_swarms: false }),
  };
});

describe("api.getSessionState consume_resume query", () => {
  afterEach(() => {
    vi.mocked(getJSON).mockClear();
  });

  it("peeks by default and consumes only when consumeResume is set", async () => {
    const { api } = await import("../lib/api");
    await api.getSessionState();
    expect(getJSON).toHaveBeenCalledWith(withToken("/api/session/state"));
    await api.getSessionState({ consumeResume: true });
    expect(getJSON).toHaveBeenCalledWith(withToken("/api/session/state?consume_resume=1"));
  });

  it("rearms latch via rearm_resume query", async () => {
    const { api } = await import("../lib/api");
    await api.getSessionState({ rearmResume: true });
    expect(getJSON).toHaveBeenCalledWith(withToken("/api/session/state?rearm_resume=1"));
  });

  it("threads session_id for session-scoped latch peek/consume/rearm", async () => {
    const { api } = await import("../lib/api");
    await api.getSessionState({ sessionId: "sess-a" });
    expect(getJSON).toHaveBeenCalledWith(
      withToken("/api/session/state?session_id=sess-a"),
    );
    await api.getSessionState({ consumeResume: true, sessionId: "sess-a" });
    expect(getJSON).toHaveBeenCalledWith(
      withToken("/api/session/state?consume_resume=1&session_id=sess-a"),
    );
    await api.getSessionState({ rearmResume: true, sessionId: "sess-b" });
    expect(getJSON).toHaveBeenCalledWith(
      withToken("/api/session/state?rearm_resume=1&session_id=sess-b"),
    );
  });
});
