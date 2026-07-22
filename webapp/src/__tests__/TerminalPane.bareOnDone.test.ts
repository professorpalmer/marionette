import { describe, expect, it } from "vitest";
import { terminalBareOnDoneAction } from "../components/terminalStreamPolicy";

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
});
