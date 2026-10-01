import { useCallback, useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import type { Item, TranscriptFindApi } from "../TranscriptList";
import { clearFindHighlights, findMatches, paintFindHighlights, stepMatch, type FindMatch } from "../../lib/transcriptFind";
import { TranscriptFindBar } from "./TranscriptFindBar";

/** Cmd/Ctrl+F belongs to the chat when focus is in it or nowhere in particular. */
function ownsFindShortcut(target: EventTarget | null, column: Element | null): boolean {
  if (!(target instanceof Element)) return true;
  if (target === document.body || target === document.documentElement) return true;
  return Boolean(column && column.contains(target));
}

export function useTranscriptFind(
  feedRef: RefObject<HTMLDivElement | null>,
  columnRef: RefObject<HTMLDivElement | null>,
  items: Item[],
  sessionId: string | undefined,
  rightInset: number,
): { findApiRef: RefObject<TranscriptFindApi | null>; findBar: ReactNode } {
  const findApiRef = useRef<TranscriptFindApi | null>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(-1);
  const [focusNonce, setFocusNonce] = useState(0);

  const [matches, setMatches] = useState<FindMatch[]>([]);
  const searchedQuery = useRef("");

  const close = useCallback(() => {
    setOpen(false);
    clearFindHighlights();
  }, []);
  const step = useCallback(
    (delta: 1 | -1) => setActive((a) => stepMatch(a, matches.length, delta)),
    [matches.length],
  );

  // Row texts come from the grouped rows the list renders, so they track
  // `items`. A new query lands on its first match; new transcript rows keep
  // the current position (clamped).
  useEffect(() => {
    const next = open ? findMatches(findApiRef.current?.rowTexts() ?? [], query) : [];
    const newQuery = searchedQuery.current !== query;
    searchedQuery.current = query;
    setMatches(next);
    setActive((a) => (!next.length ? -1 : newQuery || a < 0 ? 0 : Math.min(a, next.length - 1)));
  }, [open, query, items]);
  const hit = active >= 0 && active < matches.length ? matches[active]! : null;
  // Primitives, not the match object: matches are rebuilt on every streamed
  // update and must not re-scroll to an unchanged target.
  const activeRow = hit ? hit.row : -1;
  const activeOccurrence = hit ? hit.occurrence : -1;

  useEffect(() => close, [sessionId, close]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
      const key = e.key.toLowerCase();
      if (key === "f" && !e.shiftKey && ownsFindShortcut(e.target, columnRef.current)) {
        e.preventDefault();
        setOpen(true);
        setFocusNonce((n) => n + 1);
      } else if (key === "g" && open) {
        e.preventDefault();
        step(e.shiftKey ? -1 : 1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [columnRef, open, step]);

  // Bring the active match's row into view, then center the match itself once
  // the row has mounted (virtualized rows mount a frame or two later).
  useEffect(() => {
    if (!open || activeRow < 0) return;
    const current = { row: activeRow, occurrence: activeOccurrence };
    findApiRef.current?.revealRow(activeRow);
    let frame = 0;
    let tries = 0;
    const center = () => {
      const feed = feedRef.current;
      const range = paintFindHighlights(feed, query, current);
      if (range && feed) {
        const at = range.getBoundingClientRect();
        const port = feed.getBoundingClientRect();
        feed.scrollTop += at.top + at.height / 2 - (port.top + port.height / 2);
        paintFindHighlights(feed, query, current);
        return;
      }
      if (++tries < 12) frame = requestAnimationFrame(center);
    };
    frame = requestAnimationFrame(center);
    return () => cancelAnimationFrame(frame);
  }, [open, activeRow, activeOccurrence, query, feedRef]);

  // Repaint as rows mount/unmount while scrolling or streaming.
  useEffect(() => {
    const feed = feedRef.current;
    if (!open || !feed) return;
    const current = activeRow >= 0 ? { row: activeRow, occurrence: activeOccurrence } : null;
    let frame = 0;
    const repaint = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => paintFindHighlights(feed, query, current));
    };
    repaint();
    feed.addEventListener("scroll", repaint, { passive: true });
    const observer = new MutationObserver(repaint);
    observer.observe(feed, { childList: true, subtree: true, characterData: true });
    return () => {
      cancelAnimationFrame(frame);
      feed.removeEventListener("scroll", repaint);
      observer.disconnect();
    };
  }, [open, query, activeRow, activeOccurrence, feedRef, items]);

  useEffect(() => clearFindHighlights, []);

  const findBar = open ? (
    <TranscriptFindBar
      query={query}
      onQuery={setQuery}
      active={hit ? active : -1}
      total={matches.length}
      onStep={step}
      onClose={close}
      focusNonce={focusNonce}
      rightInset={rightInset}
    />
  ) : null;
  return { findApiRef, findBar };
}
