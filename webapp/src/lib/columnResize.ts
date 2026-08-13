/** Document class while a column drag is live so webviews cannot steal the pointer. */
export const COLUMN_RESIZE_CLASS = "is-col-resizing";
export const ROW_RESIZE_CLASS = "is-row-resizing";

function beginPaneResize(className: string, cursor: string): void {
  document.body.classList.add(className);
  document.body.style.cursor = cursor;
  document.body.style.userSelect = "none";
}

function endPaneResize(className: string): void {
  document.body.classList.remove(className);
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
}

export function beginColumnResize(): void {
  beginPaneResize(COLUMN_RESIZE_CLASS, "col-resize");
}

export function endColumnResize(): void {
  endPaneResize(COLUMN_RESIZE_CLASS);
}

export function beginRowResize(): void {
  beginPaneResize(ROW_RESIZE_CLASS, "row-resize");
}

export function endRowResize(): void {
  endPaneResize(ROW_RESIZE_CLASS);
}
