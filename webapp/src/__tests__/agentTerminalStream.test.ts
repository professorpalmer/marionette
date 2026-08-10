import { describe, expect, it, beforeEach } from "vitest";
import {
  _agentTerminalBacklogForTests,
  _resetAgentTerminalStreamForTests,
  registerAgentTerminalWriter,
  seedAgentTerminalCommand,
  syncAgentTerminalSnapshot,
  writeAgentTerminalChunk,
} from "../lib/agentTerminalStream";

describe("agentTerminalStream", () => {
  beforeEach(() => {
    _resetAgentTerminalStreamForTests();
  });

  it("seeds a command header once", () => {
    seedAgentTerminalCommand("p1", "pytest -q");
    seedAgentTerminalCommand("p1", "pytest -q");
    expect(_agentTerminalBacklogForTests("p1")).toBe("$ pytest -q\r\n");
  });

  it("syncs output as a delta after the header", () => {
    seedAgentTerminalCommand("p1", "ls");
    syncAgentTerminalSnapshot("p1", "a\n");
    syncAgentTerminalSnapshot("p1", "a\nb\n");
    expect(_agentTerminalBacklogForTests("p1")).toBe("$ ls\r\na\nb\n");
  });

  it("replays backlog to a late-registered writer", () => {
    seedAgentTerminalCommand("p1", "echo hi");
    syncAgentTerminalSnapshot("p1", "hi\n");
    const chunks: string[] = [];
    const unregister = registerAgentTerminalWriter("p1", (c) => chunks.push(c));
    expect(chunks.join("")).toBe("$ echo hi\r\nhi\n");
    writeAgentTerminalChunk("p1", "more\n");
    expect(chunks.at(-1)).toBe("more\n");
    unregister();
  });

  it("caps backlog near 256k", () => {
    seedAgentTerminalCommand("p1", "big");
    const chunk = "x".repeat(200_000);
    writeAgentTerminalChunk("p1", chunk);
    writeAgentTerminalChunk("p1", chunk);
    const backlog = _agentTerminalBacklogForTests("p1");
    expect(backlog.length).toBeLessThanOrEqual(256_000);
    expect(backlog.endsWith("x".repeat(100))).toBe(true);
  });
});
