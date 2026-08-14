/** Split streaming markdown so highlighters never see a fence they can still extend.

Hold a tail (partial opener, last open-fence line). Flush every complete block.
Pretty-render (react-markdown / highlight.js) may run only on `flushed`.
The hold / open `<pre>` is the urgent path — cheap monospace, no highlight.

Invariant: `reconstructStreamMarkdown(buf) === text` for any split of `text`.
*/

export type StreamFence = {
  ticks: string;
  lang: string;
  body: string;
  /** Exact opener line, including the trailing newline. */
  opener: string;
};

export type StreamMarkdownBuf = {
  flushed: string;
  hold: string;
  open: StreamFence | null;
};

const EMPTY_BUF: StreamMarkdownBuf = { flushed: "", hold: "", open: null };

/** Trailing run that can still become a fence opener (` `` ` / ` ``` ` / ` ```py `). */
const PARTIAL_FENCE_TAIL = /(`{2,}|~{3,})(\w*)$/;

const FENCE_LINE = /^( {0,3})(`{3,}|~{3,})(.*)$/;
const CLOSER_LINE = /^( {0,3})(`{3,}|~{3,})[ \t]*$/;

export function splitStreamingMarkdown(text: string): StreamMarkdownBuf {
  if (!text) return EMPTY_BUF;

  let pos = 0;
  let flushed = "";
  const n = text.length;

  while (pos < n) {
    const nl = text.indexOf("\n", pos);
    const isLastLine = nl === -1;
    const line = isLastLine ? text.slice(pos) : text.slice(pos, nl);
    const lineStart = pos;

    const opener = parseOpenerLine(line);
    if (opener) {
      if (isLastLine) {
        return { flushed, hold: text.slice(lineStart), open: null };
      }
      const openerLine = text.slice(lineStart, nl + 1);
      const bodyStart = nl + 1;
      const closed = findClosingFence(text, bodyStart, opener.ticks);
      if (closed) {
        flushed += text.slice(lineStart, closed.end);
        pos = closed.end;
        continue;
      }
      const body = text.slice(bodyStart);
      const lastNl = body.lastIndexOf("\n");
      if (lastNl === -1) {
        return {
          flushed,
          hold: body,
          open: { ticks: opener.ticks, lang: opener.lang, body: "", opener: openerLine },
        };
      }
      return {
        flushed,
        hold: body.slice(lastNl + 1),
        open: {
          ticks: opener.ticks,
          lang: opener.lang,
          body: body.slice(0, lastNl + 1),
          opener: openerLine,
        },
      };
    }

    if (isLastLine) {
      const partial = line.match(PARTIAL_FENCE_TAIL);
      if (partial && partial.index !== undefined) {
        return {
          flushed: flushed + line.slice(0, partial.index),
          hold: partial[0],
          open: null,
        };
      }
      return { flushed: flushed + line, hold: "", open: null };
    }

    flushed += text.slice(pos, nl + 1);
    pos = nl + 1;
  }

  return { flushed, hold: "", open: null };
}

/** Rebuild the source string from a split. Used by tests as the hold/flush contract. */
export function reconstructStreamMarkdown(buf: StreamMarkdownBuf): string {
  if (!buf.open) return buf.flushed + buf.hold;
  return `${buf.flushed}${buf.open.opener}${buf.open.body}${buf.hold}`;
}

function parseOpenerLine(line: string): { ticks: string; lang: string } | null {
  const m = line.match(FENCE_LINE);
  if (!m) return null;
  if (CLOSER_LINE.test(line) && !m[3].trim()) {
    // A ticks-only line at the start of a scan is still an opener (empty lang).
    return { ticks: m[2], lang: "" };
  }
  const info = m[3].trim();
  const lang = info.split(/\s+/, 1)[0] || "";
  return { ticks: m[2], lang };
}

function findClosingFence(
  text: string,
  from: number,
  ticks: string,
): { end: number } | null {
  let pos = from;
  const n = text.length;
  while (pos <= n) {
    if (pos === n) return null;
    const nl = text.indexOf("\n", pos);
    const isLastLine = nl === -1;
    const line = isLastLine ? text.slice(pos) : text.slice(pos, nl);
    if (isCloserLine(line, ticks)) {
      if (isLastLine) return { end: n };
      return { end: nl + 1 };
    }
    if (isLastLine) return null;
    pos = nl + 1;
  }
  return null;
}

function isCloserLine(line: string, ticks: string): boolean {
  const m = line.match(CLOSER_LINE);
  return Boolean(m && m[2][0] === ticks[0] && m[2].length >= ticks.length);
}
