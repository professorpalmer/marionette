import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import {
  activateTerminalLink,
  findTerminalPathMatches,
  registerTerminalPathLinks,
} from "../lib/terminalPathLinks";

describe("findTerminalPathMatches", () => {
  it("matches relative workspace paths with optional :line[:col]", () => {
    const matches = findTerminalPathMatches("error in harness/foo.py:12:3 next");
    expect(matches.map((m) => m.text)).toEqual(["harness/foo.py:12:3"]);
    expect(matches[0].start).toBeGreaterThanOrEqual(0);
    expect(matches[0].end).toBe(matches[0].start + "harness/foo.py:12:3".length);
  });

  it("matches absolute and ./ relative paths", () => {
    const abs = findTerminalPathMatches("/Users/me/proj/src/app.ts");
    expect(abs.map((m) => m.text)).toContain("/Users/me/proj/src/app.ts");
    const rel = findTerminalPathMatches("see ./webapp/src/main.tsx");
    expect(rel.map((m) => m.text)).toContain("./webapp/src/main.tsx");
  });

  it("matches file:// paths by widening the prefix", () => {
    const matches = findTerminalPathMatches("open file:///tmp/demo.py please");
    expect(matches.some((m) => m.text.startsWith("file://") && m.text.endsWith("demo.py"))).toBe(
      true,
    );
  });

  it("skips shell launchers and plain prose", () => {
    expect(findTerminalPathMatches("running npm.cmd")).toEqual([]);
    expect(findTerminalPathMatches("ok exit 0")).toEqual([]);
  });

  it("matches quoted and unquoted spaced macOS paths", () => {
    const spaced = findTerminalPathMatches(
      "error in /Users/me/My Projects/app.ts:12 next",
    );
    expect(spaced.map((m) => m.text)).toContain("/Users/me/My Projects/app.ts:12");

    const quoted = findTerminalPathMatches(
      'trace "/Users/me/My Projects/app.ts" please',
    );
    expect(quoted.map((m) => m.text)).toContain("/Users/me/My Projects/app.ts");
    // Underline skips wrapping quotes.
    expect(quoted[0].start).toBeGreaterThan(
      'trace "'.length - 1,
    );
  });

  it("does not link-spam ordinary prose with spaces", () => {
    expect(findTerminalPathMatches("see my file please")).toEqual([]);
    expect(findTerminalPathMatches("running pytest -q")).toEqual([]);
  });
});

describe("activateTerminalLink", () => {
  beforeEach(() => {
    vi.spyOn(window, "dispatchEvent").mockImplementation(() => true);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("opens https URLs via harness-open-url", () => {
    activateTerminalLink("https://example.com/docs");
    const calls = (window.dispatchEvent as ReturnType<typeof vi.fn>).mock.calls.map(
      (c) => (c[0] as CustomEvent).type,
    );
    expect(calls).toContain("harness-open-url");
  });

  it("opens file paths via harness-open-file", () => {
    activateTerminalLink("harness/review_memory.py:30");
    const ev = (window.dispatchEvent as ReturnType<typeof vi.fn>).mock.calls
      .map((c) => c[0] as CustomEvent)
      .find((e) => e.type === "harness-open-file");
    expect(ev).toBeTruthy();
    expect((ev as CustomEvent).detail.path).toBe("harness/review_memory.py");
    expect((ev as CustomEvent).detail.line).toBe(30);
  });
});

describe("registerTerminalPathLinks", () => {
  it("registers an ILinkProvider that surfaces path matches", () => {
    const providers: Array<{ provideLinks: Function }> = [];
    const lineText = "trace harness/foo.py:12";
    const term = {
      buffer: {
        active: {
          getLine: () => ({
            translateToString: () => lineText,
          }),
        },
      },
      registerLinkProvider: (provider: { provideLinks: Function }) => {
        providers.push(provider);
        return { dispose: () => undefined };
      },
    };

    registerTerminalPathLinks(term as any);
    expect(providers).toHaveLength(1);

    let links: Array<{ text: string }> | undefined;
    providers[0].provideLinks(1, (result: Array<{ text: string }> | undefined) => {
      links = result;
    });
    expect(links?.map((l) => l.text)).toEqual(["harness/foo.py:12"]);
  });
});
