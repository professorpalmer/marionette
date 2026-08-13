import { describe, expect, it } from "vitest";
import { detectEditorKind } from "../components/FileEditorPane";

describe("detectEditorKind", () => {
  const cases: Array<{
    path: string;
    binary?: boolean;
    expected: ReturnType<typeof detectEditorKind>;
  }> = [
    { path: "README.md", expected: "markdown" },
    { path: "notes.markdown", expected: "markdown" },
    { path: "index.html", expected: "html" },
    { path: "legacy.htm", expected: "html" },
    { path: "doc.pdf", binary: true, expected: "pdf" },
    { path: "photo.png", binary: true, expected: "image" },
    { path: "archive.zip", binary: true, expected: "binary" },
    { path: "src/app.ts", expected: "code" },
    { path: "script.py", expected: "code" },
  ];

  it.each(cases)("maps $path (binary=$binary) to $expected", ({ path, binary, expected }) => {
    expect(detectEditorKind(path, binary)).toBe(expected);
  });
});
