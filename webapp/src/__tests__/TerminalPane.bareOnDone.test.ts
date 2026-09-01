import { describe, expect, it } from "vitest";
import {
  decodeTerminalStreamEvent,
  terminalBareOnDoneAction,
  terminalMissingSessionAction,
  terminalNotice,
  terminalStreamPath,
} from "../components/terminalStreamPolicy";

describe("terminalBareOnDoneAction", () => {
  const base = {
    disposed: false,
    sawExit: false,
    hasSession: true,
    sawOutput: true,
    autoRecovered: false,
  };

  it("reattaches on bare onDone after ConPTY output (no kill)", () => {
    expect(terminalBareOnDoneAction(base)).toBe("reattach");
  });

  it("auto-recovers once on empty first stream", () => {
    expect(
      terminalBareOnDoneAction({ ...base, sawOutput: false, autoRecovered: false }),
    ).toBe("auto_recover");
  });

  it("marks exited after a second empty-stream close", () => {
    expect(
      terminalBareOnDoneAction({ ...base, sawOutput: false, autoRecovered: true }),
    ).toBe("mark_exited");
  });

  it("marks exited after kind:exit settled", () => {
    expect(terminalBareOnDoneAction({ ...base, sawExit: true })).toBe("mark_exited");
  });

  it("marks exited when the session id is already cleared", () => {
    expect(terminalBareOnDoneAction({ ...base, hasSession: false })).toBe("mark_exited");
  });

  it("noops when the pane effect already disposed", () => {
    expect(terminalBareOnDoneAction({ ...base, disposed: true })).toBe("noop");
  });

  it("decodes lifecycle frames and bounds backend notices", () => {
    expect(decodeTerminalStreamEvent({ kind: "process_exit", offset: 4, error: "done" })).toEqual({
      kind: "process_exit", offset: 4, error: "done",
    });
    expect(decodeTerminalStreamEvent({ kind: "exit" }).kind).toBe("legacy_exit");
    expect(terminalMissingSessionAction(false)).toBe("auto_recover");
    expect(terminalMissingSessionAction(true)).toBe("mark_exited");
    expect(terminalNotice("x".repeat(300))).toHaveLength(240);
    expect(terminalStreamPath("abc")).toBe("/api/terminal/stream?id=abc");
    expect(terminalStreamPath("abc", 12)).toBe("/api/terminal/stream?id=abc&offset=12");
    expect(terminalStreamPath("abc", -4)).toBe("/api/terminal/stream?id=abc");
  });
});
