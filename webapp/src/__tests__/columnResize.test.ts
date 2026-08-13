import { afterEach, describe, expect, it } from "vitest";
import {
  beginColumnResize,
  beginRowResize,
  COLUMN_RESIZE_CLASS,
  endColumnResize,
  endRowResize,
  ROW_RESIZE_CLASS,
} from "../lib/columnResize";

describe("columnResize", () => {
  afterEach(() => {
    endColumnResize();
    endRowResize();
  });

  it("marks the document while a column drag is live", () => {
    beginColumnResize();
    expect(document.body.classList.contains(COLUMN_RESIZE_CLASS)).toBe(true);
    expect(document.body.style.cursor).toBe("col-resize");
    endColumnResize();
    expect(document.body.classList.contains(COLUMN_RESIZE_CLASS)).toBe(false);
    expect(document.body.style.cursor).toBe("");
  });

  it("marks the document while a stacked-row drag is live", () => {
    beginRowResize();
    expect(document.body.classList.contains(ROW_RESIZE_CLASS)).toBe(true);
    expect(document.body.style.cursor).toBe("row-resize");
    endRowResize();
    expect(document.body.classList.contains(ROW_RESIZE_CLASS)).toBe(false);
    expect(document.body.style.cursor).toBe("");
  });
});
