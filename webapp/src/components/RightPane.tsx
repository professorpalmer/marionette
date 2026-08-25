import { useCallback, useState, useEffect, useLayoutEffect, useRef } from "react";
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
import EconomicsPane from "./EconomicsPane";
import ErrorBoundary from "./ErrorBoundary";
import { api, type PendingReview } from "../lib/api";
import { lastSelectedProjectRoot } from "../lib/panelTransition";
import { usePolling } from "../lib/usePolling";
import { writeSWRCache } from "../lib/useStaleWhileRevalidate";
import { countRunningTrackerJobs } from "../lib/jobClassification";
import {
  isSettingsOverlayOpen,
  setSettingsOverlayOpen,
} from "../lib/settingsOverlay";
import {
  loadRightPaneTabVisibility,
  saveRightPaneTabVisibility,
  type RightPaneTabVisibility,
} from "../lib/rightPaneTabVisibility";
import { beginColumnResize, beginRowResize, endColumnResize, endRowResize } from "../lib/columnResize";
import {
  BOARD_COLUMNS_STORAGE_KEY,
  MIN_MULTI_COLUMN_BOARD_PX,
  NEW_COLUMN_DROP_LABEL,
  canOpenLeftColumn,
  columnIndexOf,
  defaultColumns,
  extractCardToLeftColumn,
  flattenColumns,
  moveCardIntoColumn,
  reconcileColumns,
} from "../lib/boardColumns";
import {
  CARD_LAYOUT_STORAGE_KEY,
  GRID_COLUMN_COUNT,
  MIN_CARD_COLUMN_SPAN,
  applyPairwiseColumnResize,
  clampCardColumnSpan,
  columnSpanFromPointerDelta,
  columnTrackTemplate,
  groupGridColumn,
  absorbShellResize,
  normalizeGroupWidths,
  showColumnResizeHandle,
} from "../lib/boardColumnWidths";
import {
  STACK_FRACTIONS_STORAGE_KEY,
  STACK_ROW_RESIZE_LABEL,
  STACK_SPLIT_STORAGE_KEY,
  clampStackSplit,
  equalFractions,
  fractionsFromBoundaryDrag,
  fractionsFromKey,
  normalizeFractions,
  stackPairKey,
  stackRowTemplateN,
} from "../lib/stackSplit";

type Tab = "state" | "files" | "git" | "worktrees" | "terminal" | "browser" | "settings" | "checkpoints" | "review" | "swarm" | "economics";

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
  economics: { label: "Economics" },
};

const TAB_GROUPS: { group: string; tabs: Tab[] }[] = [
  { group: "workspace", tabs: ["state", "swarm", "economics", "files", "git", "worktrees", "terminal"] },
  { group: "changes", tabs: ["review", "checkpoints"] },
  { group: "tools", tabs: ["browser"] },
];
const PINNED_LAST: Tab = "settings";
const CANONICAL_ORDER: Tab[] = [
  ...TAB_GROUPS.flatMap(g => g.tabs),
  PINNED_LAST,
];

type CardLayout = {
  columnSpan: number;
  customized?: boolean;
};

type CardLayouts = Partial<Record<Tab, CardLayout>>;

type CardPlacement = {
  gridColumn: string;
  gridRow: string;
  showResizeHandle: boolean;
  showRowResizeHandle: boolean;
  columnSpan: number;
  groupIndex: number;
};

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

function buildCardPlacements(
  columns: Tab[][],
  layouts: CardLayouts,
  preferredGroupIndex: number,
): Map<Tab, CardPlacement> {
  const groups = columns.filter((group) => group.length > 0);
  const groupCount = groups.length;
  const requestedWidths = groups.map(group =>
    Math.max(...group.map(tab => cardColumnSpan(tab, layouts, groupCount))),
  );
  const groupWidths = normalizeGroupWidths(requestedWidths, preferredGroupIndex);
  const placements = new Map<Tab, CardPlacement>();

  groups.forEach((group, groupIndex) => {
    const groupWidth = groupWidths[groupIndex];

    group.forEach((tab, cardIndex) => {
      placements.set(tab, {
        gridColumn: groupGridColumn(groupIndex, groupCount),
        gridRow: String(cardIndex + 1),
        // Left-edge splitter on every column except the leftmost. A single
        // column always fills the board; the shell Resizer owns that width.
        showResizeHandle: showColumnResizeHandle(groupIndex, groupCount),
        showRowResizeHandle: group.length > 1 && cardIndex < group.length - 1,
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

function readStackFractions(): Record<string, number[]> {
  const fractions: Record<string, number[]> = {};
  try {
    const raw = JSON.parse(localStorage.getItem(STACK_FRACTIONS_STORAGE_KEY) || "null");
    if (raw && typeof raw === "object") {
      for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
        if (typeof key !== "string" || !Array.isArray(value)) continue;
        const nums = value.map(Number).filter((item) => Number.isFinite(item));
        if (nums.length >= 2) fractions[key] = normalizeFractions(nums, nums.length);
      }
    }
  } catch {
    /* fall through to v1 */
  }
  try {
    const raw = JSON.parse(localStorage.getItem(STACK_SPLIT_STORAGE_KEY) || "null");
    if (raw && typeof raw === "object") {
      for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
        if (fractions[key] || typeof key !== "string" || !key.includes("|")) continue;
        const split = clampStackSplit(Number(value));
        fractions[key] = [split, 1 - split];
      }
    }
  } catch {
    /* ignore corrupt v1 */
  }
  return fractions;
}

function readBoardColumns(openCards: Tab[]): Tab[][] {
  try {
    const raw = JSON.parse(localStorage.getItem(BOARD_COLUMNS_STORAGE_KEY) || "null");
    if (!Array.isArray(raw)) return defaultColumns(openCards);
    const parsed = raw
      .filter((col): col is unknown[] => Array.isArray(col))
      .map((col) => col.filter((tab): tab is Tab => (
        typeof tab === "string"
        && CANONICAL_ORDER.includes(tab as Tab)
        && tab !== PINNED_LAST
      )));
    return reconcileColumns(openCards, parsed);
  } catch {
    return defaultColumns(openCards);
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

function readInitialOpenCards(): Tab[] {
  const savedOpen = readTabList("pmharness.board.openCards");
  const legacyCards = readLegacyOpenCards();
  const initialCards = savedOpen ?? (legacyCards.length > 0 ? legacyCards : []);
  return initialCards
    .map(tab => tab === "mcp" ? "state" : tab)
    .filter((tab, index, list): tab is Tab =>
      CANONICAL_ORDER.includes(tab as Tab) && tab !== PINNED_LAST && list.indexOf(tab) === index);
}

export default function RightPane({ visible, artifacts, onOpenWizard, initialTab, onEmpty, onRequestMinWidth }: {
  visible: boolean;
  artifacts: { type: string; headline: string; confidence?: number }[];
  onOpenWizard: () => void;
  initialTab?: string | null;
  onEmpty?: () => void;
  onRequestMinWidth?: (minPx: number) => void;
}) {
  const tabVisibilityRef = useRef<RightPaneTabVisibility>(loadRightPaneTabVisibility());
  const [cardLayouts, setCardLayouts] = useState<CardLayouts>(() => readCardLayouts());
  const cardLayoutsRef = useRef(cardLayouts);
  cardLayoutsRef.current = cardLayouts;
  const [stackFractions, setStackFractions] = useState<Record<string, number[]>>(() => readStackFractions());
  const stackFractionsRef = useRef(stackFractions);
  stackFractionsRef.current = stackFractions;
  const boardRef = useRef<HTMLDivElement | null>(null);
  const [boardWidth, setBoardWidth] = useState(0);
  const prevBoardWidthRef = useRef(0);
  const preferredResizeGroupRef = useRef(-1);
  const [draggedTab, setDraggedTab] = useState<Tab | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(isSettingsOverlayOpen);
  const handledInitialTab = useRef<string | null>(null);
  const [tabOrder, setTabOrder] = useState<Tab[]>(() => {
    const validTabs = CANONICAL_ORDER.filter(tab => tab !== PINNED_LAST);
    const savedOrder = readTabList("pmharness.tabOrder");
    const legacySplit = readLegacyOpenCards();
    const order = [...(savedOrder || []), ...legacySplit, ...validTabs]
      .map(tab => tab === "mcp" ? "state" : tab)
      .filter((tab, index, list): tab is Tab =>
        validTabs.includes(tab as Tab) && list.indexOf(tab) === index);
    const openCards = readInitialOpenCards();
    if (!readTabList("pmharness.board.openCards")) {
      localStorage.setItem("pmharness.board.openCards", JSON.stringify(openCards));
    }
    return order;
  });
  const [openCards, setOpenCards] = useState<Tab[]>(() => readInitialOpenCards());
  const [columns, setColumns] = useState<Tab[][]>(() => readBoardColumns(readInitialOpenCards()));
  const columnsRef = useRef(columns);
  columnsRef.current = columns;

  const persistBoard = useCallback((
    nextOrder: Tab[],
    nextOpenCards: Tab[],
    nextColumns?: Tab[][],
  ) => {
    preferredResizeGroupRef.current = -1;
    const cols = reconcileColumns(nextOpenCards, nextColumns ?? columnsRef.current);
    const flat = flattenColumns(cols);
    columnsRef.current = cols;
    setTabOrder(nextOrder);
    setOpenCards(flat);
    setColumns(cols);
    localStorage.setItem("pmharness.tabOrder", JSON.stringify(nextOrder));
    localStorage.setItem("pmharness.board.openCards", JSON.stringify(flat));
    localStorage.setItem(BOARD_COLUMNS_STORAGE_KEY, JSON.stringify(cols));
    if (flat.length === 0 && !isSettingsOverlayOpen()) onEmpty?.();
  }, [onEmpty]);

  const openSettings = useCallback(() => {
    setSettingsOverlayOpen(true);
    setSettingsOpen(true);
  }, []);

  const closeSettings = useCallback(() => {
    setSettingsOverlayOpen(false);
    setSettingsOpen(false);
    if (openCards.length === 0) onEmpty?.();
  }, [onEmpty, openCards.length]);

  const addCard = useCallback((tabName: Tab) => {
    if (tabName === PINNED_LAST) {
      openSettings();
      return;
    }
    if (!tabVisibilityRef.current[tabName]) {
      const nextVisibility = { ...tabVisibilityRef.current, [tabName]: true };
      tabVisibilityRef.current = nextVisibility;
      saveRightPaneTabVisibility(nextVisibility);
    }
    persistBoard(tabOrder, openCards.includes(tabName) ? openCards : [...openCards, tabName]);
    requestAnimationFrame(() => document.getElementById(`right-pane-card-${tabName}`)?.focus());
  }, [openCards, openSettings, persistBoard, tabOrder]);

  const removeCard = (tabName: Tab) => {
    const nextOpenCards = openCards.filter(card => card !== tabName);
    persistBoard(tabOrder, nextOpenCards);
  };

  useEffect(() => {
    const onClose = (e: Event) => {
      const tab = (e as CustomEvent<{ tab?: Tab }>).detail?.tab;
      if (!tab) return;
      if (tab === PINNED_LAST) {
        closeSettings();
        return;
      }
      if (openCards.includes(tab)) removeCard(tab);
    };
    window.addEventListener("harness-close-right-card", onClose as EventListener);
    return () => window.removeEventListener("harness-close-right-card", onClose as EventListener);
  }, [closeSettings, openCards, tabOrder]);

  const persistCardLayouts = useCallback((nextLayouts: CardLayouts) => {
    cardLayoutsRef.current = nextLayouts;
    setCardLayouts(nextLayouts);
    localStorage.setItem(CARD_LAYOUT_STORAGE_KEY, JSON.stringify(nextLayouts));
  }, []);

  useLayoutEffect(() => {
    const el = boardRef.current;
    if (!el) {
      prevBoardWidthRef.current = 0;
      setBoardWidth(0);
      return;
    }
    const applyWidth = () => {
      const nextWidth = el.getBoundingClientRect().width;
      const prevWidth = prevBoardWidthRef.current;
      prevBoardWidthRef.current = nextWidth;
      setBoardWidth(nextWidth);
      if (!(prevWidth > 0 && nextWidth > 0) || Math.abs(prevWidth - nextWidth) < 0.5) return;
      const groups = columnsRef.current.filter(group => group.length > 0);
      if (groups.length <= 1) return;
      const current = groups.map(group => Math.max(
        ...group.map(tab => cardColumnSpan(tab, cardLayoutsRef.current, groups.length)),
      ));
      const absorbed = absorbShellResize(current, prevWidth, nextWidth);
      const nextLayouts: CardLayouts = { ...cardLayoutsRef.current };
      groups.forEach((group, index) => {
        for (const tab of group) {
          nextLayouts[tab] = { columnSpan: absorbed[index], customized: true };
        }
      });
      persistCardLayouts(nextLayouts);
    };
    applyWidth();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(applyWidth);
    observer.observe(el);
    return () => observer.disconnect();
  }, [visible, openCards.length, persistCardLayouts]);

  const persistStackFractions = useCallback((nextFractions: Record<string, number[]>) => {
    stackFractionsRef.current = nextFractions;
    setStackFractions(nextFractions);
    localStorage.setItem(STACK_FRACTIONS_STORAGE_KEY, JSON.stringify(nextFractions));
  }, []);

  const setStackFractionsForKey = useCallback((pairKey: string, nextFractions: number[]) => {
    persistStackFractions({
      ...stackFractionsRef.current,
      [pairKey]: normalizeFractions(nextFractions, nextFractions.length),
    });
  }, [persistStackFractions]);

  const setGroupColumnSpan = useCallback((groupIndex: number, nextSpan: number) => {
    const groups = columnsRef.current.filter((group) => group.length > 0);
    const groupCount = groups.length;
    if (!showColumnResizeHandle(groupIndex, groupCount)) return;
    preferredResizeGroupRef.current = groupIndex;
    const current = groups.map((group) => Math.max(
      ...group.map(tab => cardColumnSpan(tab, cardLayoutsRef.current, groupCount)),
    ));
    const next = applyPairwiseColumnResize(current, groupIndex, nextSpan);
    const nextLayouts: CardLayouts = { ...cardLayoutsRef.current };
    groups.forEach((group, index) => {
      for (const tab of group) {
        nextLayouts[tab] = { columnSpan: next[index], customized: true };
      }
    });
    persistCardLayouts(nextLayouts);
  }, [persistCardLayouts]);

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
    beginColumnResize();

    const onMove = (moveEvent: PointerEvent) => {
      if (!handle.hasPointerCapture(moveEvent.pointerId)) return;
      setGroupColumnSpan(
        placement.groupIndex,
        columnSpanFromPointerDelta({
          startSpan,
          startClientX: startX,
          clientX: moveEvent.clientX,
          boardWidth,
        }),
      );
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

  const resizeStackFromPointer = useCallback((
    event: React.PointerEvent<HTMLSpanElement>,
    pairKey: string,
    boundaryIndex: number,
    stackLength: number,
  ) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    const stack = event.currentTarget.closest(".right-pane-card-stack");
    const stackHeight = stack?.getBoundingClientRect().height || 0;
    if (!stackHeight) return;
    const handle = event.currentTarget;
    handle.setPointerCapture(event.pointerId);
    const startY = event.clientY;
    const startFractions = normalizeFractions(
      stackFractionsRef.current[pairKey] ?? equalFractions(stackLength),
      stackLength,
    );
    beginRowResize();

    const onMove = (moveEvent: PointerEvent) => {
      if (!handle.hasPointerCapture(moveEvent.pointerId)) return;
      setStackFractionsForKey(pairKey, fractionsFromBoundaryDrag({
        fractions: startFractions,
        boundaryIndex,
        startClientY: startY,
        clientY: moveEvent.clientY,
        stackHeight,
      }));
    };
    const onUp = (upEvent: PointerEvent) => {
      if (handle.hasPointerCapture(upEvent.pointerId)) {
        handle.releasePointerCapture(upEvent.pointerId);
      }
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("pointercancel", onUp);
      endRowResize();
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onUp);
  }, [setStackFractionsForKey]);

  const handleDragStart = (event: React.DragEvent, tabId: Tab) => {
    setDraggedTab(tabId);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", tabId);
  };

  const handleDrop = (event: React.DragEvent, targetTab: Tab) => {
    event.preventDefault();
    const sourceTab = draggedTab || event.dataTransfer.getData("text/plain") as Tab;
    if (!sourceTab || sourceTab === targetTab) return;
    const destCol = columnIndexOf(columnsRef.current, targetTab);
    if (destCol < 0) return;
    const destIndex = columnsRef.current[destCol].indexOf(targetTab);
    const nextCols = moveCardIntoColumn(columnsRef.current, sourceTab, destCol, destIndex);
    persistBoard(tabOrder, flattenColumns(nextCols), nextCols);
    setDraggedTab(null);
  };

  const handleDropNewColumn = (event: React.DragEvent) => {
    event.preventDefault();
    event.stopPropagation();
    const sourceTab = draggedTab || event.dataTransfer.getData("text/plain") as Tab;
    if (!sourceTab || !canOpenLeftColumn(columnsRef.current, sourceTab)) {
      setDraggedTab(null);
      return;
    }
    const nextCols = extractCardToLeftColumn(columnsRef.current, sourceTab);
    persistBoard(tabOrder, flattenColumns(nextCols), nextCols);
    onRequestMinWidth?.(MIN_MULTI_COLUMN_BOARD_PX);
    setDraggedTab(null);
  };

  useEffect(() => {
    if (!initialTab || initialTab === PINNED_LAST) {
      if (initialTab === PINNED_LAST) openSettings();
      return;
    }
    if (handledInitialTab.current === initialTab) return;
    handledInitialTab.current = initialTab;
    addCard(initialTab as Tab);
  }, [initialTab, addCard, openSettings]);

  useEffect(() => {
    if (visible && openCards.length === 0 && !initialTab && !settingsOpen) onEmpty?.();
  }, [initialTab, onEmpty, openCards.length, settingsOpen, visible]);

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
        setSwarmRunning(countRunningTrackerJobs(jobs));
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
        const validTabs: Tab[] = ["state", "files", "git", "worktrees", "terminal", "browser", "settings", "swarm", "economics", "checkpoints", "review"];
        if (validTabs.includes(targetTab)) {
          if (targetTab === "settings") {
            openSettings();
            return;
          }
          addCard(targetTab);
        }
      }
    };
    window.addEventListener("harness-focus-tab", onFocusTab as EventListener);
    return () => window.removeEventListener("harness-focus-tab", onFocusTab as EventListener);
  }, [addCard, openSettings]);

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
      case "economics":
        return <EconomicsPane />;
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
    columns,
    cardLayouts,
    preferredResizeGroupRef.current,
  );
  const cardStacks = columns.filter((group) => group.length > 0);
  const groupWidths = cardStacks.map((stack) => (
    cardPlacements.get(stack[0])?.columnSpan ?? MIN_CARD_COLUMN_SPAN
  ));
  const showNewColumnDrop = Boolean(draggedTab && canOpenLeftColumn(columns, draggedTab));

  return (
    <>
      {visible && openCards.length > 0 && (
        <div ref={boardRef} className="right-pane-board h-full w-full overflow-y-auto">
            {showNewColumnDrop && (
              <div
                role="region"
                aria-label={NEW_COLUMN_DROP_LABEL}
                className="right-pane-new-column-drop"
                onDragOver={event => event.preventDefault()}
                onDrop={handleDropNewColumn}
              >
                {NEW_COLUMN_DROP_LABEL}
              </div>
            )}
            <div
              className="right-pane-board-grid"
              style={{
                gridTemplateColumns: columnTrackTemplate(groupWidths, boardWidth),
                gridTemplateRows: "minmax(0, 1fr)",
              }}
            >
              {cardStacks.map((stackTabs) => {
                const stackPlacement = cardPlacements.get(stackTabs[0]);
                if (!stackPlacement) return null;
                const pairKey = stackPairKey(stackTabs);
                const stackRows = normalizeFractions(
                  stackFractions[pairKey] ?? equalFractions(stackTabs.length),
                  stackTabs.length,
                );
                return (
            <div
              key={pairKey}
              className={`right-pane-card-stack${stackPlacement.groupIndex > 0 ? " right-pane-card-stack-join-end" : ""}`}
              style={{
                gridColumn: stackPlacement.gridColumn,
                gridRow: "1",
                gridTemplateRows: stackTabs.length === 1
                  ? "minmax(0, 1fr)"
                  : stackRowTemplateN(stackRows),
              }}
            >
              {stackTabs.map((tabName) => {
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
              className={`right-pane-card pointer-events-auto flex flex-col${Number(placement.gridRow) > 1 ? " right-pane-card-join-top" : ""}${draggedTab === tabName ? " opacity-40" : ""}`}
              style={{
                gridColumn: "1",
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
                data-testid={`column-resize-${placement.groupIndex}`}
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
              {placement.showRowResizeHandle && (
              <span
                role="separator"
                aria-orientation="horizontal"
                aria-label={STACK_ROW_RESIZE_LABEL}
                tabIndex={0}
                className="right-pane-card-row-resize-handle"
                onPointerDown={event => resizeStackFromPointer(
                  event,
                  pairKey,
                  Number(placement.gridRow) - 1,
                  stackTabs.length,
                )}
                onKeyDown={event => {
                  if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
                  event.preventDefault();
                  setStackFractionsForKey(
                    pairKey,
                    fractionsFromKey(stackRows, Number(placement.gridRow) - 1, event.key),
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
                      const colIndex = columnIndexOf(columnsRef.current, tabName);
                      if (colIndex < 0) return;
                      const col = columnsRef.current[colIndex];
                      const currentIndex = col.indexOf(tabName);
                      const targetIndex = currentIndex + (event.key === "ArrowUp" ? -1 : 1);
                      if (currentIndex < 0 || targetIndex < 0 || targetIndex >= col.length) return;
                      const nextCol = col.slice();
                      nextCol.splice(currentIndex, 1);
                      nextCol.splice(targetIndex, 0, tabName);
                      const nextCols = columnsRef.current.slice();
                      nextCols[colIndex] = nextCol;
                      persistBoard(tabOrder, flattenColumns(nextCols), nextCols);
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
      {settingsOpen && (
        <SettingsShell
          onOpenWizard={onOpenWizard}
          onClose={closeSettings}
        />
      )}
    </>
  );
}
