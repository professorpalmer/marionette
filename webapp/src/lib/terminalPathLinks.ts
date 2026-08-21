/** xterm path-link provider for workspace / file:// tokens.

WebLinksAddon ships an https?-only matcher plus a hard `new URL()` filter, so
bare paths never become links even when a custom urlRegex is passed. This
provider owns file-path matching (reusing clickableOutput PATH_IN_TEXT) and
routes activates through agentLinks.
*/

import type { IDisposable, ILink, Terminal } from "@xterm/xterm";
import {
  isExternalUrl,
  looksLikeFilePath,
  looksLikeShellCommand,
  openAgentFile,
  openAgentUrl,
} from "./agentLinks";
import { PATH_IN_TEXT, unwrapPathToken } from "./clickableOutput";

export type TerminalPathMatch = {
  text: string;
  /** 0-based start index in the line string. */
  start: number;
  /** Exclusive end index in the line string. */
  end: number;
};

function pathCandidateValid(raw: string): boolean {
  const bare = unwrapPathToken(raw).replace(/(?::\d+){1,2}$/, "");
  if (!bare || looksLikeShellCommand(bare)) return false;
  if (looksLikeFilePath(bare)) return true;
  return false;
}

/** Match clickable path tokens on a single terminal line (testable pure helper). */
export function findTerminalPathMatches(line: string): TerminalPathMatch[] {
  const text = String(line || "");
  if (!text) return [];
  const out: TerminalPathMatch[] = [];
  const re = new RegExp(PATH_IN_TEXT.source, PATH_IN_TEXT.flags.includes("g") ? PATH_IN_TEXT.flags : `${PATH_IN_TEXT.flags}g`);
  re.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const token = m[0];
    if (!pathCandidateValid(token)) continue;
    // PATH_IN_TEXT often drops a leading '/' on multi-segment abs paths and
    // the 'file:' scheme — widen left so activate/parseFileHref see them.
    let start = m.index;
    let end = m.index + token.length;
    // Quoted matches: underline the inner path (skip wrapping quotes).
    if (
      (token.startsWith('"') && token.endsWith('"')) ||
      (token.startsWith("'") && token.endsWith("'"))
    ) {
      start += 1;
      end -= 1;
    }
    while (start > 0 && text[start - 1] === "/") {
      start -= 1;
    }
    if (start >= 5 && text.slice(start - 5, start).toLowerCase() === "file:") {
      start -= 5;
    }
    const matched = unwrapPathToken(text.slice(start, end));
    if (!looksLikeFilePath(matched)) continue;
    if (out.some((k) => start < k.end && end > k.start)) continue;
    out.push({ text: matched, start, end });
  }
  return out;
}

/** Activate a terminal link URI (https → browser, path → editor). */
export function activateTerminalLink(uri: string): void {
  const u = String(uri || "").trim();
  if (!u) return;
  if (isExternalUrl(u)) {
    openAgentUrl(u);
    return;
  }
  if (looksLikeFilePath(u)) {
    openAgentFile(u);
  }
}

/**
 * Register an ILinkProvider that underlines workspace / file:// path tokens.
 * Returns the disposable from xterm (caller may ignore; term.dispose cleans up).
 */
export function registerTerminalPathLinks(term: Terminal): IDisposable {
  return term.registerLinkProvider({
    provideLinks(bufferLineNumber, callback) {
      try {
        const lineObj = term.buffer.active.getLine(bufferLineNumber - 1);
        if (!lineObj) {
          callback(undefined);
          return;
        }
        const lineText = lineObj.translateToString(true);
        const matches = findTerminalPathMatches(lineText);
        if (!matches.length) {
          callback(undefined);
          return;
        }
        const links: ILink[] = matches.map((m) => ({
          text: m.text,
          range: {
            start: { x: m.start + 1, y: bufferLineNumber },
            // xterm ranges are inclusive on end.x
            end: { x: m.end, y: bufferLineNumber },
          },
          activate: (_event, linkText) => {
            activateTerminalLink(linkText);
          },
        }));
        callback(links);
      } catch {
        callback(undefined);
      }
    },
  });
}
