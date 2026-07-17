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
