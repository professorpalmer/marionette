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

export function buildMentionInsert(
  input: string,
  mentionIndex: number,
  selectionStart: number,
  fileName: string,
): { next: string; cursor: number } {
  const before = input.slice(0, mentionIndex);
  const after = input.slice(selectionStart || mentionIndex);
  const next = before + "@" + fileName + " " + after;
  return { next, cursor: mentionIndex + fileName.length + 2 };
}

export function buildSymbolInsert(
  input: string,
  mentionIndex: number,
  selectionStart: number,
  symbolName: string,
): { next: string; cursor: number } {
  const before = input.slice(0, mentionIndex);
  const after = input.slice(selectionStart || mentionIndex);
  const next = before + "@symbol:" + symbolName + " " + after;
  return { next, cursor: mentionIndex + symbolName.length + 9 };
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
 * Resolve a dropped non-image file to an @-mention token.
 * Returns null when the path has spaces (caller should surface an error).
 */
export function mentionTokenForDroppedPath(opts: {
  osPath: string;
  repo: string;
  uploadedPath?: string;
}): string | null {
  const { osPath, repo, uploadedPath } = opts;
  const insideRepo =
    !!osPath && !!repo && (osPath === repo || osPath.startsWith(repo + "/"));
  if (insideRepo) {
    const rel = osPath.slice(repo.length + 1);
    if (/\s/.test(rel)) return null;
    return `@${rel}`;
  }
  if (!uploadedPath) return null;
  const rel =
    repo && uploadedPath.startsWith(repo + "/")
      ? uploadedPath.slice(repo.length + 1)
      : uploadedPath;
  if (/\s/.test(rel)) return null;
  return `@${rel}`;
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
