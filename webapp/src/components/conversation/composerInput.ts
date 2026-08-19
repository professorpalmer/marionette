/**
 * Pure composer input helpers: slash/mention triggers, inserts, drop paths,
 * and list navigation. React wiring stays in Conversation / ComposerDock.
 */

export type ComposerTrigger =
  | { kind: "slash"; query: string }
  | { kind: "mention"; query: string; atIndex: number }
  | { kind: "none" };

/**
 * True while the caret is still inside an open @-mention query.
 * Allows spaces for type-filtering spaced folder/file names (Cursor-style),
 * but closes once a picker-inserted token looks complete so the next word
 * is not swallowed back into the mention.
 */
function isActiveMentionQuery(textAfterAt: string): boolean {
  if (textAfterAt.includes("\n")) return false;

  const kindMatch = /^(folder:|symbol:|codebase:|terminal:)/i.exec(textAfterAt);
  const kind = kindMatch ? kindMatch[1].toLowerCase() : "";
  const body = kindMatch ? textAfterAt.slice(kindMatch[0].length) : textAfterAt;

  // Terminal refs are inserted complete (`@terminal:term:12`). Never open
  // the file picker on that prefix.
  if (kind === "terminal:") return false;

  // Quoted path/filter: stay open only until the closing quote appears.
  if (body.startsWith('"')) {
    return body.length === 1 || body.indexOf('"', 1) === -1;
  }

  // Picker inserts `@folder:…` / `@symbol:…` with a trailing space — do not
  // reopen. Users filter spaced names via bare `@my docs`, not these prefixes.
  if ((kind === "folder:" || kind === "symbol:") && /\s/.test(body)) {
    return false;
  }

  // Bare `@codebase` insert ends with a trailing space (no colon filter).
  if (!kind && /^codebase\s/i.test(textAfterAt)) {
    return false;
  }

  // Unquoted: allow spaces in the filter (`@my file`), but a first token that
  // already looks like `file.ext` means the mention was completed.
  const spaceIdx = body.search(/\s/);
  if (spaceIdx !== -1) {
    const firstToken = body.slice(0, spaceIdx);
    if (/\.\w{1,8}(?::\d+){0,2}$/.test(firstToken)) {
      return false;
    }
  }

  return true;
}

/** Detect slash-command or @-mention trigger at the caret. */
export function detectComposerTrigger(
  val: string,
  cursorPosition: number,
): ComposerTrigger {
  if (val.startsWith("/") && !val.includes("\n") && cursorPosition <= val.length) {
    const spaceIdx = val.indexOf(" ");
    if (spaceIdx === -1 || cursorPosition <= spaceIdx) {
      return { kind: "slash", query: val.slice(1) };
    }
  }

  const lastAt = val.lastIndexOf("@", cursorPosition - 1);
  if (lastAt !== -1) {
    const prefix = lastAt === 0 ? "" : val[lastAt - 1];
    if (prefix === "" || /\s/.test(prefix)) {
      const textAfterAt = val.slice(lastAt + 1, cursorPosition);
      if (isActiveMentionQuery(textAfterAt)) {
        return { kind: "mention", query: textAfterAt, atIndex: lastAt };
      }
    }
  }
  return { kind: "none" };
}

/**
 * Quote a mention path when it contains whitespace so the stream tokenizer
 * can round-trip macOS/Windows spaced paths (@"my file.ts").
 */
export function quoteMentionPathIfNeeded(path: string): string {
  const trimmed = String(path || "");
  if (!trimmed) return trimmed;
  if (trimmed.startsWith('"') && trimmed.endsWith('"') && trimmed.length >= 2) {
    return trimmed;
  }
  if (!/\s/.test(trimmed)) return trimmed;
  return `"${trimmed.replace(/"/g, "")}"`;
}

/** Build an @-mention token for a file or folder path (quoted when needed). */
export function formatMentionToken(
  path: string,
  kind: "file" | "folder" = "file",
): string {
  const body = quoteMentionPathIfNeeded(path);
  return kind === "folder" ? `@folder:${body}` : `@${body}`;
}

export function buildMentionInsert(
  input: string,
  mentionIndex: number,
  selectionStart: number,
  fileName: string,
): { next: string; cursor: number } {
  const before = input.slice(0, mentionIndex);
  const after = input.slice(selectionStart || mentionIndex);
  const tokenPath = quoteMentionPathIfNeeded(fileName);
  const next = before + "@" + tokenPath + " " + after;
  return { next, cursor: mentionIndex + tokenPath.length + 2 };
}

export function buildSymbolInsert(
  input: string,
  mentionIndex: number,
  selectionStart: number,
  symbolName: string,
): { next: string; cursor: number } {
  const before = input.slice(0, mentionIndex);
  const after = input.slice(selectionStart || mentionIndex);
  const tokenName = quoteMentionPathIfNeeded(symbolName);
  const next = before + "@symbol:" + tokenName + " " + after;
  return { next, cursor: mentionIndex + tokenName.length + 9 };
}

/** Insert an honest `@folder:path` token the send path can resolve. */
export function buildFolderInsert(
  input: string,
  mentionIndex: number,
  selectionStart: number,
  folderPath: string,
): { next: string; cursor: number } {
  const before = input.slice(0, mentionIndex);
  const after = input.slice(selectionStart || mentionIndex);
  const tokenPath = quoteMentionPathIfNeeded(folderPath);
  const next = before + "@folder:" + tokenPath + " " + after;
  return { next, cursor: mentionIndex + tokenPath.length + 9 };
}

/**
 * Offer the Codebase picker row when the user is typing `@code…` / `@codebase`
 * (or `@codebase:filter`). Empty `@` also offers it as a pinned scope.
 */
export function codebaseMentionMatches(query: string): boolean {
  const q = String(query || "").toLowerCase();
  if (!q) return true;
  return "codebase".startsWith(q) || q.startsWith("codebase");
}

/** Optional filter after `@codebase:` in the live mention search text. */
export function codebaseQueryFromMentionSearch(search: string): string | undefined {
  const raw = String(search || "");
  const lower = raw.toLowerCase();
  if (!lower.startsWith("codebase:")) return undefined;
  const filter = raw.slice("codebase:".length);
  return filter || undefined;
}

/** Insert `@codebase` or `@codebase:query` for the send-path resolver. */
export function buildCodebaseInsert(
  input: string,
  mentionIndex: number,
  selectionStart: number,
  queryFilter?: string,
): { next: string; cursor: number } {
  const before = input.slice(0, mentionIndex);
  const after = input.slice(selectionStart || mentionIndex);
  const filter = String(queryFilter || "").trim();
  const token = filter
    ? `@codebase:${quoteMentionPathIfNeeded(filter)}`
    : "@codebase";
  const next = before + token + " " + after;
  return { next, cursor: mentionIndex + token.length + 1 };
}

/** Cap mention picker hits (files or folders) without dumping the whole tree. */
export function filterMentionPaths(
  paths: string[],
  query: string,
  limit = 10,
): string[] {
  const q = query.toLowerCase();
  if (!q) return paths.slice(0, limit);
  return paths.filter((p) => p.toLowerCase().includes(q)).slice(0, limit);
}

export function filterSlashCommands<T extends { cmd: string }>(
  commands: T[],
  slashSearch: string,
): T[] {
  const prefix = "/" + slashSearch.toLowerCase();
  return commands.filter((s) => s.cmd.toLowerCase().startsWith(prefix));
}

/** Cycle a selection index with wrap-around (ArrowUp / ArrowDown). */
export function cycleSelectIndex(
  current: number,
  delta: 1 | -1,
  total: number,
): number {
  if (total <= 0) return 0;
  return (current + delta + total) % total;
}

/** Normalize OS paths so Windows `\` compares and mentions like POSIX `/`. */
export function normalizeOsPath(path: string): string {
  return String(path || "").replace(/\\/g, "/").replace(/\/+$/, "");
}

/** True when `osPath` is the repo root or a file/folder inside it. */
export function pathIsInsideRepo(osPath: string, repo: string): boolean {
  const a = normalizeOsPath(osPath);
  const b = normalizeOsPath(repo);
  if (!a || !b) return false;
  const al = a.toLowerCase();
  const bl = b.toLowerCase();
  return al === bl || al.startsWith(bl + "/");
}

/**
 * Electron no longer puts the OS path on `File.path`. Prefer
 * `webUtils.getPathForFile` exposed as `harnessIPC.pathForFile`.
 */
export function resolveDroppedOsPath(file: { path?: string }): string {
  const ipc =
    typeof window !== "undefined"
      ? (window as unknown as { harnessIPC?: { pathForFile?: (f: unknown) => string } })
          .harnessIPC?.pathForFile
      : undefined;
  if (typeof ipc === "function") {
    try {
      const resolved = ipc(file);
      if (resolved) return normalizeOsPath(String(resolved));
    } catch {
      // fall through to the legacy File.path field
    }
  }
  return normalizeOsPath(String(file?.path || ""));
}

/**
 * Resolve a dropped non-image file/folder to an @-mention token.
 * Paths with spaces become quoted tokens (@"path with spaces.ts") so the
 * harness tokenizer can resolve them. Directories use `@folder:rel`.
 * Returns null only when the drop is outside the repo and no upload path
 * was provided.
 */
export function mentionTokenForDroppedPath(opts: {
  osPath: string;
  repo: string;
  uploadedPath?: string;
  isDirectory?: boolean;
}): string | null {
  const osPath = normalizeOsPath(opts.osPath);
  const repo = normalizeOsPath(opts.repo);
  const uploadedPath = opts.uploadedPath ? normalizeOsPath(opts.uploadedPath) : "";
  const kind = opts.isDirectory ? "folder" : "file";
  if (pathIsInsideRepo(osPath, repo)) {
    const rel = osPath.slice(repo.length + 1);
    if (!rel) return null;
    return formatMentionToken(rel, kind);
  }
  if (!uploadedPath) return null;
  const rel =
    repo && pathIsInsideRepo(uploadedPath, repo)
      ? uploadedPath.slice(repo.length + 1)
      : uploadedPath;
  if (!rel) return null;
  return formatMentionToken(rel, kind);
}

type HarnessDropIpc = {
  pathForFile?: (f: unknown) => string;
  isDirectory?: (absPath: string) => boolean;
};

function dropIpc(): HarnessDropIpc | undefined {
  if (typeof window === "undefined") return undefined;
  return (window as unknown as { harnessIPC?: HarnessDropIpc }).harnessIPC;
}

/** True when Electron can stat `osPath` as a directory (outside-workspace ok). */
export function droppedPathIsDirectory(osPath: string): boolean {
  const path = normalizeOsPath(osPath);
  if (!path) return false;
  const probe = dropIpc()?.isDirectory;
  if (typeof probe !== "function") return false;
  try {
    return !!probe(path);
  } catch {
    return false;
  }
}

/**
 * Same noise dirs `harness/mention_context.py` skips when expanding `@folder:`.
 * Keep in sync when that set changes.
 */
export const DROP_FOLDER_SKIP_DIRS = new Set([
  ".git",
  "node_modules",
  ".venv",
  ".codegraph",
  "dist",
  "build",
  ".pytest_cache",
  "__pycache__",
  ".mypy_cache",
  ".ruff_cache",
  ".idea",
  ".vscode",
  "venv",
  ".next",
  "coverage",
  ".hermes",
  "release",
  "backend-dist",
]);

/** Aligns with `DEFAULT_FOLDER_ENTRY_CAP` in mention_context. */
export const DROP_FOLDER_FILE_CAP = 40;

type DirectoryReaderLike = {
  readEntries: (
    success: (entries: DirectoryEntryLike[]) => void,
    error?: (err: unknown) => void,
  ) => void;
};

export type DirectoryEntryLike = {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
  createReader?: () => DirectoryReaderLike;
  file?: (
    success: (file: File) => void,
    error?: (err: unknown) => void,
  ) => void;
};

async function readAllDirectoryEntries(
  reader: DirectoryReaderLike,
): Promise<DirectoryEntryLike[]> {
  const all: DirectoryEntryLike[] = [];
  for (;;) {
    const batch = await new Promise<DirectoryEntryLike[]>((resolve) => {
      try {
        reader.readEntries((entries) => resolve(entries || []), () => resolve([]));
      } catch {
        resolve([]);
      }
    });
    if (!batch.length) break;
    all.push(...batch);
  }
  return all;
}

function fileFromEntry(ent: DirectoryEntryLike): Promise<File | null> {
  if (typeof ent.file !== "function") return Promise.resolve(null);
  return new Promise((resolve) => {
    try {
      ent.file!((f) => resolve(f), () => resolve(null));
    } catch {
      resolve(null);
    }
  });
}

/** Walk a dropped directory entry; skip noise dirs; hard-cap file count. */
export async function collectFilesFromDirectoryEntry(
  entry: DirectoryEntryLike,
  opts?: { cap?: number; skipDirs?: ReadonlySet<string> },
): Promise<{ files: Array<{ file: File; relPath: string }>; truncated: boolean }> {
  const cap = opts?.cap ?? DROP_FOLDER_FILE_CAP;
  const skipDirs = opts?.skipDirs ?? DROP_FOLDER_SKIP_DIRS;
  const files: Array<{ file: File; relPath: string }> = [];
  let truncated = false;

  const walkDir = async (dir: DirectoryEntryLike, dirRel: string): Promise<void> => {
    const reader = dir.createReader?.();
    if (!reader) return;
    const children = await readAllDirectoryEntries(reader);
    for (const child of children) {
      if (files.length >= cap) {
        truncated = true;
        return;
      }
      const childRel = dirRel ? `${dirRel}/${child.name}` : child.name;
      if (child.isDirectory) {
        if (skipDirs.has(child.name)) continue;
        await walkDir(child, childRel);
        continue;
      }
      if (!child.isFile) continue;
      const file = await fileFromEntry(child);
      if (file) files.push({ file, relPath: childRel });
    }
  };

  if (entry.isFile) {
    const file = await fileFromEntry(entry);
    if (file) files.push({ file, relPath: entry.name });
    return { files, truncated };
  }
  if (entry.isDirectory) {
    await walkDir(entry, "");
  }
  return { files, truncated };
}

export type DroppedDirectoryPlan =
  | { kind: "mention"; token: string }
  | { kind: "open-workspace"; path: string }
  | { kind: "fail" };

/**
 * Inside the open repo: @folder mention. Anywhere else with an OS path:
 * open that folder as the workspace (window-drop / walk-empty fallback).
 */
export function droppedDirectoryPlan(opts: {
  osPath: string;
  repo: string;
}): DroppedDirectoryPlan {
  const token = mentionTokenForDroppedPath({
    osPath: opts.osPath,
    repo: opts.repo,
    isDirectory: true,
  });
  if (token) return { kind: "mention", token };
  const path = normalizeOsPath(opts.osPath);
  if (path) return { kind: "open-workspace", path };
  return { kind: "fail" };
}

/** Prefer the server/upload Error message over a generic flash. */
export function uploadErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error) {
    const msg = String(err.message || "").trim();
    if (msg && msg !== "Upload failed") return msg;
  }
  return fallback;
}

/** Append mention tokens to the composer, adding a leading space when needed. */
export function appendMentionsToInput(prev: string, mentions: string[]): string {
  if (mentions.length === 0) return prev;
  const sep = prev && !prev.endsWith(" ") ? " " : "";
  return prev + sep + mentions.join(" ") + " ";
}

/** Clamp selected index when the filtered list shrinks. */
export function clampSelectIndex(selected: number, total: number): number {
  if (total <= 0) return 0;
  if (selected >= total) return total - 1;
  if (selected < 0) return 0;
  return selected;
}
