/** Agent-loop link routing: paths → file editor, URLs → in-app browser, commands → terminal.

Mirrors Cursor/Hermes polish: clicks in the transcript open the right surface
instead of a raw OS navigation. Never throws.
*/

import { normalizeRepoPath } from "./pathNormalize";

export type OpenFileDetail = {
  path: string;
  line?: number;
  col?: number;
};

export type ParsedFileHref = {
  path: string;
  line?: number;
  col?: number;
};

const URL_RE = /^https?:\/\//i;

/** True for http(s) URLs (in-app browser). */
export function isExternalUrl(href: string): boolean {
  return URL_RE.test(href || "");
}

/**
 * Heuristic: does this look like a filesystem path (not a URL/scheme)?
 * Accepts Windows abs, POSIX abs/rel, and dotted filenames with optional :line[:col].
 */
export function looksLikeFilePath(href: string): boolean {
  if (!href) return false;
  const h = href.trim();
  if (!h) return false;
  if (/^(https?|mailto|tel|data|javascript):/i.test(h) || h.startsWith("#")) return false;
  const clean = h.replace(/^file:\/\//i, "");
  // Drive letter, absolute, relative with slash, or name.ext[:line[:col]]
  if (/^[A-Za-z]:[\\/]/.test(clean)) return true;
  if (/[\\/]/.test(clean)) return true;
  if (/^\.\.?[\\/]/.test(clean)) return true;
  if (/\.\w{1,8}(:\d+){0,2}$/.test(clean)) return true;
  return false;
}

/** Strip file:// and optional :line[:col] suffix. */
export function parseFileHref(href: string): ParsedFileHref | null {
  if (!href || !looksLikeFilePath(href)) return null;
  let raw = href.trim().replace(/^file:\/\//i, "");
  // file:///C:/foo → C:/foo on Windows; file:///home → /home
  if (/^\/[A-Za-z]:[\\/]/.test(raw)) {
    raw = raw.slice(1);
  }
  let line: number | undefined;
  let col: number | undefined;
  // path.ext:12 or path.ext:12:3 — require a dotted extension before :line
  const m = raw.match(/^(.+\.\w{1,8}):(\d+)(?::(\d+))?$/);
  if (m) {
    raw = m[1];
    line = parseInt(m[2], 10);
    if (m[3]) col = parseInt(m[3], 10);
  }
  raw = raw.trim();
  if (!raw) return null;
  // Touch normalize for side-effect-free hygiene check; keep original separators
  // so the file API receives what the user/agent wrote.
  void normalizeRepoPath(raw);
  return { path: raw, line, col };
}

/** Inline `` `path` `` that should open as a file. */
export function looksLikePathInlineCode(text: string): boolean {
  const t = (text || "").trim();
  if (!t || t.includes("\n") || t.length > 260) return false;
  // Reject obvious non-paths (commands, flags, pure identifiers).
  if (/^[-+]/.test(t)) return false;
  if (/\s/.test(t)) return false;
  return looksLikeFilePath(t);
}

export type AgentLinkKind = "url" | "file" | "command" | "none";

/** Classify an ActionCard goal by tool kind. */
export function classifyActionGoal(
  kind: string,
  goal: string
): { linkKind: AgentLinkKind; value: string } {
  const k = (kind || "").toLowerCase();
  const g = (goal || "").trim();
  if (!g) return { linkKind: "none", value: "" };
  if (
    k === "read_file" ||
    k === "write_file" ||
    k === "edit_file" ||
    k === "hash_edit" ||
    k === "view_image" ||
    k === "open_project"
  ) {
    return { linkKind: "file", value: g };
  }
  if (k === "web_fetch") {
    return { linkKind: "url", value: g };
  }
  if (k === "run_command") {
    return { linkKind: "command", value: g };
  }
  if (isExternalUrl(g)) return { linkKind: "url", value: g };
  if (looksLikeFilePath(g)) return { linkKind: "file", value: g };
  return { linkKind: "none", value: g };
}

export function openAgentUrl(url: string): void {
  if (!url || !isExternalUrl(url)) return;
  try {
    (window as any).__pmPendingBrowserUrl = url;
    window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "browser" }));
    window.dispatchEvent(new CustomEvent("harness-open-url", { detail: { url } }));
  } catch {
    /* ignore */
  }
}

export function openAgentFile(pathOrHref: string, line?: number, col?: number): void {
  const parsed = parseFileHref(pathOrHref) || (looksLikeFilePath(pathOrHref)
    ? { path: pathOrHref.trim(), line, col }
    : null);
  if (!parsed) return;
  const detail: OpenFileDetail = {
    path: parsed.path,
    line: line ?? parsed.line,
    col: col ?? parsed.col,
  };
  try {
    window.dispatchEvent(new CustomEvent("harness-open-file", { detail }));
  } catch {
    /* ignore */
  }
}

export function openAgentCommand(command: string, opts?: { run?: boolean }): void {
  const cmd = (command || "").trim();
  if (!cmd) return;
  try {
    window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "terminal" }));
    if (opts?.run) {
      window.dispatchEvent(
        new CustomEvent("harness-run-command", { detail: { command: cmd } })
      );
    }
  } catch {
    /* ignore */
  }
}

/** Route a markdown href click (or synthetic open). */
export function openAgentLink(href: string, e?: { preventDefault(): void }): void {
  if (!href) return;
  if (isExternalUrl(href)) {
    e?.preventDefault();
    openAgentUrl(href);
    return;
  }
  if (looksLikeFilePath(href)) {
    e?.preventDefault();
    openAgentFile(href);
    return;
  }
  e?.preventDefault();
}

/**
 * Autolink bare https URLs and file-ish paths in markdown prose.
 * Skips fenced code blocks and inline code; does not rewrite existing links.
 */
export function autolinkAgentText(text: string): string {
  if (!text) return text;
  const lines = text.split("\n");
  const out: string[] = [];
  let inFence = false;
  for (const line of lines) {
    const fence = line.trimStart().startsWith("```");
    if (fence) {
      inFence = !inFence;
      out.push(line);
      continue;
    }
    if (inFence) {
      out.push(line);
      continue;
    }
    out.push(_autolinkLine(line));
  }
  return out.join("\n");
}

const BARE_URL = /https?:\/\/[^\s<>"'`)\]]+[^\s<>"'`)\].,;:!?]/g;
// Windows abs, POSIX abs, ./rel, path/with/slash.ext — optional :line[:col]
const BARE_PATH =
  /(?:^|[\s(])((?:[A-Za-z]:[\\/]|\/|\.{1,2}[\\/])[^\s`"'<>\]|]+?\.\w{1,8}(?::\d+){0,2}|(?:[\w.-]+\/)+[\w.-]+\.\w{1,8}(?::\d+){0,2})(?=[\s).,]|$)/g;

function _autolinkLine(line: string): string {
  // Protect existing markdown links and inline code with placeholders.
  const slots: string[] = [];
  const protect = (s: string) => {
    const i = slots.length;
    slots.push(s);
    return `\u0000${i}\u0000`;
  };
  let work = line.replace(/`[^`\n]+`/g, protect);
  work = work.replace(/\[[^\]]*\]\([^)]+\)/g, protect);
  work = work.replace(/<https?:\/\/[^>]+>/g, protect);

  work = work.replace(BARE_URL, (m) => {
    if (m.startsWith("<")) return m;
    return `[${m}](${m})`;
  });
  work = work.replace(BARE_PATH, (full, pathPart: string, offset: number) => {
    if (isExternalUrl(pathPart) || !looksLikeFilePath(pathPart)) return full;
    // Preserve the leading delimiter captured by (?:^|[\s(])
    const lead = full.slice(0, full.length - pathPart.length);
    return `${lead}[\`${pathPart}\`](${pathPart})`;
  });

  return work.replace(/\u0000(\d+)\u0000/g, (_, i) => slots[Number(i)] || "");
}
