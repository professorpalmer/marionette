/** Tokenize process / tool stdout so URLs and file paths become clickable.

Shared by ActionCard output and any other plain-text surfaces that need the
same routing as markdown autolinks — without pulling in react-markdown.
*/

import {
  isExternalUrl,
  looksLikeFilePath,
  looksLikePathInlineCode,
  looksLikeShellCommand,
} from "./agentLinks";

export type ClickableSegment =
  | { kind: "text"; text: string }
  | { kind: "url"; text: string; href: string }
  | { kind: "file"; text: string; path: string };

const URL_IN_TEXT = /https?:\/\/[^\s<>"'`)\]]+[^\s<>"'`)\].,;:!?]/g;

/** Path-ish token with optional :line[:col], including stack-frame forms. */
const PATH_IN_TEXT =
  /(?:[A-Za-z]:[\\/]|\/|\.{1,2}[\\/]|(?:[\w.-]+[\\/])+)?[\w.-]+\.\w{1,8}(?::\d+){0,2}/g;

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
  if (!looksLikePathInlineCode(bare) && !looksLikePathInlineCode(bare.split(/[\\/]/).pop() || "")) {
    return null;
  }
  return { before: m[1], path, after: m[3] || "" };
}

function pathCandidateValid(raw: string): boolean {
  const bare = raw.replace(/(?::\d+){1,2}$/, "");
  if (!bare || looksLikeShellCommand(bare)) return false;
  if (looksLikePathInlineCode(bare) || looksLikeFilePath(bare)) return true;
  // Bare basename.ext[:line] from stack frames.
  return looksLikePathInlineCode(bare.split(/[\\/]/).pop() || "");
}

type Span = { start: number; end: number; kind: "url" | "file"; text: string };

function collectSpans(line: string): Span[] {
  const spans: Span[] = [];
  URL_IN_TEXT.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = URL_IN_TEXT.exec(line)) !== null) {
    const text = m[0];
    if (!isExternalUrl(text)) continue;
    spans.push({ start: m.index, end: m.index + text.length, kind: "url", text });
  }
  PATH_IN_TEXT.lastIndex = 0;
  while ((m = PATH_IN_TEXT.exec(line)) !== null) {
    const text = m[0];
    if (!pathCandidateValid(text)) continue;
    // Skip overlaps with URLs (e.g. example.com/foo.py inside a URL).
    const start = m.index;
    const end = start + text.length;
    if (spans.some((s) => s.kind === "url" && start < s.end && end > s.start)) continue;
    spans.push({ start, end, kind: "file", text });
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
    } else {
      out.push({ kind: "file", text: s.text, path: s.text });
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
