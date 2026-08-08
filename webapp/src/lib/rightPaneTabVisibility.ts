/** Versioned visibility preferences for optional RightPane tabs. */

export type RightPaneTabId =
  | "state"
  | "swarm"
  | "files"
  | "git"
  | "worktrees"
  | "terminal"
  | "browser"
  | "settings"
  | "checkpoints"
  | "review";

export const RIGHT_PANE_TAB_VISIBILITY_KEY = "pmharness.rightPane.visibleTabs.v1";

export const RIGHT_PANE_OPTIONAL_TAB_IDS = [
  "worktrees",
  "review",
  "checkpoints",
] as const satisfies readonly RightPaneTabId[];

export type RightPaneOptionalTabId = (typeof RIGHT_PANE_OPTIONAL_TAB_IDS)[number];
export type RightPaneTabVisibility = Record<RightPaneTabId, boolean>;

export const DEFAULT_RIGHT_PANE_TAB_VISIBILITY: RightPaneTabVisibility = {
  state: true,
  swarm: true,
  files: true,
  git: true,
  worktrees: false,
  terminal: true,
  browser: true,
  settings: true,
  checkpoints: false,
  review: false,
};

export const RIGHT_PANE_TAB_VISIBILITY_META: {
  id: RightPaneOptionalTabId;
  label: string;
}[] = [
  { id: "worktrees", label: "Worktrees" },
  { id: "review", label: "Review" },
  { id: "checkpoints", label: "History" },
];

export function normalizeRightPaneTabVisibility(raw: unknown): RightPaneTabVisibility {
  const next = { ...DEFAULT_RIGHT_PANE_TAB_VISIBILITY };
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return next;

  const source = raw as Record<string, unknown>;
  for (const id of RIGHT_PANE_OPTIONAL_TAB_IDS) {
    if (typeof source[id] === "boolean") next[id] = source[id];
  }
  return next;
}

export function loadRightPaneTabVisibility(
  storage: Pick<Storage, "getItem"> | null | undefined = typeof localStorage !== "undefined"
    ? localStorage
    : undefined,
): RightPaneTabVisibility {
  if (!storage) return { ...DEFAULT_RIGHT_PANE_TAB_VISIBILITY };
  try {
    const raw = storage.getItem(RIGHT_PANE_TAB_VISIBILITY_KEY);
    return raw ? normalizeRightPaneTabVisibility(JSON.parse(raw)) : { ...DEFAULT_RIGHT_PANE_TAB_VISIBILITY };
  } catch {
    return { ...DEFAULT_RIGHT_PANE_TAB_VISIBILITY };
  }
}

export function saveRightPaneTabVisibility(
  visibility: RightPaneTabVisibility,
  storage: Pick<Storage, "setItem"> | null | undefined = typeof localStorage !== "undefined"
    ? localStorage
    : undefined,
): void {
  if (!storage) return;
  try {
    storage.setItem(
      RIGHT_PANE_TAB_VISIBILITY_KEY,
      JSON.stringify(normalizeRightPaneTabVisibility(visibility)),
    );
  } catch {
    /* Private browsing and quota failures should not block the current session. */
  }
}

export function toggleRightPaneTabVisibility(
  current: RightPaneTabVisibility,
  id: RightPaneOptionalTabId,
): RightPaneTabVisibility {
  return { ...current, [id]: !current[id] };
}
