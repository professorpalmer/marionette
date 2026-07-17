/** Normalize OS path separators for tab identity comparisons. */
export function normalizeTabPath(path: string): string {
  return path.replace(/\\/g, "/");
}

/** True when candidate equals root or lives under root (after normalize). */
export function pathIsUnder(candidate: string, root: string): boolean {
  const c = normalizeTabPath(candidate);
  const r = normalizeTabPath(root);
  return c === r || c.startsWith(r + "/");
}

export type EditorTab = {
  path: string;
  isDirty: boolean;
  line?: number;
  col?: number;
};

/** Drop tabs whose path equals or is under a deleted path. */
export function filterTabsAfterDelete(tabs: EditorTab[], deleted: string): EditorTab[] {
  return tabs.filter((t) => !pathIsUnder(t.path, deleted));
}

/** Remap open-tab paths after a rename (exact match or nested under `from`). */
export function remapTabsAfterRename(
  tabs: EditorTab[],
  from: string,
  to: string,
): EditorTab[] {
  return tabs.map((t) => {
    if (normalizeTabPath(t.path) === normalizeTabPath(from)) {
      return { ...t, path: to };
    }
    const norm = normalizeTabPath(t.path);
    const fromNorm = normalizeTabPath(from);
    if (norm.startsWith(fromNorm + "/")) {
      return { ...t, path: to + t.path.slice(from.length) };
    }
    return t;
  });
}

/** Remap the active tab id after a rename (chat stays chat). */
export function remapActiveTabAfterRename(
  activeTab: string,
  from: string,
  to: string,
): string {
  if (normalizeTabPath(activeTab) === normalizeTabPath(from)) return to;
  const norm = normalizeTabPath(activeTab);
  const fromNorm = normalizeTabPath(from);
  if (norm.startsWith(fromNorm + "/")) return to + activeTab.slice(from.length);
  return activeTab;
}
