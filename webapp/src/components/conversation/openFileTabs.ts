/**
 * Pure helpers for the in-conversation file-editor tab strip.
 */

export type EditorTab = {
  path: string;
  isDirty: boolean;
  line?: number;
  col?: number;
};

/** Upsert a tab when harness-open-file fires (navigate or add). */
export function upsertOpenTab(
  tabs: EditorTab[],
  filePath: string,
  line?: number,
  col?: number,
): EditorTab[] {
  const exists = tabs.some((t) => t.path === filePath);
  if (exists) {
    return tabs.map((t) =>
      t.path === filePath ? { ...t, line, col } : t
    );
  }
  return [...tabs, { path: filePath, isDirty: false, line, col }];
}

export function closeTabResult(
  tabs: EditorTab[],
  path: string,
  activeTab: string,
): { tabs: EditorTab[]; activeTab: string } {
  const nextTabs = tabs.filter((t) => t.path !== path);
  return {
    tabs: nextTabs,
    activeTab: activeTab === path ? "chat" : activeTab,
  };
}

export function setTabDirty(
  tabs: EditorTab[],
  path: string,
  isDirty: boolean,
): EditorTab[] {
  return tabs.map((t) => (t.path === path ? { ...t, isDirty } : t));
}

export function tabHasDirty(tabs: EditorTab[], path?: string): boolean {
  if (path != null) {
    return tabs.some((t) => t.path === path && t.isDirty);
  }
  return tabs.some((t) => t.isDirty);
}

export function otherTabsHaveDirty(tabs: EditorTab[], keepPath: string): boolean {
  return tabs.some((t) => t.path !== keepPath && t.isDirty);
}

export type FileResolvePayload = {
  ok?: boolean;
  path?: string;
  exact?: boolean;
  error?: string;
  candidates?: string[];
};

export type FileResolveChoice =
  | { path: string }
  | { toast: string };

/**
 * Map /api/file/resolve onto an editor path. Transcript clicks fail closed
 * when the file is missing; file-tree clicks may fall back to the given path.
 */
export function chooseResolvedFilePath(
  requested: string,
  resolved: FileResolvePayload | null | undefined,
  opts?: { trusted?: boolean },
): FileResolveChoice | null {
  const hint = (requested || "").trim();
  if (!hint) return null;
  if (resolved?.ok && resolved.path) return { path: resolved.path };
  const candidates = Array.isArray(resolved?.candidates)
    ? resolved.candidates.map((c) => String(c || "").trim()).filter(Boolean)
    : [];
  if (candidates.length === 1) return { path: candidates[0] };
  if (candidates.length > 1) {
    return { toast: `Multiple files match ${hint}; use a more specific path.` };
  }
  if (opts?.trusted) return { path: hint };
  return { toast: `Couldn't open ${hint}.` };
}
