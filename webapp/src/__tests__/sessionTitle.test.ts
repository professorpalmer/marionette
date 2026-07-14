import { describe, expect, it } from "vitest";
import {
  DEFAULT_SESSION_TITLE,
  deriveSessionTitle,
  isDefaultSessionTitle,
} from "../lib/sessionTitle";

/** Mirrors tests/test_session_naming.py::test_derive_title */
describe("deriveSessionTitle", () => {
  it("truncates and capitalizes the first meaningful line", () => {
    expect(deriveSessionTitle("fix the resize bug in the tab bar please")).toBe(
      "Fix the resize bug in the tab bar",
    );
  });

  it("keeps trailing question marks on short titles", () => {
    expect(deriveSessionTitle("Why does /api/usage return 0?")).toBe(
      "Why does /api/usage return 0",
    );
  });

  it("strips code fences and markdown", () => {
    expect(deriveSessionTitle("```python\ndef foo():\n    return True\n```")).toBe("Def foo()");
  });

  it("handles bullet items", () => {
    expect(deriveSessionTitle("- item 1\n- item 2")).toBe("Item 1");
  });

  it("returns the default title for empty prompts", () => {
    expect(deriveSessionTitle("   \n   \n")).toBe(DEFAULT_SESSION_TITLE);
    expect(deriveSessionTitle("")).toBe(DEFAULT_SESSION_TITLE);
  });

  it("collapses whitespace", () => {
    expect(deriveSessionTitle("   hello    world   ")).toBe("Hello world");
  });
});

describe("isDefaultSessionTitle", () => {
  it("treats blank and New session as default", () => {
    expect(isDefaultSessionTitle("")).toBe(true);
    expect(isDefaultSessionTitle("New session")).toBe(true);
    expect(isDefaultSessionTitle("Fix the bug")).toBe(false);
  });
});
