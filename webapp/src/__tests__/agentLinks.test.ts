import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import {
  isExternalUrl,
  looksLikeFilePath,
  looksLikeShellCommand,
  looksLikeJobId,
  parseFileHref,
  looksLikePathInlineCode,
  classifyActionGoal,
  autolinkAgentText,
  openAgentLink,
  openAgentFile,
  openAgentUrl,
  openAgentCommand,
  openAgentImage,
  openAgentWorkspace,
  openAgentSwarmJob,
  stableCommandId,
} from "../lib/agentLinks";
import { _resetAgentTerminalStreamForTests } from "../lib/agentTerminalStream";

describe("agentLinks detection", () => {
  it("classifies urls and paths", () => {
    expect(isExternalUrl("https://example.com/a")).toBe(true);
    expect(isExternalUrl("file:///C:/x.ts")).toBe(false);
    expect(looksLikeFilePath("webapp/src/App.tsx")).toBe(true);
    expect(looksLikeFilePath("C:\\Ashita\\addons\\kotoba\\translator.py")).toBe(true);
    expect(looksLikeFilePath("C:\\a\\b.py")).toBe(true);
    expect(looksLikeFilePath("./foo/bar.py:12")).toBe(true);
    expect(looksLikeFilePath("https://x.com")).toBe(false);
    expect(looksLikeFilePath("mailto:a@b.c")).toBe(false);
  });

  it("rejects shell-like tokens as file paths", () => {
    expect(looksLikeShellCommand("npm.cmd")).toBe(true);
    expect(looksLikeShellCommand("pytest -q")).toBe(true);
    expect(looksLikeShellCommand("git status")).toBe(true);
    expect(looksLikeShellCommand("python setup.py")).toBe(true);
    expect(looksLikeFilePath("npm.cmd")).toBe(false);
    expect(looksLikeFilePath("pytest -q")).toBe(false);
    expect(looksLikeFilePath("git status")).toBe(false);
    expect(looksLikeFilePath("python setup.py")).toBe(false);
    expect(looksLikePathInlineCode("npm.cmd")).toBe(false);
  });

  it("treats spaced filesystem paths as files, not shell commands", () => {
    expect(looksLikeShellCommand("/Users/me/My Projects/app.ts")).toBe(false);
    expect(looksLikeFilePath("/Users/me/My Projects/app.ts")).toBe(true);
    expect(looksLikeFilePath('"/Users/me/My Projects/app.ts"')).toBe(true);
    expect(parseFileHref("/Users/me/My Projects/app.ts:12")).toEqual({
      path: "/Users/me/My Projects/app.ts",
      line: 12,
      col: undefined,
    });
    expect(parseFileHref('"/Users/me/My Projects/app.ts"')).toEqual({
      path: "/Users/me/My Projects/app.ts",
      line: undefined,
      col: undefined,
    });
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
    expect(classifyActionGoal("run_ipython", "df.head()")).toEqual({
      linkKind: "command",
      value: "df.head()",
    });
    expect(classifyActionGoal("view_image", "uploads/shot.png")).toEqual({
      linkKind: "image",
      value: "uploads/shot.png",
    });
    expect(classifyActionGoal("open_project", "C:\\Users\\me\\proj")).toEqual({
      linkKind: "workspace",
      value: "C:\\Users\\me\\proj",
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
    // Unknown kinds: shell-like never falls through to file.
    expect(classifyActionGoal("custom_tool", "npm.cmd").linkKind).toBe("command");
    expect(classifyActionGoal("custom_tool", "git status").linkKind).toBe("command");
    expect(classifyActionGoal("custom_tool", "webapp/src/App.tsx").linkKind).toBe("file");
  });

  it("detects shell-like inline code separately from paths", () => {
    expect(looksLikeShellCommand("pytest -q")).toBe(true);
    expect(looksLikeShellCommand("npm.cmd")).toBe(true);
    expect(looksLikePathInlineCode("pytest -q")).toBe(false);
    expect(looksLikePathInlineCode("harness/foo.py")).toBe(true);
    expect(isExternalUrl("https://example.com/a")).toBe(true);
  });

  it("recognizes durable and local swarm job ids", () => {
    expect(looksLikeJobId("job_abcdef012345")).toBe(true);
    expect(looksLikeJobId("job_DEADBEEF1234")).toBe(true);
    expect(looksLikeJobId("local-swarm-a1")).toBe(true);
    expect(looksLikeJobId("local-bf1b30f4")).toBe(true);
    expect(looksLikeJobId("local-cmd-bg")).toBe(true);
    expect(looksLikeJobId("local-cmdbatch-xyz")).toBe(true);
    expect(looksLikeJobId("local-x")).toBe(true);
    // Reject random hex, UUIDs, paths, and malformed durable ids.
    expect(looksLikeJobId("abcdef012345")).toBe(false);
    expect(looksLikeJobId("deadbeef")).toBe(false);
    expect(looksLikeJobId("550e8400-e29b-41d4-a716-446655440000")).toBe(false);
    expect(looksLikeJobId("job_short")).toBe(false);
    expect(looksLikeJobId("job_abcdef01234567")).toBe(false);
    expect(looksLikeJobId("webapp/src/App.tsx")).toBe(false);
    expect(looksLikeJobId("C:\\tmp\\job")).toBe(false);
    expect(classifyActionGoal("job", "job_abcdef012345")).toEqual({
      linkKind: "job",
      value: "job_abcdef012345",
    });
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
    _resetAgentTerminalStreamForTests();
  });

  afterEach(() => {
    _resetAgentTerminalStreamForTests();
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

  it("dispatches terminal focus and run for interactive inject", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    openAgentCommand("ls", { run: true });
    const kinds = spy.mock.calls.map((c) => (c[0] as CustomEvent).type);
    expect(kinds).toContain("harness-focus-tab");
    expect(kinds).toContain("harness-run-command");
    expect(kinds).not.toContain("harness-open-agent-terminal");
  });

  it("reveals agent terminal on default command click", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    openAgentCommand("pytest -q", { id: "card-1", output: "ok\n", run: false });
    const kinds = spy.mock.calls.map((c) => (c[0] as CustomEvent).type);
    expect(kinds).toContain("harness-focus-tab");
    expect(kinds).toContain("harness-open-agent-terminal");
    expect(kinds).not.toContain("harness-run-command");
    const ev = spy.mock.calls
      .map((c) => c[0] as CustomEvent)
      .find((e) => e.type === "harness-open-agent-terminal");
    expect(ev?.detail).toEqual({
      id: "card-1",
      command: "pytest -q",
      output: "ok\n",
    });
  });

  it("hashes command when reveal id is omitted", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    openAgentCommand("echo hi");
    const ev = spy.mock.calls
      .map((c) => c[0] as CustomEvent)
      .find((e) => e.type === "harness-open-agent-terminal");
    expect(ev?.detail?.id).toBe(stableCommandId("echo hi"));
    expect(ev?.detail?.command).toBe("echo hi");
  });

  it("openAgentLink routes url vs file vs shell", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    const prevent = vi.fn();
    openAgentLink("https://x.com", { preventDefault: prevent });
    expect(prevent).toHaveBeenCalled();
    openAgentLink("foo/bar.ts", { preventDefault: prevent });
    openAgentLink("npm.cmd", { preventDefault: prevent });
    const types = spy.mock.calls.map((c) => (c[0] as CustomEvent).type);
    expect(types).toContain("harness-open-url");
    expect(types).toContain("harness-open-file");
    expect(types).toContain("harness-open-agent-terminal");
    expect(types).not.toContain("harness-run-command");
  });

  it("dispatches lightbox event for images", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    openAgentImage("https://cdn.example.com/a.png");
    openAgentImage("uploads/shot.png");
    const events = spy.mock.calls
      .map((c) => c[0] as CustomEvent)
      .filter((e) => e.type === "harness-open-image");
    expect(events).toHaveLength(2);
    expect(events[0]?.detail).toEqual({
      path: "https://cdn.example.com/a.png",
      url: "https://cdn.example.com/a.png",
    });
    expect(events[1]?.detail).toEqual({
      path: "uploads/shot.png",
      url: undefined,
    });
  });

  it("dispatches workspace open event", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    openAgentWorkspace("C:\\Users\\me\\proj");
    const ev = spy.mock.calls
      .map((c) => c[0] as CustomEvent)
      .find((e) => e.type === "harness-open-workspace");
    expect(ev?.detail).toEqual({ path: "C:\\Users\\me\\proj" });
  });

  it("openAgentSwarmJob focuses swarm tab then opens the job", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    openAgentSwarmJob("job_abcdef012345");
    const events = spy.mock.calls.map((c) => c[0] as CustomEvent);
    const kinds = events.map((e) => e.type);
    expect(kinds).toEqual(["harness-focus-tab", "harness-open-swarm-job"]);
    expect(events[0]?.detail).toBe("swarm");
    expect(events[1]?.detail).toEqual({ jobId: "job_abcdef012345" });
  });

  it("openAgentSwarmJob queues the job id before dispatch for late SwarmPane mount", async () => {
    const { peekPendingSwarmOpenJob, clearPendingSwarmOpenJob } = await import(
      "../lib/pendingSwarmOpenJob"
    );
    clearPendingSwarmOpenJob();
    openAgentSwarmJob("local-swarm-a1");
    expect(peekPendingSwarmOpenJob()).toBe("local-swarm-a1");
    clearPendingSwarmOpenJob();
  });

  it("openAgentLink routes job ids to the swarm tracker", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    const prevent = vi.fn();
    openAgentLink("local-swarm-a1", { preventDefault: prevent });
    expect(prevent).toHaveBeenCalled();
    const kinds = spy.mock.calls.map((c) => (c[0] as CustomEvent).type);
    expect(kinds).toContain("harness-focus-tab");
    expect(kinds).toContain("harness-open-swarm-job");
  });

  it("does not autolink bare job tokens in markdown prose", () => {
    const src = "Dispatched job_abcdef012345 and local-swarm-a1 already.";
    const out = autolinkAgentText(src);
    expect(out).toBe(src);
    expect(out).not.toContain("](job_");
    expect(out).not.toContain("](local-");
  });
});
