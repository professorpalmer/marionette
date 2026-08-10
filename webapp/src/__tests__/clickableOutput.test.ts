import { describe, expect, it } from "vitest";
import {
  tokenizeClickableOutput,
  pathTokenInCodeLine,
  isSingleShellCommandFence,
} from "../lib/clickableOutput";

describe("tokenizeClickableOutput", () => {
  it("tokenizes https URLs and file paths with line numbers", () => {
    const segs = tokenizeClickableOutput(
      "see https://example.com/docs and harness/foo.py:12 next\nplain",
    );
    const kinds = segs.map((s) => s.kind);
    expect(kinds).toContain("url");
    expect(kinds).toContain("file");
    expect(kinds).toContain("text");
    const url = segs.find((s) => s.kind === "url");
    expect(url && url.kind === "url" ? url.href : null).toBe("https://example.com/docs");
    const file = segs.find((s) => s.kind === "file");
    expect(file && file.kind === "file" ? file.path : null).toBe("harness/foo.py:12");
  });

  it("leaves non-path text alone", () => {
    const segs = tokenizeClickableOutput("ok\nexit 0\n");
    expect(segs.every((s) => s.kind === "text")).toBe(true);
    expect(segs.map((s) => (s.kind === "text" ? s.text : "")).join("")).toBe("ok\nexit 0\n");
  });

  it("does not treat shell launchers as file paths", () => {
    const segs = tokenizeClickableOutput("running npm.cmd\n");
    expect(segs.every((s) => s.kind === "text")).toBe(true);
  });
});

describe("pathTokenInCodeLine", () => {
  it("extracts tree listing paths", () => {
    expect(pathTokenInCodeLine("├── poll_loop.py:12  # note")).toEqual({
      before: "├── ",
      path: "poll_loop.py:12",
      after: "  # note",
    });
  });
});

describe("isSingleShellCommandFence", () => {
  it("accepts shell language single lines", () => {
    expect(isSingleShellCommandFence("pytest -q", "language-bash")).toBe(true);
    expect(isSingleShellCommandFence("ls", "language-sh")).toBe(true);
  });

  it("accepts shell-like text without language", () => {
    expect(isSingleShellCommandFence("git status")).toBe(true);
  });

  it("rejects multi-line or non-shell fences", () => {
    expect(isSingleShellCommandFence("a\nb", "language-bash")).toBe(false);
    expect(isSingleShellCommandFence("const x = 1", "language-ts")).toBe(false);
  });
});
