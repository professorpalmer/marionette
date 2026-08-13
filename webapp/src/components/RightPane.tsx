import { useCallback, useState, useEffect, useRef } from "react";
import { X, GripVertical } from "lucide-react";
import StatePane from "./StatePane";
import BrowserPane from "./BrowserPane";
import FileTree from "./FileTree";
import SourceControl from "./SourceControl";
import WorktreesPane from "./WorktreesPane";
import SettingsShell from "./SettingsShell";
import TerminalPane from "./TerminalPane";
import CheckpointsPane from "./CheckpointsPane";
import DiffReviewPane from "./DiffReviewPane";
import SwarmPane from "./SwarmPane";
import ErrorBoundary from "./ErrorBoundary";
import { api, type PendingReview } from "../lib/api";
import { lastSelectedProjectRoot } from "../lib/panelTransition";
import { usePolling } from "../lib/usePolling";
import { writeSWRCache } from "../lib/useStaleWhileRevalidate";
import {
  loadRightPaneTabVisibility,
  saveRightPaneTabVisibility,
  type RightPaneTabVisibility,
} from "../lib/rightPaneTabVisibility";
import { beginColumnResize, endColumnResize } from "../lib/columnResize";

type Tab = "state" | "files" | "git" | "worktrees" | "terminal" | "browser" | "settings" | "checkpoints" | "review" | "swarm";

const TAB_CONFIG: Record<Tab, { label: string }> = {
  state: { label: "State" },
  files: { label: "Files" },
  git: { label: "Git" },
  worktrees: { label: "Worktrees" },
  terminal: { label: "Terminal" },
  browser: { label: "Browser" },
  settings: { label: "Settings" },
  checkpoints: { label: "History" },
  review: { label: "Review" },
  swarm: { label: "Swarm" },
};

const TAB_GROUPS: { group: string; tabs: Tab[] }[] = [
  { group: "workspace", tabs: ["state", "swarm", "files", "git", "worktrees", "terminal"] },
  { group: "changes", tabs: ["review", "checkpoints"] },
  { group: "tools", tabs: ["browser"] },
];
const PINNED_LAST: Tab = "settings";
const CANONICAL_ORDER: Tab[] = [
  ...TAB_GROUPS.flatMap(g => g.tabs),
  PINNED_LAST,
];

const CARD_LAYOUT_STORAGE_KEY = "pmharness.board.cardLayouts.v1";
const GRID_COLUMN_COUNT = 12;
const MIN_CARD_COLUMN_SPAN = 1;

type CardLayout = {
  columnSpan: number;
  customized?: boolean;
};

type CardLayouts = Partial<Record<Tab, CardLayout>>;

type CardPlacement = {
  gridColumn: string;
  gridRow: string;
  showResizeHandle: boolean;
  columnSpan: number;
  groupIndex: number;
};

function clampCardColumnSpan(value: number, fallback: number): number {
  if (!Number.isFinite(value)) return fallback;
  return Math.max(MIN_CARD_COLUMN_SPAN, Math.min(GRID_COLUMN_COUNT, Math.round(value)));
}

function readCardLayouts(): CardLayouts {
  try {
    const raw = JSON.parse(localStorage.getItem(CARD_LAYOUT_STORAGE_KEY) || "null");
    if (!raw || typeof raw !== "object") return {};
    const layouts: CardLayouts = {};
    for (const [tab, value] of Object.entries(raw as Record<string, unknown>)) {
      if (!CANONICAL_ORDER.includes(tab as Tab) || !value || typeof value !== "object") continue;
      const candidate = value as { columnSpan?: unknown; customized?: unknown };
      const columnSpan = Number(candidate.columnSpan);
      if (!Number.isFinite(columnSpan)) continue;
      layouts[tab as Tab] = {
        columnSpan: clampCardColumnSpan(columnSpan, GRID_COLUMN_COUNT),
        customized: candidate.customized === true,
      };
    }
    return layouts;
  } catch {
    return {};
  }
}

function defaultCardColumnSpan(groupCount: number): number {
  return Math.max(MIN_CARD_COLUMN_SPAN, Math.floor(GRID_COLUMN_COUNT / Math.max(1, groupCount)));
}

function cardColumnSpan(tab: Tab, layouts: CardLayouts, groupCount: number): number {
  const fallback = defaultCardColumnSpan(groupCount);
  return clampCardColumnSpan(layouts[tab]?.columnSpan ?? fallback, fallback);
}

function normalizeGroupWidths(requested: number[], preferredGroupIndex: number): number[] {
  if (requested.length === 0) return [];
  const minimumWidth: number = requested.length <= 4 ? 2 : 1;
  const widths = requested.map(width => Math.max(minimumWidth, Math.min(GRID_COLUMN_COUNT, width)));
  const totalRequested = widths.reduce((total, width) => total + width, 0);

  if (totalRequested <= GRID_COLUMN_COUNT) {
    let remaining = GRID_COLUMN_COUNT - totalRequested;
    const order = [...widths.keys()].sort((a, b) => {
      if (a === preferredGroupIndex) return -1;
      if (b === preferredGroupIndex) return 1;
      return a - b;
    });
    let cursor = 0;
    while (remaining > 0) {
      widths[order[cursor % order.length]] += 1;
      remaining -= 1;
      cursor += 1;
    }
    return widths;
  }

  const primaryIndex = preferredGroupIndex >= 0 && preferredGroupIndex < widths.length
    ? preferredGroupIndex
    : widths.indexOf(Math.max(...widths));
  const primaryWidth = Math.min(widths[primaryIndex], GRID_COLUMN_COUNT - minimumWidth * (widths.length - 1));
  const normalized = widths.map(() => minimumWidth);
  normalized[primaryIndex] = primaryWidth;
  let remaining = GRID_COLUMN_COUNT - primaryWidth - minimumWidth * (widths.length - 1);
  const secondaryOrder = [...widths.keys()]
    .filter(index => index !== primaryIndex)
    .sort((a, b) => widths[b] - widths[a]);

  for (const index of secondaryOrder) {
    if (remaining <= 0) break;
    const extraCapacity = Math.max(0, widths[index] - minimumWidth);
    const extra = Math.min(extraCapacity, remaining);
    normalized[index] += extra;
    remaining -= extra;
  }
  return normalized;
}

function buildCardPlacements(
  openCards: Tab[],
  layouts: CardLayouts,
  preferredGroupIndex: number,
): Map<Tab, CardPlacement> {
  const groupCount = Math.ceil(openCards.length / 2);
  const groups = Array.from({ length: groupCount }, (_, index) =>
    openCards.slice(index * 2, index * 2 + 2),
  );
  const requestedWidths = groups.map(group =>
    Math.max(...group.map(tab => cardColumnSpan(tab, layouts, groupCount))),
  );
  const groupWidths = normalizeGroupWidths(requestedWidths, preferredGroupIndex);
  const placements = new Map<Tab, CardPlacement>();
  let rightmostColumn = GRID_COLUMN_COUNT;

  groups.forEach((group, groupIndex) => {
    const groupWidth = groupWidths[groupIndex];
    const groupStart = rightmostColumn - groupWidth + 1;
    rightmostColumn = groupStart - 1;
    const isRightmostGroup = groupIndex === 0;

    group.forEach((tab, cardIndex) => {
      placements.set(tab, {
        gridColumn: `${groupStart} / span ${groupWidth}`,
        gridRow: group.length === 1 ? "1 / span 2" : String(cardIndex + 1),
        // Left-edge splitter on every card in the rightmost column so the
        // grab target covers the full stack. A single column always fills
        // the board; the shell Resizer owns that width.
        showResizeHandle: groupCount > 1 && isRightmostGroup,
        columnSpan: groupWidth,
        groupIndex,
      });
    });
  });
  return placements;
}

function readTabList(key: string): string[] | null {
  try {
    const value = JSON.parse(localStorage.getItem(key) || "null");
    return Array.isArray(value) ? value.filter((tab): tab is string => typeof tab === "string") : null;
  } catch {
    return null;
  }
}

function readLegacyOpenCards(): Tab[] {
  try {
    const value = JSON.parse(localStorage.getItem("pmharness.splitState") || "null");
    if (!value || typeof value !== "object") return [];
    const legacyTabs = [value.primaryTab, ...(value.isSplit ? [value.secondaryTab] : [])];
    return legacyTabs
      .map(tab => tab === "mcp" ? "state" : tab)
      .filter((tab): tab is Tab => typeof tab === "string" && CANONICAL_ORDER.includes(tab as Tab) && tab !== PINNED_LAST);
  } catch {
    return [];
  }
}

export default function RightPane({ visible, artifacts, onOpenWizard, initialTab, onEmpty }: {
  visible: boolean;
  artifacts: { type: string; headline: string; confidence?: number }[];
  onOpenWizard: () => void;
  initialTab?: string | null;
  onEmpty?: () => void;
}) {
  const tabVisibilityRef = useRef<RightPaneTabVisibility>(loadRightPaneTabVisibility());
  const [cardLayouts, setCardLayouts] = useState<CardLayouts>(() => readCardLayouts());
  const cardLayoutsRef = useRef(cardLayouts);
  cardLayoutsRef.current = cardLayouts;
  const boardRef = useRef<HTMLDivElement | null>(null);
  const preferredResizeGroupRef = useRef(-1);
  const [draggedTab, setDraggedTab] = useState<Tab | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const handledInitialTab = useRef<string | null>(null);
  const [tabOrder, setTabOrder] = useState<Tab[]>(() => {
    const validTabs = CANONICAL_ORDER.filter(tab => tab !== PINNED_LAST);
    const savedOrder = readTabList("pmharness.tabOrder");
    const legacySplit = readLegacyOpenCards();
    const order = [...(savedOrder || []), ...legacySplit, ...validTabs]
      .map(tab => tab === "mcp" ? "state" : tab)
      .filter((tab, index, list): tab is Tab =>
        validTabs.includes(tab as Tab) && list.indexOf(tab) === index);
    const savedOpen = readTabList("pmharness.board.openCards");
    const openCards = (savedOpen || (legacySplit.length > 0 ? legacySplit : []))
      .map(tab => tab === "mcp" ? "state" : tab)
      .filter((tab, index, list): tab is Tab =>
        validTabs.includes(tab as Tab) && list.indexOf(tab) === index);
    if (!savedOpen) localStorage.setItem("pmharness.board.openCards", JSON.stringify(openCards));
    return order;
  });
  const [openCards, setOpenCards] = useState<Tab[]>(() => {
    const savedOpen = readTabList("pmharness.board.openCards");
    const legacyCards = readLegacyOpenCards();
    const initialCards = savedOpen ?? (legacyCards.length > 0 ? legacyCards : []);
    return initialCards
      .map(tab => tab === "mcp" ? "state" : tab)
      .filter((tab, index, list): tab is Tab =>
        CANONICAL_ORDER.includes(tab as Tab) && tab !== PINNED_LAST && list.indexOf(tab) === index);
  });

  const persistBoard = useCallback((nextOrder: Tab[], nextOpenCards: Tab[]) => {
    preferredResizeGroupRef.current = -1;
    setTabOrder(nextOrder);
    setOpenCards(nextOpenCards);
    localStorage.setItem("pmharness.tabOrder", JSON.stringify(nextOrder));
    localStorage.setItem("pmharness.board.openCards", JSON.stringify(nextOpenCards));
    if (nextOpenCards.length === 0) onEmpty?.();
  }, [onEmpty]);

  const addCard = useCallback((tabName: Tab) => {
    if (tabName === PINNED_LAST) {
      setSettingsOpen(true);
      return;
    }
    if (!tabVisibilityRef.current[tabName]) {
      const nextVisibility = { ...tabVisibilityRef.current, [tabName]: true };
      tabVisibilityRef.current = nextVisibility;
      saveRightPaneTabVisibility(nextVisibility);
    }
    persistBoard(tabOrder, openCards.includes(tabName) ? openCards : [...openCards, tabName]);
    requestAnimationFrame(() => document.getElementById(`right-pane-card-${tabName}`)?.focus());
  }, [openCards, persistBoard, tabOrder]);

  const removeCard = (tabName: Tab) => {
    const nextOpenCards = openCards.filter(card => card !== tabName);
    persistBoard(tabOrder, nextOpenCards);
  };

  const persistCardLayouts = useCallback((nextLayouts: CardLayouts) => {
    cardLayoutsRef.current = nextLayouts;
    setCardLayouts(nextLayouts);
    localStorage.setItem(CARD_LAYOUT_STORAGE_KEY, JSON.stringify(nextLayouts));
  }, []);

  const setGroupColumnSpan = useCallback((groupIndex: number, nextSpan: number) => {
    const groupCount = Math.ceil(openCards.length / 2);
    if (groupCount <= 1) return;
    preferredResizeGroupRef.current = groupIndex;
    const groups = Array.from({ length: groupCount }, (_, index) =>
      openCards.slice(index * 2, index * 2 + 2),
    );
    const minWidth = groupCount <= 4 ? 2 : 1;
    const requested = groups.map((group, index) => {
      if (index === groupIndex) return clampCardColumnSpan(nextSpan, minWidth);
      return Math.max(
        ...group.map(tab => cardColumnSpan(tab, cardLayoutsRef.current, groupCount)),
      );
    });
    if (groupCount === 2) {
      const primary = Math.max(
        minWidth,
        Math.min(GRID_COLUMN_COUNT - minWidth, requested[groupIndex]),
      );
      requested[groupIndex] = primary;
      requested[1 - groupIndex] = GRID_COLUMN_COUNT - primary;
    }
    const normalized = normalizeGroupWidths(requested, groupIndex);
    const nextLayouts: CardLayouts = { ...cardLayoutsRef.current };
    groups.forEach((group, index) => {
      for (const tab of group) {
        nextLayouts[tab] = { columnSpan: normalized[index], customized: true };
      }
    });
    persistCardLayouts(nextLayouts);
  }, [openCards, persistCardLayouts]);

  const resizeGroupFromPointer = useCallback((
    event: React.PointerEvent<HTMLSpanElement>,
    placement: CardPlacement,
  ) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    const boardWidth = boardRef.current?.getBoundingClientRect().width || 0;
    if (!boardWidth) return;
    const handle = event.currentTarget;
    handle.setPointerCapture(event.pointerId);
    const startX = event.clientX;
    const startSpan = placement.columnSpan;
    const pixelsPerColumn = boardWidth / GRID_COLUMN_COUNT;
    beginColumnResize();

    const onMove = (moveEvent: PointerEvent) => {
      if (!handle.hasPointerCapture(moveEvent.pointerId)) return;
      const deltaColumns = Math.round((startX - moveEvent.clientX) / pixelsPerColumn);
      setGroupColumnSpan(placement.groupIndex, startSpan + deltaColumns);
    };
    const onUp = (upEvent: PointerEvent) => {
      if (handle.hasPointerCapture(upEvent.pointerId)) {
        handle.releasePointerCapture(upEvent.pointerId);
      }
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("pointercancel", onUp);
      endColumnResize();
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onUp);
  }, [setGroupColumnSpan]);

  const handleDragStart = (event: React.DragEvent, tabId: Tab) => {
    setDraggedTab(tabId);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", tabId);
  };

  const handleDrop = (event: React.DragEvent, targetTab: Tab) => {
    event.preventDefault();
    const sourceTab = draggedTab || event.dataTransfer.getData("text/plain") as Tab;
    if (!sourceTab || sourceTab === targetTab) return;
    const fromIndex = openCards.indexOf(sourceTab);
    const toIndex = openCards.indexOf(targetTab);
    if (fromIndex < 0 || toIndex < 0) return;
    const next = [...openCards];
    next.splice(fromIndex, 1);
    next.splice(toIndex, 0, sourceTab);
    persistBoard(tabOrder, next);
    setDraggedTab(null);
  };

  useEffect(() => {
    if (!initialTab || initialTab === PINNED_LAST) {
      if (initialTab === PINNED_LAST) setSettingsOpen(true);
      return;
    }
    if (handledInitialTab.current === initialTab) return;
    handledInitialTab.current = initialTab;
    addCard(initialTab as Tab);
  }, [initialTab, addCard]);

  useEffect(() => {
    if (visible && openCards.length === 0 && !initialTab) onEmpty?.();
  }, [initialTab, onEmpty, openCards.length, visible]);

  const [reviews, setReviews] = useState<PendingReview[]>([]);
  /** Sticky when getReviews fails — DiffReviewPane must not claim an empty queue. */
  const [reviewsLoadError, setReviewsLoadError] = useState<string | null>(null);
  // Live swarm activity for the Swarm tab light -- so a running job is visible
  // even when the tracker tab itself is not open.
  const [swarmRunning, setSwarmRunning] = useState(0);
  const [swarmRepo, setSwarmRepo] = useState<string | undefined>(
    () => lastSelectedProjectRoot() || undefined,
  );

  useEffect(() => {
    const onProject = (e: Event) => {
      const path = (e as CustomEvent<string>).detail;
      if (typeof path === "string") setSwarmRepo(path || undefined);
    };
    window.addEventListener("harness-project-selected", onProject);
    return () => window.removeEventListener("harness-project-selected", onProject);
  }, []);

  const fetchReviews = () => {
    return api.getReviews()
      .then((data) => {
        if (Array.isArray(data)) {
          setReviews(data);
          setReviewsLoadError(null);
        }
      })
      .catch((err) => {
        console.error("Failed to load reviews:", err);
        // Keep last-known reviews; surface sticky load failure so the pane
        // never lies with "No pending edits…" after a failed fetch.
        setReviewsLoadError("Couldn't load pending reviews.");
      });
  };

  const fetchSwarmActivity = () => {
    return api.swarmLive(swarmRepo)
      .then((data) => {
        // Warm SwarmPane's SWR key so first open of the tracker is not a cold
        // "Loading swarm jobs..." flash — the tab light already polls this payload.
        writeSWRCache(`swarm:${swarmRepo || "__default__"}`, data);
        const jobs = Array.isArray(data?.jobs) ? data.jobs : [];
        const n = jobs.filter((j) => {
          const s = (j.status || "").toLowerCase();
          return s.includes("run") || s.includes("progress") || s.includes("active");
        }).length;
        setSwarmRunning(n);
      })
      .catch(() => {
        /* keep last known; tab light is best-effort */
      });
  };

  usePolling(fetchReviews, 4000);
  usePolling(fetchSwarmActivity, 4000);

  // Immediate refresh when a swarm parks a DiffReview (pending_review stream/poll).
  useEffect(() => {
    const onRefresh = () => {
      void fetchReviews();
    };
    window.addEventListener("harness-reviews-refresh", onRefresh);
    return () => window.removeEventListener("harness-reviews-refresh", onRefresh);
  }, []);

  // Hotkey listener
  useEffect(() => {
    const onFocusTab = (e: CustomEvent<string>) => {
      if (e?.detail) {
        // MCP merged into State; expand that section when something asks for MCP.
        if (e.detail === "mcp") {
          addCard("state");
          window.dispatchEvent(new Event("harness-expand-mcp"));
          return;
        }
        const targetTab = e.detail as Tab;
        const validTabs: Tab[] = ["state", "files", "git", "worktrees", "terminal", "browser", "settings", "swarm", "checkpoints", "review"];
        if (validTabs.includes(targetTab)) {
          if (targetTab === "settings") {
            setSettingsOpen(true);
            return;
          }
          addCard(targetTab);
        }
      }
    };
    window.addEventListener("harness-focus-tab", onFocusTab as EventListener);
    return () => window.removeEventListener("harness-focus-tab", onFocusTab as EventListener);
  }, [addCard]);

  const renderCardBody = (tabName: Tab) => (
    <ErrorBoundary label={TAB_CONFIG[tabName]?.label || tabName} inline>
      {tabName === "state" ? <StatePane artifacts={artifacts} embedded /> : renderTabInner(tabName)}
    </ErrorBoundary>
  );

  const renderTabInner = (tabName: Tab) => {
    switch (tabName) {
      case "browser":
        return <BrowserPane />;
      case "files":
        return <FileTree />;
      case "git":
        return <SourceControl />;
      case "terminal":
        return <TerminalPane />;
      case "worktrees":
        return <WorktreesPane />;
      case "settings":
        return null;
      case "checkpoints":
        return <CheckpointsPane />;
      case "swarm":
        return <SwarmPane />;
      case "review":
        return (
          <DiffReviewPane
            reviews={reviews}
            onRefresh={fetchReviews}
            loadError={reviewsLoadError}
          />
        );
      default:
        return null;
    }
  };
  const cardPlacements = buildCardPlacements(
    openCards,
    cardLayouts,
    preferredResizeGroupRef.current,
  );

  return (
    <>
      {visible && openCards.length > 0 && (
        <div ref={boardRef} className="right-pane-board h-full w-full overflow-y-auto">
            <div
              className="right-pane-board-grid"
              style={{
                gridTemplateColumns: `repeat(${GRID_COLUMN_COUNT}, minmax(0, 1fr))`,
                gridTemplateRows: "repeat(2, minmax(0, 1fr))",
              }}
            >
              {openCards.map((tabName) => {
                const config = TAB_CONFIG[tabName];
                const placement = cardPlacements.get(tabName);
                if (!placement) return null;
                return (
            <section
              id={`right-pane-card-${tabName}`}
              key={tabName}
              tabIndex={-1}
              role="region"
              aria-label={`${config.label} panel`}
              className={`right-pane-card pointer-events-auto flex flex-col ${draggedTab === tabName ? "opacity-40" : ""}`}
              style={{
                gridColumn: placement.gridColumn,
                gridRow: placement.gridRow,
              }}
              onDragOver={event => event.preventDefault()}
              onDrop={event => handleDrop(event, tabName)}
            >
              {placement.showResizeHandle && (
              <span
                role="separator"
                aria-orientation="vertical"
                aria-label="Resize tool columns"
                tabIndex={0}
                className="right-pane-card-resize-handle left-0"
                onPointerDown={event => resizeGroupFromPointer(event, placement)}
                onKeyDown={event => {
                  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
                  event.preventDefault();
                  setGroupColumnSpan(
                    placement.groupIndex,
                    placement.columnSpan + (event.key === "ArrowLeft" ? 1 : -1),
                  );
                }}
              />
              )}
              <header
                className="right-pane-card-header"
              >
                <div className="flex items-center gap-0.5 shrink-0 ml-auto">
                  {tabName === "review" && reviews.length > 0 && <span className="right-pane-badge">{reviews.length}</span>}
                  {tabName === "swarm" && swarmRunning > 0 && <span className="right-pane-live" title={`${swarmRunning} swarm jobs running`} />}
                  <button
                    type="button"
                    draggable
                    aria-label={`Drag ${config.label} panel`}
                    aria-keyshortcuts="Alt+ArrowUp Alt+ArrowDown"
                    title="Drag to reorder (Alt+Arrow Up/Down also works)"
                    onDragStart={event => handleDragStart(event, tabName)}
                    onDragEnd={() => setDraggedTab(null)}
                    onMouseDown={event => event.stopPropagation()}
                    onKeyDown={event => {
                      if (!event.altKey || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
                      event.preventDefault();
                      const currentIndex = openCards.indexOf(tabName);
                      const targetIndex = currentIndex + (event.key === "ArrowUp" ? -1 : 1);
                      if (currentIndex < 0 || targetIndex < 0 || targetIndex >= openCards.length) return;
                      const next = [...openCards];
                      next.splice(currentIndex, 1);
                      next.splice(targetIndex, 0, tabName);
                      persistBoard(tabOrder, next);
                    }}
                    className="right-pane-drag-handle"
                  >
                    <GripVertical size={13} />
                  </button>
                  <button type="button" aria-label={`Close ${config.label} panel`} title={`Close ${config.label}`} onClick={() => removeCard(tabName)} onMouseDown={event => event.stopPropagation()} className="right-pane-icon-btn"><X size={12} /></button>
                </div>
              </header>
              <div className="right-pane-card-body">{renderCardBody(tabName)}</div>
            </section>
                );
              })}
            </div>
        </div>
      )}
      {/* Keep the expensive interactive panes alive when users close their cards. */}
      <div className="hidden" aria-hidden>
        {!openCards.includes("state") && (
          <div data-testid="state-pane-slot">
            <StatePane artifacts={artifacts} embedded />
          </div>
        )}
        {!openCards.includes("terminal") && (
          <div data-testid="terminal-pane-slot">
            <TerminalPane />
          </div>
        )}
        {!openCards.includes("swarm") && (
          <div data-testid="swarm-pane-slot">
            <SwarmPane />
          </div>
        )}
      </div>
      {visible && settingsOpen && (
        <SettingsShell
          onOpenWizard={onOpenWizard}
          onClose={() => {
            setSettingsOpen(false);
            if (openCards.length === 0) onEmpty?.();
          }}
        />
      )}
    </>
  );
}
