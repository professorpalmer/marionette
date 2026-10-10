import { describe, expect, it } from "vitest";
import { Terminal } from "@xterm/xterm";
import { AGENT_TERMINAL_OPTIONS } from "../components/TerminalPane";

function rows(term: Terminal, count: number): string[] {
  const out: string[] = [];
  for (let i = 0; i < count; i += 1) {
    out.push(term.buffer.active.getLine(i)?.translateToString(true) ?? "");
  }
  return out;
}

describe("agent terminal mirror", () => {
  it("starts each line of piped output at column 0", async () => {
    const term = new Terminal(AGENT_TERMINAL_OPTIONS);
    const piped = "$ git status --short\r\n M a.ts\n M b.ts\n?? c.txt\n";
    await new Promise<void>((resolve) => term.write(piped, resolve));
    expect(rows(term, 4)).toEqual(["$ git status --short", " M a.ts", " M b.ts", "?? c.txt"]);
  });
});
