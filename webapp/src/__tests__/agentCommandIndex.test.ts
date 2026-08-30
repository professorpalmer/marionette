import { describe, expect, it, beforeEach, vi } from "vitest";
import {
  getAgentCommandIndexVersion,
  dismissAgentCommandSession,
  listAgentCommandSessions,
  lookupAgentCommandSession,
  lookupAgentCommandSessionById,
  normalizeCommandKey,
  registerAgentCommandSession,
  _resetAgentCommandIndexForTests,
} from "../lib/agentCommandIndex";

describe("agentCommandIndex", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
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

  it("does not restamp updatedAt when transcript re-indexes a settled command", () => {
    const now = vi.spyOn(Date, "now")
      .mockReturnValueOnce(100)
      .mockReturnValueOnce(200);
    registerAgentCommandSession({ id: "card-1", command: "brew install llama.cpp", state: "done" });
    const first = lookupAgentCommandSessionById("card-1")!.updatedAt;
    registerAgentCommandSession({
      id: "card-1",
      command: "brew install llama.cpp",
      output: "already installed\n",
      state: "done",
    });
    expect(lookupAgentCommandSessionById("card-1")?.updatedAt).toBe(first);
    expect(lookupAgentCommandSessionById("card-1")?.railVisible).toBe(false);
    now.mockRestore();
  });

  it("keeps a completed command rail-visible when it was observed running", () => {
    registerAgentCommandSession({ id: "card-1", command: "brew install llama.cpp", state: "running" });
    registerAgentCommandSession({ id: "card-1", command: "brew install llama.cpp", state: "done" });
    expect(lookupAgentCommandSessionById("card-1")?.railVisible).toBe(true);
  });

  it("does not regress a settled command to running", () => {
    registerAgentCommandSession({ id: "card-1", command: "brew install llama.cpp", state: "done" });
    const settledAt = lookupAgentCommandSessionById("card-1")!.updatedAt;
    registerAgentCommandSession({ id: "card-1", command: "brew install llama.cpp", state: "running" });
    expect(lookupAgentCommandSessionById("card-1")).toMatchObject({
      state: "done",
      railVisible: false,
      updatedAt: settledAt,
    });
  });

  it("ignores empty and overlong commands", () => {
    expect(registerAgentCommandSession({ id: "x", command: "   " })).toBeNull();
    expect(registerAgentCommandSession({ id: "x", command: "n".repeat(501) })).toBeNull();
    expect(lookupAgentCommandSession("echo hi")).toBeNull();
  });

  it("lists only the active chat when sessionId is passed", () => {
    registerAgentCommandSession({
      id: "old-chat",
      command: "git checkout -- tsconfig.json",
      state: "done",
      sessionId: "sess-old",
    });
    registerAgentCommandSession({
      id: "new-chat",
      command: "echo hi",
      state: "running",
      sessionId: "sess-new",
    });
    registerAgentCommandSession({
      id: "orphan",
      command: "git status",
      state: "done",
    });
    expect(listAgentCommandSessions("sess-new").map((s) => s.id)).toEqual(["new-chat"]);
    expect(listAgentCommandSessions("")).toEqual([]);
    expect(listAgentCommandSessions().map((s) => s.id).sort()).toEqual([
      "new-chat",
      "old-chat",
      "orphan",
    ]);
  });

  it("keeps sessionId on streaming updates so a later list still scopes", () => {
    registerAgentCommandSession({
      id: "cmd-1",
      command: "pytest -q",
      sessionId: "sess-1",
    });
    registerAgentCommandSession({ id: "cmd-1", command: "pytest -q", output: "ok\n" });
    expect(lookupAgentCommandSessionById("cmd-1")?.sessionId).toBe("sess-1");
    expect(listAgentCommandSessions("sess-1").map((s) => s.id)).toEqual(["cmd-1"]);
  });

  it("drops a dismissed session so X can clear a phantom Term row", () => {
    registerAgentCommandSession({
      id: "cg-1",
      command: "custom OpenAI-compatible provider API base URL",
      state: "running",
      sessionId: "sess-1",
    });
    expect(dismissAgentCommandSession("cg-1")).toBe(true);
    expect(lookupAgentCommandSessionById("cg-1")).toBeNull();
    expect(listAgentCommandSessions("sess-1")).toEqual([]);
  });
});
