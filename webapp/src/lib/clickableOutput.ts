/** Tokenize process / tool stdout so URLs and file paths become clickable.

Shared by ActionCard output and any other plain-text surfaces that need the
same routing as markdown autolinks — without pulling in react-markdown.
*/

import {
  isExternalUrl,
  looksLikeFilePath,
  looksLikeShellCommand,
  looksLikeSpillUri,
} from "./agentLinks";

export type ClickableSegment =
  | { kind: "text"; text: string }
  | { kind: "url"; text: string; href: string }
  | { kind: "spill"; text: string; uri: string }
  | { kind: "file"; text: string; path: string };

const URL_IN_TEXT = /https?:\/\/[^\s<>"'`)\]]+[^\s<>"'`)\].,;:!?]/g;
const SPILL_IN_TEXT = /spill:\/\/[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+/g;

/**
 * Path-ish token with optional :line[:col], including stack-frame forms.
 * Alternation order: quoted → unquoted spaced (dir sep required) → classic
 * no-space tokens. Spaced matches stay conservative to avoid prose link spam.
 */
/**
 * Path segment with optional single spaces (`My Projects`).
 * Continuation words after a space omit `.` so `app.ts and more.txt` cannot
 * glue into one token.
 */
const PATH_SEG = String.raw`[\w.-]+(?: [\w-]+)*`;
/** Filename body before the final .ext (no dotted intermediate words). */
const PATH_FILE = String.raw`[\w-]+(?: [\w-]+)*`;

export const PATH_IN_TEXT = new RegExp(
  [
    // Quoted abs/rel paths: "/Users/me/My Projects/app.ts" or ".../my file.ts"
    String.raw`["'](?:[A-Za-z]:[\\/]|\/|\.{1,2}[\\/])(?:${PATH_SEG}[\\/])*${PATH_FILE}\.\w{1,8}(?::\d+){0,2}["']`,
    // Unquoted spaced abs/rel: must start at a boundary (not inside https://…)
    // and contain a space; directory separator required.
    String.raw`(?:^|(?<=[\s(\[{]))(?:[A-Za-z]:[\\/]|\/|\.{1,2}[\\/])(?=[^\n]* )(?:${PATH_SEG}[\\/])+${PATH_FILE}\.\w{1,8}(?::\d+){0,2}`,
    // Classic no-space paths / bare basename.ext
    String.raw`(?:[A-Za-z]:[\\/]|\/|\.{1,2}[\\/]|(?:[\w.-]+[\\/])+)?[\w.-]+\.\w{1,8}(?::\d+){0,2}`,
  ].join("|"),
  "g",
);

/** Strip wrapping quotes from a matched path token (keeps :line[:col]). */
export function unwrapPathToken(raw: string): string {
  const t = String(raw || "");
  if (
    (t.startsWith('"') && t.endsWith('"') && t.length >= 2) ||
    (t.startsWith("'") && t.endsWith("'") && t.length >= 2)
  ) {
    return t.slice(1, -1);
  }
  // Quotes with :line[:col] after the closing quote — rare but defend.
  const m = t.match(/^["'](.+)["']((?::\d+){0,2})$/);
  if (m) return m[1] + m[2];
  return t;
}

/** Pull a file-ish path from a tree / listing line (`├── poll_loop.py:12  # note`). */
export function pathTokenInCodeLine(
  line: string,
): { before: string; path: string; after: string } | null {
  const m = line.match(
    /^(.*?)((?:[\w.-]+[\\/])*[\w.-]+\.\w{1,8}(?::\d+){0,2})(\s*(?:#.*)?)$/,
  );
  if (!m) return null;
  const path = m[2];
  const bare = path.replace(/(?::\d+){1,2}$/, "");
  if (!looksLikeFilePath(bare)) return null;
  return { before: m[1], path, after: m[3] || "" };
}

function pathCandidateValid(raw: string): boolean {
  const unwrapped = unwrapPathToken(raw);
  const bare = unwrapped.replace(/(?::\d+){1,2}$/, "");
  if (!bare || looksLikeShellCommand(bare)) return false;
  if (looksLikeFilePath(bare)) return true;
  return false;
}

type Span = {
  start: number;
  end: number;
  kind: "url" | "spill" | "file";
  text: string;
  /** Open target for file spans (quotes stripped). */
  path?: string;
  /** Open target for spill spans. */
  uri?: string;
};

function collectSpans(line: string): Span[] {
  const spans: Span[] = [];
  URL_IN_TEXT.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = URL_IN_TEXT.exec(line)) !== null) {
    const text = m[0];
    if (!isExternalUrl(text)) continue;
    spans.push({ start: m.index, end: m.index + text.length, kind: "url", text });
  }
  SPILL_IN_TEXT.lastIndex = 0;
  while ((m = SPILL_IN_TEXT.exec(line)) !== null) {
    const text = m[0];
    if (!looksLikeSpillUri(text)) continue;
    spans.push({
      start: m.index,
      end: m.index + text.length,
      kind: "spill",
      text,
      uri: text,
    });
  }
  PATH_IN_TEXT.lastIndex = 0;
  while ((m = PATH_IN_TEXT.exec(line)) !== null) {
    const text = m[0];
    if (!pathCandidateValid(text)) continue;
    // Skip overlaps with URLs / spills (e.g. example.com/foo.py inside a URL).
    const start = m.index;
    const end = start + text.length;
    if (spans.some((s) => (s.kind === "url" || s.kind === "spill") && start < s.end && end > s.start)) {
      continue;
    }
    spans.push({
      start,
      end,
      kind: "file",
      text,
      path: unwrapPathToken(text),
    });
  }
  spans.sort((a, b) => a.start - b.start || b.end - a.end);
  const kept: Span[] = [];
  for (const s of spans) {
    if (kept.some((k) => s.start < k.end && s.end > k.start)) continue;
    kept.push(s);
  }
  return kept;
}

function tokenizeLine(line: string): ClickableSegment[] {
  if (!line) return [{ kind: "text", text: "" }];
  const spans = collectSpans(line);
  if (!spans.length) return [{ kind: "text", text: line }];
  const out: ClickableSegment[] = [];
  let cursor = 0;
  for (const s of spans) {
    if (s.start > cursor) {
      out.push({ kind: "text", text: line.slice(cursor, s.start) });
    }
    if (s.kind === "url") {
      out.push({ kind: "url", text: s.text, href: s.text });
    } else if (s.kind === "spill") {
      out.push({ kind: "spill", text: s.text, uri: s.uri || s.text });
    } else {
      out.push({ kind: "file", text: s.text, path: s.path || unwrapPathToken(s.text) });
    }
    cursor = s.end;
  }
  if (cursor < line.length) {
    out.push({ kind: "text", text: line.slice(cursor) });
  }
  return out;
}

/** Tokenize multi-line process output into text / url / file segments. */
export function tokenizeClickableOutput(text: string): ClickableSegment[] {
  const raw = String(text || "");
  if (!raw) return [];
  const lines = raw.split("\n");
  const out: ClickableSegment[] = [];
  for (let i = 0; i < lines.length; i += 1) {
    const segs = tokenizeLine(lines[i]);
    out.push(...segs);
    if (i < lines.length - 1) {
      // Preserve newlines as text so a <pre> renderer stays faithful.
      const last = out[out.length - 1];
      if (last && last.kind === "text") {
        last.text += "\n";
      } else {
        out.push({ kind: "text", text: "\n" });
      }
    }
  }
  return out;
}

const SHELL_FENCE_LANGS = new Set([
  "bash",
  "sh",
  "shell",
  "zsh",
  "powershell",
  "pwsh",
  "cmd",
  "ps1",
  "console",
  "terminal",
]);

/** True when a fenced block is a single shell command worth revealing. */
export function isSingleShellCommandFence(codeText: string, className?: string): boolean {
  const trimmed = String(codeText || "").replace(/\n$/, "").trim();
  if (!trimmed || trimmed.includes("\n")) return false;
  const lang = String(className || "")
    .split(/\s+/)
    .map((c) => c.replace(/^language-/, "").toLowerCase())
    .find(Boolean) || "";
  if (lang && SHELL_FENCE_LANGS.has(lang)) return true;
  // A non-shell language tag wins — don't treat `const x = 1` as a command.
  if (lang) return false;
  return looksLikeShellCommand(trimmed);
}
