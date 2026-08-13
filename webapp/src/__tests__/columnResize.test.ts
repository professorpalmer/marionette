import { afterEach, describe, expect, it } from "vitest";
import { beginColumnResize, COLUMN_RESIZE_CLASS, endColumnResize } from "../lib/columnResize";

describe("columnResize", () => {
  afterEach(() => {
    endColumnResize();
  });

  it("marks the document while a column drag is live", () => {
    beginColumnResize();
    expect(document.body.classList.contains(COLUMN_RESIZE_CLASS)).toBe(true);
    expect(document.body.style.cursor).toBe("col-resize");
    endColumnResize();
    expect(document.body.classList.contains(COLUMN_RESIZE_CLASS)).toBe(false);
    expect(document.body.style.cursor).toBe("");
  });
});
