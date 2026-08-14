import { describe, expect, it } from "vitest";
import {
  reconstructStreamMarkdown,
  splitStreamingMarkdown,
} from "../lib/streamMarkdown";

function split(text: string) {
  const buf = splitStreamingMarkdown(text);
  expect(reconstructStreamMarkdown(buf)).toBe(text);
  return buf;
}

describe("splitStreamingMarkdown", () => {
  it("flushes plain prose", () => {
    const buf = split("Looking at the renderer. Packages first.");
    expect(buf.flushed).toBe("Looking at the renderer. Packages first.");
    expect(buf.hold).toBe("");
    expect(buf.open).toBeNull();
  });

  it("holds a trailing fence opener until the language tag is done", () => {
    expect(split("Packages.\n```").hold).toBe("```");
    expect(split("Packages.\n```py").hold).toBe("```py");
    const mid = split("Packages.\n```python");
    expect(mid.hold).toBe("```python");
    expect(mid.open).toBeNull();
    expect(mid.flushed).toBe("Packages.\n");
  });

  it("does not treat ```py as a python fence when the next token is thon", () => {
    const first = split("see\n```py");
    expect(first.open).toBeNull();
    expect(first.hold).toBe("```py");

    const second = split("see\n```python\n");
    expect(second.open?.lang).toBe("python");
    expect(second.open?.body).toBe("");
    expect(second.hold).toBe("");
    expect(second.flushed).toBe("see\n");
  });

  it("opens the fence only after the newline that ends the opener", () => {
    const buf = split("see\n```python\nprint(1)");
    expect(buf.flushed).toBe("see\n");
    expect(buf.open?.lang).toBe("python");
    expect(buf.open?.body).toBe("");
    expect(buf.hold).toBe("print(1)");
  });

  it("flushes complete lines inside an open fence and holds the partial line", () => {
    const buf = split("see\n```python\nprint(1)\nprint(");
    expect(buf.flushed).toBe("see\n");
    expect(buf.open?.lang).toBe("python");
    expect(buf.open?.body).toBe("print(1)\n");
    expect(buf.hold).toBe("print(");
  });

  it("flushes a completed fence including the closer", () => {
    const src = "see\n```python\nprint(1)\n```\nnext";
    const buf = split(src);
    expect(buf.flushed).toBe(src);
    expect(buf.hold).toBe("");
    expect(buf.open).toBeNull();
  });

  it("treats closer ticks at EOS as a completed fence", () => {
    const src = "```js\nconst x = 1;\n```";
    const buf = split(src);
    expect(buf.open).toBeNull();
    expect(buf.flushed).toBe(src);
  });

  it("does not close on backticks that are not a line-start fence", () => {
    const buf = split("```python\nprint('```')\nmore");
    expect(buf.open?.lang).toBe("python");
    expect(buf.open?.body).toBe("print('```')\n");
    expect(buf.hold).toBe("more");
  });

  it("holds a mid-line `` / ``` tail; a newline there is prose, not a fence", () => {
    expect(split("hello ``").hold).toBe("``");
    expect(split("hello ```").hold).toBe("```");
    const joined = split("hello ```python\n");
    expect(joined.open).toBeNull();
    expect(joined.flushed).toBe("hello ```python\n");
  });

  it("completes a line-start opener split across tokens", () => {
    expect(split("see\n``").hold).toBe("``");
    const opened = split("see\n```python\n");
    expect(opened.flushed).toBe("see\n");
    expect(opened.open?.lang).toBe("python");
  });

  it("does not hold a single trailing backtick (complete inline code stays flushed)", () => {
    const buf = split("use `code` here");
    expect(buf.flushed).toBe("use `code` here");
    expect(buf.hold).toBe("");
  });

  it("keeps a first complete fence flushed when a second is still open", () => {
    const buf = split("```a\nx\n```\n```b\ny");
    expect(buf.flushed).toBe("```a\nx\n```\n");
    expect(buf.open?.lang).toBe("b");
    expect(buf.hold).toBe("y");
  });

  it("supports tilde fences", () => {
    const open = split("~~~\nbody");
    expect(open.open?.ticks).toBe("~~~");
    expect(open.hold).toBe("body");
    const done = split("~~~\nbody\n~~~");
    expect(done.open).toBeNull();
    expect(done.flushed).toBe("~~~\nbody\n~~~");
  });

  it("returns an empty buffer for empty input", () => {
    expect(split("")).toEqual({ flushed: "", hold: "", open: null });
  });
});
