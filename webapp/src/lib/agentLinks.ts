/** Agent-loop link routing: paths → file editor, URLs → in-app browser, commands → terminal.

Mirrors Cursor/Hermes polish: clicks in the transcript open the right surface
instead of a raw OS navigation. Never throws.
*/

import { normalizeRepoPath } from "./pathNormalize";
import {
  lookupAgentCommandSession,
  lookupAgentCommandSessionById,
  registerAgentCommandSession,
} from "./agentCommandIndex";
import {
  seedAgentTerminalCommand,
  syncAgentTerminalSnapshot,
} from "./agentTerminalStream";
import { queuePendingSwarmOpenJob } from "./pendingSwarmOpenJob";

export type OpenFileDetail = {
  path: string;
  line?: number;
  col?: number;
  /** File-tree / known-good paths may open even if resolve is unavailable. */
  trusted?: boolean;
};

export type ParsedFileHref = {
  path: string;
  line?: number;
  col?: number;
};

const URL_RE = /^https?:\/\//i;

/** Bare Windows/shell launcher extensions without a directory separator. */
const BARE_EXEC_EXT = /\.(cmd|exe|bat|ps1|sh)$/i;

const SHELL_LAUNCHERS = new Set([
  "npm",
  "npx",
  "pnpm",
  "yarn",
  "pip",
  "pip3",
  "pytest",
  "python",
  "python3",
  "node",
  "git",
  "curl",
  "wget",
  "make",
  "cargo",
  "go",
  "docker",
  "kubectl",
  "rg",
  "find",
  "ls",
  "cd",
  "echo",
  "cmd",
  "powershell",
  "pwsh",
  "bash",
  "sh",
  "zsh",
  "brew",
  "poetry",
  "uv",
  "ruby",
  "perl",
  "php",
  "java",
  "mvn",
  "gradle",
  "cmake",
  "terraform",
  "ssh",
  "scp",
  "rsync",
  "sudo",
  "env",
  "cat",
  "head",
  "tail",
  "grep",
  "sed",
  "awk",
  "which",
  "where",
  "dir",
  "type",
]);

/** Stable short id for an agent-command reveal (Hermes-style hash). */
export function stableCommandId(value: string): string {
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) {
    hash = Math.imul(31, hash) + value.charCodeAt(i);
  }
  return Math.abs(hash).toString(36);
}

/**
 * Hermes `looksLikePath` / Prime `isLocalPathSpecifier`: a real filesystem
 * prefix, not merely a slash. `@scope/pkg` has a slash and is not a path.
 */
function hasFilesystemPrefix(clean: string): boolean {
  return (
    /^[A-Za-z]:[\\/]/.test(clean)
    || clean.startsWith("/")
    || clean.startsWith("~/")
    || /^\.{1,2}[\\/]/.test(clean)
  );
}

/**
 * Last segment looks like a filename (`App.tsx`, `foo.py:12`, `archive.7z`).
 * Letter-start extensions reject version tails (`.1` in `tar@6.2.1`).
 */
function hasFilenameExtension(clean: string): boolean {
  const withoutLine = clean.replace(/(?::\d+(?:-\d+)?)(?::\d+)?$/, "");
  const base = withoutLine.split(/[\\/]/).pop() || "";
  return /\.([A-Za-z][\w]{0,7}|7z)$/.test(base);
}

/**
 * npm/pip-style package specs and import aliases, not local files.
 * `@` without a filesystem prefix is `@scope/pkg`, `tar@6.2.1`, `@/alias`.
 */
function looksLikePackageSpec(clean: string): boolean {
  return clean.includes("@") && !hasFilesystemPrefix(clean);
}

/**
 * Conservative spaced filesystem path (macOS "My Projects/app.ts", etc.).
 * Requires a directory separator and a dotted filename so prose / shell
 * lines with spaces stay non-paths.
 */
function looksLikeSpacedFilePath(text: string): boolean {
  const clean = text
    .replace(/^file:\/\//i, "")
    .replace(/^["']|["']$/g, "");
  if (!clean || !/\s/.test(clean)) return false;
  if (/&&|\|\||[|<>;&]/.test(clean)) return false;
  if (!/[\\/]/.test(clean)) return false;
  if (looksLikePackageSpec(clean)) return false;
  return hasFilenameExtension(clean);
}

/**
 * Heuristic: does this look like a shell command line rather than a file path?
 * Whitespace args, flags, shell operators, or known launcher tokens.
 * Spaced paths that still look like files (dir sep + extension) are excluded.
 */
export function looksLikeShellCommand(text: string): boolean {
  const t = (text || "").trim();
  if (!t) return false;
  if (/&&|\|\||[|<>;&]/.test(t)) return true;
  if (/^[-+]/.test(t)) return true;
  if (/\s/.test(t)) {
    // `/Users/me/My Projects/app.ts` is a path, not `pytest -q`.
    if (looksLikeSpacedFilePath(t)) return false;
    return true;
  }
  const base = t.replace(BARE_EXEC_EXT, "");
  if (SHELL_LAUNCHERS.has(base.toLowerCase())) return true;
  return false;
}

/** True for http(s) URLs (in-app browser). */
export function isExternalUrl(href: string): boolean {
  return URL_RE.test(href || "");
}

/**
 * Heuristic: does this look like a filesystem path (not a URL/scheme)?
 * Accepts Windows abs, POSIX abs/home/rel, and dotted filenames with optional
 * :line[:col]. Rejects shell lines, launchers, and package specs
 * (`@scope/pkg`, `tar@6.2.1`) the way Hermes/Prime require a path prefix.
 */
export function looksLikeFilePath(href: string): boolean {
  if (!href) return false;
  const h = href.trim();
  if (!h) return false;
  if (
    /^(https?|mailto|tel|data|javascript|spill|artifact|job|agent|conflict):/i.test(h)
    || h.startsWith("#")
  ) {
    return false;
  }
  if (looksLikeShellCommand(h)) return false;
  const clean = h
    .replace(/^file:\/\//i, "")
    .replace(/^["']|["']$/g, "");
  if (looksLikePackageSpec(clean)) return false;
  // Bare executables without a directory separator are shell launchers, not files.
  if (!/[\\/]/.test(clean) && BARE_EXEC_EXT.test(clean)) return false;
  if (hasFilesystemPrefix(clean)) return true;
  // Repo-relative `dir/file.ext` or bare `name.ext` — never a slash-only token
  // (`anysphere/ui`) or a numeric version tail (`6.2.1`).
  return hasFilenameExtension(clean);
}

/** Strip file:// and optional :line[:col] suffix. */
export function parseFileHref(href: string): ParsedFileHref | null {
  if (!href || !looksLikeFilePath(href)) return null;
  let raw = href
    .trim()
    .replace(/^file:\/\//i, "")
    .replace(/^["']|["']$/g, "");
  // file:///C:/foo → C:/foo on Windows; file:///home → /home
  if (/^\/[A-Za-z]:[\\/]/.test(raw)) {
    raw = raw.slice(1);
  }
  let line: number | undefined;
  let col: number | undefined;
  // path.ext:12 or path.ext:12:3 — require a dotted extension before :line
  const m = raw.match(/^(.+\.\w{1,8}):(\d+)(?:-\d+)?(?::(\d+))?$/);
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

/**
 * Transcript / markdown href that should open as a file.
 * Bare `` `backend.py` `` is not enough — models backtick identifiers constantly.
 * Require a filesystem prefix or a directory separator plus a filename extension.
 */
export function looksLikePathInlineCode(text: string): boolean {
  const t = (text || "").trim();
  if (!t || t.includes("\n") || t.length > 260) return false;
  if (/^[-+]/.test(t)) return false;
  if (/\s/.test(t)) return looksLikeSpacedFilePath(t);
  if (looksLikeShellCommand(t)) return false;
  if (!looksLikeFilePath(t)) return false;
  const clean = t
    .replace(/^file:\/\//i, "")
    .replace(/^["']|["']$/g, "");
  return hasFilesystemPrefix(clean) || /[\\/]/.test(clean);
}

/**
 * Hermes `#session/` / `#preview/` style: identity-bound destinations that
 * survive markdown sanitization. Never invent these from prose.
 */
export function commandMarkdownHref(id: string): string {
  return `#command/${encodeURIComponent(id)}`;
}

export function commandRefFromMarkdownHref(href?: string): string | null {
  if (!href?.startsWith("#command/")) return null;
  try {
    return decodeURIComponent(href.slice("#command/".length)) || null;
  } catch {
    return null;
  }
}

export function fileMarkdownHref(path: string): string {
  return `#file/${encodeURIComponent(path)}`;
}

export function fileRefFromMarkdownHref(href?: string): string | null {
  if (!href?.startsWith("#file/")) return null;
  try {
    return decodeURIComponent(href.slice("#file/".length)) || null;
  } catch {
    return null;
  }
}

export type TranscriptTarget =
  | { kind: "url"; href: string }
  | { kind: "spill"; href: string }
  | { kind: "job"; href: string }
  | { kind: "file"; href: string }
  | { kind: "command"; command: string; id: string; output: string }
  | { kind: "none" };

/**
 * Classify a markdown href / autolink target for transcript clicks.
 * Commands light up only when a live/completed agent session is registered
 * (Hermes `procId` / Codex trusted-destination rule).
 */
export function classifyTranscriptTarget(href: string): TranscriptTarget {
  const h = (href || "").trim();
  if (!h) return { kind: "none" };
  if (isExternalUrl(h)) return { kind: "url", href: h };
  if (looksLikeSpillUri(h)) return { kind: "spill", href: h };
  if (looksLikeJobId(h)) return { kind: "job", href: h };
  const commandId = commandRefFromMarkdownHref(h);
  if (commandId) {
    const live = lookupAgentCommandSessionById(commandId);
    return live
      ? { kind: "command", command: live.command, id: live.id, output: live.output }
      : { kind: "none" };
  }
  const fileRef = fileRefFromMarkdownHref(h);
  if (fileRef && looksLikeFilePath(fileRef)) {
    return { kind: "file", href: fileRef };
  }
  if (looksLikePathInlineCode(h)) return { kind: "file", href: h };
  const live = lookupAgentCommandSession(h);
  if (live) {
    return { kind: "command", command: live.command, id: live.id, output: live.output };
  }
  return { kind: "none" };
}

export function classifyTranscriptHref(href: string): AgentLinkKind {
  return classifyTranscriptTarget(href).kind;
}

export type AgentLinkKind =
  | "url"
  | "file"
  | "command"
  | "image"
  | "workspace"
  | "job"
  | "spill"
  | "none";

/**
 * True for a concrete spilled-output URI ``spill://{session}/{tool_call}``.
 * Rejects directory forms (``spill://`` / ``spill://session``) and unsafe ids.
 */
export function looksLikeSpillUri(href: string): boolean {
  const t = (href || "").trim();
  if (!t || t.length > 200) return false;
  return /^spill:\/\/[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(t);
}

/**
 * True for durable Puppetmaster job ids (`job_` + 12 hex) and Marionette local
 * ids used in the tracker (`local-swarm-*`, `local-cmd-*`, `local-{short}`).
 * Rejects bare UUIDs, random hex, and filesystem paths.
 */
export function looksLikeJobId(id: string): boolean {
  const t = (id || "").trim();
  if (!t || t.length > 80) return false;
  if (/[\\/\s]/.test(t)) return false;
  // Durable substrate id minted by Puppetmaster.
  if (/^job_[a-fA-F0-9]{12}$/.test(t)) return true;
  // Marionette local / placeholder ids (swarm pills, cmd batches, short locals).
  if (/^local-(?:swarm|cmd(?:batch)?)-[A-Za-z0-9][A-Za-z0-9_-]*$/.test(t)) {
    return true;
  }
  // Short local-{token} forms (e.g. local-bf1b30f4) — single segment, no UUID shape.
  if (/^local-[A-Za-z0-9]{1,32}$/.test(t)) return true;
  return false;
}

/** Classify an ActionCard goal by tool kind. */
export function classifyActionGoal(
  kind: string,
  goal: string
): { linkKind: AgentLinkKind; value: string } {
  const k = (kind || "").toLowerCase();
  const g = (goal || "").trim();
  if (!g) return { linkKind: "none", value: "" };
  if (k === "view_image") {
    return { linkKind: "image", value: g };
  }
  if (k === "open_project") {
    return { linkKind: "workspace", value: g };
  }
  if (
    k === "read_file" ||
    k === "write_file" ||
    k === "edit_file" ||
    k === "hash_edit"
  ) {
    return { linkKind: "file", value: g };
  }
  if (k === "web_fetch") {
    return { linkKind: "url", value: g };
  }
  // Search / wiki queries are prose. looksLikeShellCommand treats any
  // spaced string as a command, which parked CodeGraph queries on the Term rail.
  if (
    k === "search_codegraph"
    || k === "search_files"
    || k === "search_state"
    || k === "search_tools"
    || k === "web_search"
    || k === "query_wiki"
  ) {
    return { linkKind: "none", value: g };
  }
  // Worker / shell dispatches are processes, not files — even when the goal
  // text embeds a path (looksLikeFilePath would otherwise open the editor).
  if (
    k === "run_command"
    || k === "run_ipython"
    || k === "run_implement"
    || k === "run_parallel"
    || k === "run_swarm"
    || k === "route_task"
    || k === "shell"
    || k === "bash"
    || k === "execute"
  ) {
    return { linkKind: "command", value: g };
  }
  if (isExternalUrl(g)) return { linkKind: "url", value: g };
  if (k === "spill" || looksLikeSpillUri(g)) return { linkKind: "spill", value: g };
  // Explicit job-id goals (ActionCard KV / synthetic classify) — not prose autolink.
  if (k === "job" || looksLikeJobId(g)) return { linkKind: "job", value: g };
  // Unknown kinds: never fall through to file when the goal is shell-like.
  if (looksLikeShellCommand(g)) return { linkKind: "command", value: g };
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

/** Cmd/Ctrl+click or middle-click: system browser, not the in-app pane. */
export function openAgentUrlExternal(url: string): void {
  if (!url || !isExternalUrl(url)) return;
  const ipc = (window as unknown as { harnessIPC?: { openExternal?: (href: string) => void } }).harnessIPC;
  if (ipc && typeof ipc.openExternal === "function") {
    try { ipc.openExternal(url); return; } catch { /* fall through */ }
  }
  try { window.open(url, "_blank", "noopener,noreferrer"); } catch { /* ignore */ }
}

function wantsSystemBrowser(e?: { metaKey?: boolean; ctrlKey?: boolean; button?: number }): boolean {
  if (!e) return false;
  return Boolean(e.metaKey || e.ctrlKey || e.button === 1);
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

/**
 * Open an image in the transcript lightbox. Accepts http(s), data:, or a
 * repo/uploaded path (resolved to api.imageUrl by the Conversation listener).
 */
export function openAgentImage(pathOrUrl: string): void {
  const v = (pathOrUrl || "").trim();
  if (!v) return;
  try {
    window.dispatchEvent(
      new CustomEvent("harness-open-image", {
        detail: { path: v, url: isExternalUrl(v) || v.startsWith("data:") ? v : undefined },
      }),
    );
  } catch {
    /* ignore */
  }
}

/** Open a folder as the active workspace (same path as WorkspaceChip). */
export function openAgentWorkspace(path: string): void {
  const p = (path || "").trim();
  if (!p) return;
  try {
    window.dispatchEvent(
      new CustomEvent("harness-open-workspace", { detail: { path: p } }),
    );
  } catch {
    /* ignore */
  }
}

/**
 * Focus the Swarm Tracker tab and expand/scroll to a job row.
 * Does not invent a Puppetmaster dashboard URL — tracker focus only.
 *
 * Queues the job id before dispatch so a late-mounted SwarmPane still
 * expands/scrolls (harness-open-swarm-job is easy to miss when the pane
 * mounts only after harness-focus-tab opens the right rail).
 */
export function openAgentSwarmJob(jobId: string, artifactId?: string): void {
  const id = (jobId || "").trim();
  const artifact = (artifactId || "").trim();
  if (!id || !looksLikeJobId(id)) return;
  try {
    queuePendingSwarmOpenJob(id, artifact);
    window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "swarm" }));
    window.dispatchEvent(
      new CustomEvent("harness-open-swarm-job", {
        detail: { jobId: id, ...(artifact ? { artifactId: artifact } : {}) },
      }),
    );
  } catch {
    /* ignore */
  }
}

/** Awaiting swarms own tracker navigation; other live work owns Terminal. */
export function openAgentBusyDetail(status: string, jobIds: readonly string[]): void {
  if (status === "awaiting_swarm") {
    const jobId = jobIds.find((id) => looksLikeJobId(String(id || "").trim()));
    if (jobId) {
      openAgentSwarmJob(jobId);
      return;
    }
  }
  try {
    window.dispatchEvent(new CustomEvent("harness-focus-tab", {
      detail: status === "awaiting_swarm" ? "swarm" : "terminal",
    }));
  } catch {
    /* ignore */
  }
}

/**
 * Open a spilled tool-output URI in the operator peek surface.
 * Conversation fetches ``/api/spill/read`` and paints a read-only modal.
 */
export function openAgentSpill(uri: string): void {
  const u = (uri || "").trim();
  if (!u || !looksLikeSpillUri(u)) return;
  try {
    window.dispatchEvent(
      new CustomEvent("harness-open-spill", { detail: { uri: u } }),
    );
  } catch {
    /* ignore */
  }
}

export type OpenAgentCommandOpts = {
  /** When true, inject into the interactive user PTY. Default reveals the agent mirror. */
  run?: boolean;
  /** Stable process/card id for the agent mirror (defaults to hash of command). */
  id?: string;
  /** Captured stdout/stderr snapshot to seed/sync into the mirror. */
  output?: string;
};

/**
 * Focus the Terminal tab. Default click reveals a read-only agent command
 * session (`$ cmd` + output). `run: true` injects into the interactive ConPTY.
 */
export function openAgentCommand(command: string, opts?: OpenAgentCommandOpts): void {
  const cmd = (command || "").trim();
  if (!cmd) return;
  const byId = opts?.id ? lookupAgentCommandSessionById(opts.id) : null;
  const byCmd = lookupAgentCommandSession(cmd);
  const live = byId || byCmd;
  const id = String(live?.id || "").trim();
  const output = String(opts?.output || live?.output || "");
  // Speculative transcript clicks used to mint a blank agent-terminal
  // mirror. Fail closed unless this is an interactive inject or a real
  // registered / tool-card reveal (Hermes openAgentTerminal(procId)).
  if (!opts?.run && !id && !output) return;
  try {
    window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "terminal" }));
    if (opts?.run) {
      window.dispatchEvent(
        new CustomEvent("harness-run-command", { detail: { command: cmd } })
      );
      return;
    }
    const revealId = id || (output ? (String(opts?.id || "").trim() || stableCommandId(cmd)) : "");
    if (!revealId) return;
    registerAgentCommandSession({ id: revealId, command: cmd, output });
    seedAgentTerminalCommand(revealId, cmd);
    if (output) syncAgentTerminalSnapshot(revealId, output);
    window.dispatchEvent(
      new CustomEvent("harness-open-agent-terminal", {
        detail: { id: revealId, command: cmd, output },
      })
    );
  } catch {
    /* ignore */
  }
}

/** Best-effort live sync for an open agent mirror while a card's output grows. */
export function syncAgentCommandOutput(id: string, output: string): void {
  const procId = String(id || "").trim();
  const snap = String(output || "");
  if (!procId || !snap) return;
  const known = lookupAgentCommandSessionById(procId);
  if (known) {
    registerAgentCommandSession({ id: procId, command: known.command, output: snap });
  }
  try {
    syncAgentTerminalSnapshot(procId, snap);
    window.dispatchEvent(
      new CustomEvent("harness-sync-agent-terminal", {
        detail: { id: procId, output: snap },
      })
    );
  } catch {
    /* ignore */
  }
}

/** Route a markdown href click (or synthetic open). */
export function openAgentLink(href: string, e?: { preventDefault(): void; metaKey?: boolean; ctrlKey?: boolean; button?: number }): void {
  if (!href) return;
  const target = classifyTranscriptTarget(href);
  if (target.kind === "none") {
    e?.preventDefault();
    return;
  }
  e?.preventDefault();
  if (target.kind === "url") {
    if (wantsSystemBrowser(e)) openAgentUrlExternal(target.href);
    else openAgentUrl(target.href);
    return;
  }
  if (target.kind === "spill") {
    openAgentSpill(target.href);
    return;
  }
  if (target.kind === "job") {
    openAgentSwarmJob(target.href);
    return;
  }
  if (target.kind === "file") {
    openAgentFile(target.href);
    return;
  }
  if (target.kind === "command") {
    openAgentCommand(target.command, {
      id: target.id,
      output: target.output,
      run: false,
    });
  }
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
const BARE_SPILL = /spill:\/\/[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+/g;
/** Path segment with optional single spaces (`My Projects`) — PATH_IN_TEXT parity. */
const PATH_SEG = String.raw`[\w.-]+(?: [\w-]+)*`;
/** Filename body before the final .ext (no dotted intermediate words). */
const PATH_FILE = String.raw`[\w-]+(?: [\w-]+)*`;
/**
 * Windows abs, POSIX abs, ./rel, path/with/slash.ext — optional :line[:col].
 * Alternation order matches PATH_IN_TEXT: unquoted spaced (dir sep required)
 * before classic no-space, so `/Users/me/My Projects/app.ts` is not truncated
 * to a wrong-target suffix like `Projects/app.ts`.
 */
const BARE_PATH = new RegExp(
  [
    String.raw`(?:^|[\s(])(`,
    // Unquoted spaced abs/rel: must contain a space; directory separator required.
    String.raw`(?:[A-Za-z]:[\\/]|\/|\.{1,2}[\\/])(?=[^\n]* )(?:${PATH_SEG}[\\/])+${PATH_FILE}\.\w{1,8}(?::\d+){0,2}`,
    String.raw`|`,
    // Classic no-space abs/rel
    String.raw`(?:[A-Za-z]:[\\/]|\/|\.{1,2}[\\/])[^\s\`"'<>\]|]+?\.\w{1,8}(?::\d+){0,2}`,
    String.raw`|`,
    // Relative path/with/slash.ext (no leading ./)
    String.raw`(?:[\w.-]+\/)+[\w.-]+\.\w{1,8}(?::\d+){0,2}`,
    String.raw`)(?=[\s).,]|$)`,
  ].join(""),
  "g",
);

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
  work = work.replace(BARE_SPILL, (m) => {
    if (!looksLikeSpillUri(m)) return m;
    return `[${m}](${m})`;
  });
  work = work.replace(BARE_PATH, (full, pathPart: string) => {
    if (isExternalUrl(pathPart) || !looksLikeFilePath(pathPart)) return full;
    // Preserve the leading delimiter captured by (?:^|[\s(])
    const lead = full.slice(0, full.length - pathPart.length);
    // Angle-bracket destinations keep spaces intact for CommonMark/remark.
    const dest = /\s/.test(pathPart) ? `<${pathPart}>` : pathPart;
    return `${lead}[\`${pathPart}\`](${dest})`;
  });

  return work.replace(/\u0000(\d+)\u0000/g, (_, i) => slots[Number(i)] || "");
}
