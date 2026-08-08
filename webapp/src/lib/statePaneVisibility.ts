/** Versioned State-pane card visibility prefs (CodeGraph / Wiki / Environment / MCP). */

export type StatePaneCardId = "codegraph" | "wiki" | "environment" | "mcp";

export const STATE_PANE_CARD_IDS: readonly StatePaneCardId[] = [
  "codegraph",
  "wiki",
  "environment",
  "mcp",
] as const;

/** Bump the suffix when the stored shape changes incompatibly. */
export const STATE_PANE_VISIBLE_CARDS_KEY = "pmharness.statePane.visibleCards.v1";

export type StatePaneVisibleCards = Record<StatePaneCardId, boolean>;

/**
 * Calm minimal default: keep primary health (CodeGraph) and essential MCP
 * actions visible; Wiki stays as a quiet status strip; Environment is optional
 * and starts hidden.
 */
export const DEFAULT_STATE_PANE_VISIBLE_CARDS: StatePaneVisibleCards = {
  codegraph: true,
  wiki: true,
  environment: false,
  mcp: true,
};

export const STATE_PANE_CARD_META: {
  id: StatePaneCardId;
  label: string;
  short: string;
}[] = [
  { id: "codegraph", label: "CodeGraph", short: "CG" },
  { id: "wiki", label: "Wiki", short: "Wiki" },
  { id: "environment", label: "Environment", short: "Env" },
  { id: "mcp", label: "MCP", short: "MCP" },
];

/** Coerce unknown storage into a safe visibility map; never leave all cards off. */
export function normalizeStatePaneVisibleCards(raw: unknown): StatePaneVisibleCards {
  const next: StatePaneVisibleCards = { ...DEFAULT_STATE_PANE_VISIBLE_CARDS };
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return next;
  }
  const obj = raw as Record<string, unknown>;
  for (const id of STATE_PANE_CARD_IDS) {
    if (typeof obj[id] === "boolean") next[id] = obj[id];
  }
  if (!STATE_PANE_CARD_IDS.some((id) => next[id])) {
    next.codegraph = true;
  }
  return next;
}

export function loadStatePaneVisibleCards(
  storage: Pick<Storage, "getItem"> | null | undefined = typeof localStorage !== "undefined"
    ? localStorage
    : undefined,
): StatePaneVisibleCards {
  if (!storage) return { ...DEFAULT_STATE_PANE_VISIBLE_CARDS };
  try {
    const raw = storage.getItem(STATE_PANE_VISIBLE_CARDS_KEY);
    if (raw == null || raw === "") return { ...DEFAULT_STATE_PANE_VISIBLE_CARDS };
    return normalizeStatePaneVisibleCards(JSON.parse(raw));
  } catch {
    return { ...DEFAULT_STATE_PANE_VISIBLE_CARDS };
  }
}

export function saveStatePaneVisibleCards(
  cards: StatePaneVisibleCards,
  storage: Pick<Storage, "setItem"> | null | undefined = typeof localStorage !== "undefined"
    ? localStorage
    : undefined,
): void {
  if (!storage) return;
  try {
    storage.setItem(
      STATE_PANE_VISIBLE_CARDS_KEY,
      JSON.stringify(normalizeStatePaneVisibleCards(cards)),
    );
  } catch {
    /* quota / private mode — in-memory state still works for the session */
  }
}

/** Toggle one card; refuse to hide the last remaining visible card. */
export function toggleStatePaneCardVisibility(
  current: StatePaneVisibleCards,
  id: StatePaneCardId,
): StatePaneVisibleCards {
  const next: StatePaneVisibleCards = { ...current, [id]: !current[id] };
  if (!STATE_PANE_CARD_IDS.some((cardId) => next[cardId])) {
    return current;
  }
  return next;
}

/** Force a card visible (e.g. harness-expand-mcp / wiki connect). */
export function revealStatePaneCard(
  current: StatePaneVisibleCards,
  id: StatePaneCardId,
): StatePaneVisibleCards {
  if (current[id]) return current;
  return { ...current, [id]: true };
}
