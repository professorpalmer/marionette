import { describe, expect, it, beforeEach } from "vitest";
import {
  getAgentCommandIndexVersion,
  listAgentCommandSessions,
  lookupAgentCommandSession,
  lookupAgentCommandSessionById,
  normalizeCommandKey,
  registerAgentCommandSession,
  _resetAgentCommandIndexForTests,
} from "../lib/agentCommandIndex";

describe("agentCommandIndex", () => {
  beforeEach(() => {
    _resetAgentCommandIndexForTests();
  });

  it("normalizes $ prefix and whitespace", () => {
    expect(normalizeCommandKey("  $   git   pull  ")).toBe("git pull");
  });

  it("looks up the latest session for a command", () => {
    registerAgentCommandSession({ id: "old", command: "git pull", output: "a", state: "done" });
    registerAgentCommandSession({ id: "live", command: "$ git pull", output: "b", state: "running" });
    const found = lookupAgentCommandSession("git pull");
    expect(found?.id).toBe("live");
    expect(found?.output).toBe("b");
    expect(found?.state).toBe("running");
    expect(lookupAgentCommandSessionById("old")?.command).toBe("git pull");
  });

  it("keeps the latest terminal state and lists sessions newest-first", () => {
    registerAgentCommandSession({ id: "card-1", command: "pytest -q", state: "running" });
    registerAgentCommandSession({ id: "card-2", command: "npm test", state: "failed" });
    registerAgentCommandSession({ id: "card-1", command: "pytest -q", output: "ok\n", state: "done" });
    const sessions = listAgentCommandSessions();
    expect(sessions.map((s) => s.id)).toEqual(["card-1", "card-2"]);
    expect(lookupAgentCommandSessionById("card-1")?.state).toBe("done");
    expect(lookupAgentCommandSessionById("card-2")?.state).toBe("failed");
  });

  it("does not bump version on streaming output for the same id", () => {
    registerAgentCommandSession({ id: "card-1", command: "pytest -q" });
    const v = getAgentCommandIndexVersion();
    registerAgentCommandSession({ id: "card-1", command: "pytest -q", output: "ok\n" });
    registerAgentCommandSession({ id: "card-1", command: "pytest -q", output: "ok\nmore\n" });
    expect(getAgentCommandIndexVersion()).toBe(v);
    expect(lookupAgentCommandSession("pytest -q")?.output).toBe("ok\nmore\n");
  });

  it("ignores empty and overlong commands", () => {
    expect(registerAgentCommandSession({ id: "x", command: "   " })).toBeNull();
    expect(registerAgentCommandSession({ id: "x", command: "n".repeat(501) })).toBeNull();
    expect(lookupAgentCommandSession("echo hi")).toBeNull();
  });
});
