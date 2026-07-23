import { describe, expect, it, vi, beforeEach } from "vitest";
import {
  isExternalUrl,
  looksLikeFilePath,
  parseFileHref,
  looksLikePathInlineCode,
  classifyActionGoal,
  autolinkAgentText,
  openAgentLink,
  openAgentFile,
  openAgentUrl,
  openAgentCommand,
} from "../lib/agentLinks";

describe("agentLinks detection", () => {
  it("classifies urls and paths", () => {
    expect(isExternalUrl("https://example.com/a")).toBe(true);
    expect(isExternalUrl("file:///C:/x.ts")).toBe(false);
    expect(looksLikeFilePath("webapp/src/App.tsx")).toBe(true);
    expect(looksLikeFilePath("C:\\Ashita\\addons\\kotoba\\translator.py")).toBe(true);
    expect(looksLikeFilePath("./foo/bar.py:12")).toBe(true);
    expect(looksLikeFilePath("https://x.com")).toBe(false);
    expect(looksLikeFilePath("mailto:a@b.c")).toBe(false);
  });

  it("parses line:col suffixes", () => {
    expect(parseFileHref("src/main.py:10")).toEqual({
      path: "src/main.py",
      line: 10,
      col: undefined,
    });
    expect(parseFileHref("src/main.py:10:4")).toEqual({
      path: "src/main.py",
      line: 10,
      col: 4,
    });
    expect(parseFileHref("file:///C:/proj/a.ts")).toEqual({
      path: "C:/proj/a.ts",
      line: undefined,
      col: undefined,
    });
  });

  it("detects path-like inline code", () => {
    expect(looksLikePathInlineCode("harness/server.py")).toBe(true);
    expect(looksLikePathInlineCode("foo.py:3")).toBe(true);
    expect(looksLikePathInlineCode("--flag")).toBe(false);
    expect(looksLikePathInlineCode("npm install")).toBe(false);
  });

  it("classifies action goals by kind", () => {
    expect(classifyActionGoal("read_file", "a/b.ts")).toEqual({
      linkKind: "file",
      value: "a/b.ts",
    });
    expect(classifyActionGoal("web_fetch", "https://x.com")).toEqual({
      linkKind: "url",
      value: "https://x.com",
    });
    expect(classifyActionGoal("run_command", "pytest -q")).toEqual({
      linkKind: "command",
      value: "pytest -q",
    });
    // Worker goals often embed paths — never open the file editor for them.
    expect(classifyActionGoal(
      "run_implement",
      "Prefer C:\\Users\\pwall\\.marionette\\marionette over parent",
    )).toEqual({
      linkKind: "command",
      value: "Prefer C:\\Users\\pwall\\.marionette\\marionette over parent",
    });
    expect(classifyActionGoal(
      "run_parallel",
      "audit harness/send_loop_dispatch.py mode=analysis",
    ).linkKind).toBe("command");
  });
});

describe("autolinkAgentText", () => {
  it("wraps bare urls and paths outside fences", () => {
    const src = "See https://example.com/docs and webapp/src/App.tsx please.";
    const out = autolinkAgentText(src);
    expect(out).toContain("[https://example.com/docs](https://example.com/docs)");
    expect(out).toContain("[`webapp/src/App.tsx`](webapp/src/App.tsx)");
  });

  it("skips fenced code and existing links", () => {
    const src = "```\npath/to/x.py\n```\nAlready [ok](src/a.ts) and `keep/me.py`.";
    const out = autolinkAgentText(src);
    expect(out).toContain("```\npath/to/x.py\n```");
    expect(out).toContain("[ok](src/a.ts)");
    expect(out).toContain("`keep/me.py`");
  });
});

describe("openAgentLink events", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("dispatches browser events for urls", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    openAgentUrl("https://example.com");
    expect(spy).toHaveBeenCalled();
    const kinds = spy.mock.calls.map((c) => (c[0] as CustomEvent).type);
    expect(kinds).toContain("harness-focus-tab");
    expect(kinds).toContain("harness-open-url");
  });

  it("dispatches open-file for paths", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    openAgentFile("src/a.ts:9");
    const ev = spy.mock.calls
      .map((c) => c[0] as CustomEvent)
      .find((e) => e.type === "harness-open-file");
    expect(ev?.detail).toEqual({ path: "src/a.ts", line: 9, col: undefined });
  });

  it("dispatches terminal focus and optional run", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    openAgentCommand("ls", { run: true });
    const kinds = spy.mock.calls.map((c) => (c[0] as CustomEvent).type);
    expect(kinds).toContain("harness-focus-tab");
    expect(kinds).toContain("harness-run-command");
  });

  it("openAgentLink routes url vs file", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    const prevent = vi.fn();
    openAgentLink("https://x.com", { preventDefault: prevent });
    expect(prevent).toHaveBeenCalled();
    openAgentLink("foo/bar.ts", { preventDefault: prevent });
    const types = spy.mock.calls.map((c) => (c[0] as CustomEvent).type);
    expect(types).toContain("harness-open-url");
    expect(types).toContain("harness-open-file");
  });
});
