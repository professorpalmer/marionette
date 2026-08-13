/** Document class while a column drag is live so webviews cannot steal the pointer. */
export const COLUMN_RESIZE_CLASS = "is-col-resizing";

export function beginColumnResize(): void {
  document.body.classList.add(COLUMN_RESIZE_CLASS);
  document.body.style.cursor = "col-resize";
  document.body.style.userSelect = "none";
}

export function endColumnResize(): void {
  document.body.classList.remove(COLUMN_RESIZE_CLASS);
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
}
