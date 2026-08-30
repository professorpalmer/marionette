import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import {
  isExternalUrl,
  looksLikeFilePath,
  looksLikeShellCommand,
  looksLikeJobId,
  looksLikeSpillUri,
  parseFileHref,
  looksLikePathInlineCode,
  classifyActionGoal,
  classifyTranscriptHref,
  commandMarkdownHref,
  fileMarkdownHref,
  autolinkAgentText,
  openAgentLink,
  openAgentFile,
  openAgentUrl,
  openAgentCommand,
  openAgentImage,
  openAgentWorkspace,
  openAgentBusyDetail,
  openAgentSwarmJob,
  openAgentSpill,
} from "../lib/agentLinks";
import { _resetAgentTerminalStreamForTests } from "../lib/agentTerminalStream";
import {
  registerAgentCommandSession,
  _resetAgentCommandIndexForTests,
} from "../lib/agentCommandIndex";

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
    expect(parseFileHref("src/main.py:165-210")).toEqual({
      path: "src/main.py",
      line: 165,
      col: undefined,
    });
    expect(parseFileHref("file:///C:/proj/a.ts")).toEqual({
      path: "C:/proj/a.ts",
      line: undefined,
      col: undefined,
    });
  });

  it("detects path-like inline code", () => {
    expect(looksLikePathInlineCode("harness/server.py")).toBe(true);
    expect(looksLikePathInlineCode("foo.py:3")).toBe(false);
    expect(looksLikePathInlineCode("src/foo.py:3")).toBe(true);
    expect(looksLikePathInlineCode("--flag")).toBe(false);
    expect(looksLikePathInlineCode("npm install")).toBe(false);
    expect(looksLikePathInlineCode("/Users/me/My Projects/app.ts")).toBe(true);
    expect(looksLikePathInlineCode("/Users/me/My Projects/app.ts:12")).toBe(true);
    expect(looksLikePathInlineCode("backend.py")).toBe(false);
    expect(looksLikePathInlineCode("job_thread_id")).toBe(false);
    expect(looksLikePathInlineCode("Starting")).toBe(false);
  });

  it("does not treat package specs as file editor links", () => {
    const packages = [
      "@anysphere/ui",
      "@anysphere/agent-store-sync",
      "@cursor/july@0.1.40",
      "@base-ui/react@1.6.0",
      "@stylexjs/stylex@0.18.3",
      "@tanstack/react-virtual@3.13.23",
      "tar@6.2.1",
      "lodash@4.17.21",
      "@/lib/utils",
    ];
    for (const spec of packages) {
      expect(looksLikeFilePath(spec)).toBe(false);
      expect(looksLikePathInlineCode(spec)).toBe(false);
    }
    expect(looksLikeFilePath("anysphere/ui")).toBe(false);
    expect(looksLikePathInlineCode("anysphere/ui")).toBe(false);
    expect(looksLikeFilePath("package.json")).toBe(true);
    expect(looksLikePathInlineCode("package.json")).toBe(false);
    expect(looksLikeFilePath("~/Downloads/security-assessment-report.md")).toBe(true);
    expect(looksLikePathInlineCode("~/Downloads/security-assessment-report.md")).toBe(true);
    expect(looksLikeFilePath("/tmp/@scope/pkg/index.ts")).toBe(true);
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
    expect(classifyActionGoal(
      "run_implement",
      "Prefer C:\\Users\\pwall\\.marionette\\marionette over parent",
    ).linkKind).toBe("none");
    expect(classifyActionGoal(
      "run_parallel",
      "audit harness/send_loop_dispatch.py mode=analysis",
    ).linkKind).toBe("none");
    expect(classifyActionGoal("run_swarm", "audit composer terminal ownership").linkKind).toBe("none");
    expect(classifyActionGoal("route_task", "implement the terminal rail fix").linkKind).toBe("none");
    expect(classifyActionGoal(
      "search_codegraph",
      "custom OpenAI-compatible provider API base URL model configuration settings",
    ).linkKind).toBe("none");
    expect(classifyActionGoal("search_files", "base_url|api_base").linkKind).toBe("none");
    expect(classifyActionGoal("query_wiki", "terminal process list").linkKind).toBe("none");
    expect(classifyActionGoal("custom_tool", "npm.cmd").linkKind).toBe("command");
    expect(classifyActionGoal("custom_tool", "git status").linkKind).toBe("command");
    expect(classifyActionGoal("custom_tool", "webapp/src/App.tsx").linkKind).toBe("file");
  });

  it("does not classify swarm, parallel, implement, or route cards as command", () => {
    for (const kind of ["run_swarm", "run_parallel", "run_implement", "route_task"] as const) {
      expect(classifyActionGoal(kind, "pytest -q").linkKind, kind).toBe("none");
      expect(classifyActionGoal(kind, "sleep 999").linkKind, kind).toBe("none");
    }
    expect(classifyActionGoal("run_command", "pytest -q").linkKind).toBe("command");
    expect(classifyActionGoal("search_codegraph", "pytest -q").linkKind).toBe("none");
    expect(classifyActionGoal("web_search", "pytest -q").linkKind).toBe("none");
    expect(classifyActionGoal("query_wiki", "pytest -q").linkKind).toBe("none");
    expect(classifyActionGoal("puppetmaster_codegraph_search", "pytest -q").linkKind).toBe("none");
    expect(classifyActionGoal("search_symbols", "git status").linkKind).toBe("none");
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

  it("recognizes spill:// URIs and rejects file-path treatment", () => {
    expect(looksLikeSpillUri("spill://sess1/call_a")).toBe(true);
    expect(looksLikeSpillUri("spill://sess_1.call/tool-call_2")).toBe(true);
    expect(looksLikeSpillUri("spill://")).toBe(false);
    expect(looksLikeSpillUri("spill://sess1")).toBe(false);
    expect(looksLikeSpillUri("spill://sess1/evil/extra")).toBe(false);
    expect(looksLikeSpillUri("artifact://x/y")).toBe(false);
    expect(looksLikeFilePath("spill://sess1/call_a")).toBe(false);
    expect(classifyActionGoal("spill", "spill://sess1/call_a")).toEqual({
      linkKind: "spill",
      value: "spill://sess1/call_a",
    });
    expect(classifyActionGoal("", "spill://sess1/call_a")).toEqual({
      linkKind: "spill",
      value: "spill://sess1/call_a",
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

  it("autolinks unquoted spaced filesystem paths without truncating", () => {
    const src = "Open /Users/me/My Projects/app.ts next.";
    const out = autolinkAgentText(src);
    expect(out).toContain(
      "[`/Users/me/My Projects/app.ts`](</Users/me/My Projects/app.ts>)",
    );
    expect(out).not.toContain("[`Projects/app.ts`]");
    expect(out).not.toContain("](Projects/app.ts)");
  });

  it("wraps bare spill:// URIs outside fences", () => {
    const out = autolinkAgentText("Full output at spill://sess1/call_a for recall.");
    expect(out).toContain("[spill://sess1/call_a](spill://sess1/call_a)");
  });

  it("does not autolink package specs in prose", () => {
    const src = "The critical tar@6.2.1 finding and @anysphere/ui stay plain.";
    const out = autolinkAgentText(src);
    expect(out).toBe(src);
    expect(out).not.toContain("](@anysphere");
    expect(out).not.toContain("](tar@");
  });

  it("skips fenced code and existing links", () => {
    const src = "```\npath/to/x.py\n```\nAlready [ok](src/a.ts) and `keep/me.py`.";
    const out = autolinkAgentText(src);
    expect(out).toContain("```\npath/to/x.py\n```");
    expect(out).toContain("[ok](src/a.ts)");
    expect(out).toContain("`keep/me.py`");
  });

  it("does not autolink identifiers or bare filenames", () => {
    const src = "Starting Plan Steps Done job_thread_id backend.py PROGRESS.";
    expect(autolinkAgentText(src)).toBe(src);
  });
});

describe("classifyTranscriptHref", () => {
  it("accepts real urls, jobs, spills, and pathed files", () => {
    expect(classifyTranscriptHref("https://example.com/a")).toBe("url");
    expect(classifyTranscriptHref("spill://sess1/call_a")).toBe("spill");
    expect(classifyTranscriptHref("job_abcdef012345")).toBe("job");
    expect(classifyTranscriptHref("webapp/src/App.tsx")).toBe("file");
    expect(classifyTranscriptHref("src/main.py:165-210")).toBe("file");
  });

  it("rejects model-spam identifiers, commands, and bare filenames", () => {
    expect(classifyTranscriptHref("Starting")).toBe("none");
    expect(classifyTranscriptHref("Plan")).toBe("none");
    expect(classifyTranscriptHref("job_thread_id")).toBe("none");
    expect(classifyTranscriptHref("backend.py")).toBe("none");
    expect(classifyTranscriptHref("git status")).toBe("none");
    expect(classifyTranscriptHref("Starting the job")).toBe("none");
    expect(classifyTranscriptHref("PROGRESS")).toBe("none");
  });

  it("lights a command href only after a live session is registered", () => {
    _resetAgentCommandIndexForTests();
    expect(classifyTranscriptHref("git pull")).toBe("none");
    expect(classifyTranscriptHref(commandMarkdownHref("card-9"))).toBe("none");
    registerAgentCommandSession({ id: "card-9", command: "git pull", output: "ok\n" });
    expect(classifyTranscriptHref("git pull")).toBe("command");
    expect(classifyTranscriptHref("$ git pull")).toBe("command");
    expect(classifyTranscriptHref(commandMarkdownHref("card-9"))).toBe("command");
    expect(classifyTranscriptHref(fileMarkdownHref("webapp/src/App.tsx"))).toBe("file");
    _resetAgentCommandIndexForTests();
  });
});

describe("openAgentLink events", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    _resetAgentTerminalStreamForTests();
    _resetAgentCommandIndexForTests();
  });

  afterEach(() => {
    _resetAgentTerminalStreamForTests();
    _resetAgentCommandIndexForTests();
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

  it("does not mint a blank terminal for speculative command clicks", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    openAgentCommand("echo hi");
    openAgentCommand("Starting the job", { run: false });
    const kinds = spy.mock.calls.map((c) => (c[0] as CustomEvent).type);
    expect(kinds).not.toContain("harness-open-agent-terminal");
    expect(kinds).not.toContain("harness-focus-tab");
  });

  it("prefers the live session over an unregistered job id", () => {
    registerAgentCommandSession({ id: "pty-live", command: "pytest -q", output: "....\\n" });
    const spy = vi.spyOn(window, "dispatchEvent");
    openAgentCommand("pytest -q", { id: "local-cmd-dead", output: "" });
    const ev = spy.mock.calls
      .map((c) => c[0] as CustomEvent)
      .find((e) => e.type === "harness-open-agent-terminal");
    expect(ev?.detail?.id).toBe("pty-live");
    expect(ev?.detail?.id).not.toBe("local-cmd-dead");
  });

  it("opens the registered live session for a later chat command click", () => {
    registerAgentCommandSession({ id: "card-live", command: "git pull", output: "Already up to date.\n" });
    const spy = vi.spyOn(window, "dispatchEvent");
    openAgentCommand("git pull");
    const ev = spy.mock.calls
      .map((c) => c[0] as CustomEvent)
      .find((e) => e.type === "harness-open-agent-terminal");
    expect(ev?.detail).toEqual({
      id: "card-live",
      command: "git pull",
      output: "Already up to date.\n",
    });
  });

  it("openAgentLink routes a registered command href to that terminal", () => {
    registerAgentCommandSession({ id: "card-live", command: "git pull", output: "ok\n" });
    const spy = vi.spyOn(window, "dispatchEvent");
    const prevent = vi.fn();
    openAgentLink("git pull", { preventDefault: prevent });
    expect(prevent).toHaveBeenCalled();
    const ev = spy.mock.calls
      .map((c) => c[0] as CustomEvent)
      .find((e) => e.type === "harness-open-agent-terminal");
    expect(ev?.detail?.id).toBe("card-live");
    expect(spy.mock.calls.map((c) => (c[0] as CustomEvent).type)).not.toContain(
      "harness-run-command",
    );
  });

  it("openAgentLink routes url vs file and ignores dead command/file hrefs", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    const prevent = vi.fn();
    openAgentLink("https://x.com", { preventDefault: prevent });
    expect(prevent).toHaveBeenCalled();
    openAgentLink("foo/bar.ts", { preventDefault: prevent });
    openAgentLink("npm.cmd", { preventDefault: prevent });
    openAgentLink("backend.py", { preventDefault: prevent });
    openAgentLink("Starting the job", { preventDefault: prevent });
    const types = spy.mock.calls.map((c) => (c[0] as CustomEvent).type);
    expect(types).toContain("harness-open-url");
    expect(types).toContain("harness-open-file");
    expect(types).not.toContain("harness-open-agent-terminal");
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

  it("openAgentSpill dispatches harness-open-spill", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    openAgentSpill("spill://sess1/call_a");
    const ev = spy.mock.calls
      .map((c) => c[0] as CustomEvent)
      .find((e) => e.type === "harness-open-spill");
    expect(ev?.detail).toEqual({ uri: "spill://sess1/call_a" });
  });

  it("openAgentLink routes spill:// before file heuristics", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    const preventDefault = vi.fn();
    openAgentLink("spill://sess1/call_a", { preventDefault });
    expect(preventDefault).toHaveBeenCalled();
    const kinds = spy.mock.calls.map((c) => (c[0] as CustomEvent).type);
    expect(kinds).toContain("harness-open-spill");
    expect(kinds).not.toContain("harness-open-file");
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

  it("awaiting-swarm busy chrome opens its exact job instead of Terminal", () => {
    const spy = vi.spyOn(window, "dispatchEvent");

    openAgentBusyDetail("awaiting_swarm", ["job_abcdef012345"]);

    const events = spy.mock.calls.map((c) => c[0] as CustomEvent);
    expect(events.map((event) => event.type)).toEqual([
      "harness-focus-tab",
      "harness-open-swarm-job",
    ]);
    expect(events[0]?.detail).toBe("swarm");
    expect(events[1]?.detail).toEqual({ jobId: "job_abcdef012345" });
    expect(events.some((event) => event.detail === "terminal")).toBe(false);
  });

  it("awaiting-swarm busy chrome skips invalid ids then opens the real job", () => {
    const spy = vi.spyOn(window, "dispatchEvent");

    openAgentBusyDetail("awaiting_swarm", ["not-a-job", "job_abcdef012345"]);

    const events = spy.mock.calls.map((c) => c[0] as CustomEvent);
    expect(events.map((event) => event.type)).toEqual([
      "harness-focus-tab",
      "harness-open-swarm-job",
    ]);
    expect(events[1]?.detail).toEqual({ jobId: "job_abcdef012345" });
    expect(events.some((event) => event.detail === "terminal")).toBe(false);
  });

  it("awaiting-swarm busy chrome without a job id opens Swarm Tracker, not Terminal", () => {
    const spy = vi.spyOn(window, "dispatchEvent");

    openAgentBusyDetail("awaiting_swarm", ["nope"]);

    const events = spy.mock.calls.map((c) => c[0] as CustomEvent);
    expect(events.map((event) => event.type)).toEqual(["harness-focus-tab"]);
    expect(events[0]?.detail).toBe("swarm");
    expect(events.some((event) => event.detail === "terminal")).toBe(false);
    expect(events.some((event) => event.type === "harness-open-swarm-job")).toBe(false);
  });

  it("non-swarm busy chrome still opens Terminal", () => {
    const spy = vi.spyOn(window, "dispatchEvent");

    openAgentBusyDetail("thinking", ["job_abcdef012345"]);

    const events = spy.mock.calls.map((c) => c[0] as CustomEvent);
    expect(events.map((event) => event.type)).toEqual(["harness-focus-tab"]);
    expect(events[0]?.detail).toBe("terminal");
    expect(events.some((event) => event.type === "harness-open-swarm-job")).toBe(false);
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

  it("Cmd/middle-click opens the system browser instead of the in-app pane", () => {
    const openExternal = vi.fn();
    const prev = (window as any).harnessIPC;
    (window as any).harnessIPC = { openExternal };
    const spy = vi.spyOn(window, "dispatchEvent");
    openAgentLink("https://example.com/cmd", { preventDefault: vi.fn(), metaKey: true });
    openAgentLink("https://example.com/mid", { preventDefault: vi.fn(), button: 1 });
    const types = spy.mock.calls.map((c) => (c[0] as CustomEvent).type);
    expect(types).not.toContain("harness-open-url");
    expect(openExternal).toHaveBeenCalledWith("https://example.com/cmd");
    expect(openExternal).toHaveBeenCalledWith("https://example.com/mid");
    if (prev === undefined) delete (window as any).harnessIPC;
    else (window as any).harnessIPC = prev;
  });
});

