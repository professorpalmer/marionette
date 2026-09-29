export type SessionViewport =
  | { kind: "tail" }
  | { kind: "anchor"; key: string | null; offset: number; scrollTop: number };

export type TranscriptViewportHandle = {
  capture: (pinned: boolean) => SessionViewport;
  restore: (saved: SessionViewport) => void;
};

/** ``measureRows`` forces layout, so it runs only when a row anchor is needed:
 * pinned-to-tail captures fire on every streamed token. */
export function captureSessionViewport(
  pinned: boolean,
  scrollTop: number,
  measureRows: () => readonly { key: string; start: number; end: number }[],
): SessionViewport {
  if (pinned) return { kind: "tail" };
  const row = measureRows().find((row) => row.end > scrollTop);
  return { kind: "anchor", key: row?.key ?? null, offset: row ? scrollTop - row.start : 0, scrollTop };
}

export function sessionViewportOffset(
  saved: SessionViewport,
  anchorStart: number | null,
  scrollHeight: number,
  clientHeight: number,
  userGesture: boolean,
): number | null {
  if (userGesture) return null;
  const max = Math.max(0, scrollHeight - clientHeight);
  if (saved.kind === "tail") return max;
  return Math.max(0, Math.min(max, anchorStart === null ? saved.scrollTop : anchorStart + saved.offset));
}
