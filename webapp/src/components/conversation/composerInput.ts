/**
 * Pure composer input helpers: slash/mention triggers, inserts, drop paths,
 * and list navigation. React wiring stays in Conversation / ComposerDock.
 */

export type ComposerTrigger =
  | { kind: "slash"; query: string }
  | { kind: "mention"; query: string; atIndex: number }
  | { kind: "none" };

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
      if (!/\s/.test(textAfterAt)) {
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
  const { osPath, repo, uploadedPath, isDirectory } = opts;
  const kind = isDirectory ? "folder" : "file";
  const insideRepo =
    !!osPath && !!repo && (osPath === repo || osPath.startsWith(repo + "/"));
  if (insideRepo) {
    const rel = osPath.slice(repo.length + 1);
    if (!rel) return null;
    return formatMentionToken(rel, kind);
  }
  if (!uploadedPath) return null;
  const rel =
    repo && uploadedPath.startsWith(repo + "/")
      ? uploadedPath.slice(repo.length + 1)
      : uploadedPath;
  if (!rel) return null;
  return formatMentionToken(rel, kind);
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
