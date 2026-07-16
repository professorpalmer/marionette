import { useState, useEffect, useRef, Fragment } from "react";
import { Database, Globe, FolderTree, GitBranch, GitFork, Settings, SquareTerminal, Columns, Rows, Split, X, History, GitPullRequest, Network } from "lucide-react";
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

type Tab = "state" | "files" | "git" | "worktrees" | "terminal" | "browser" | "settings" | "checkpoints" | "review" | "swarm";

const TAB_CONFIG: Record<Tab, { label: string; icon: React.ReactNode }> = {
  state: { label: "State", icon: <Database size={12} /> },
  files: { label: "Files", icon: <FolderTree size={12} /> },
  git: { label: "Git", icon: <GitBranch size={12} /> },
  worktrees: { label: "Worktrees", icon: <GitFork size={12} /> },
  terminal: { label: "Terminal", icon: <SquareTerminal size={12} /> },
  browser: { label: "Browser", icon: <Globe size={12} /> },
  settings: { label: "Settings", icon: <Settings size={12} /> },
  checkpoints: { label: "History", icon: <History size={12} /> },
  review: { label: "Review", icon: <GitPullRequest size={12} /> },
  swarm: { label: "Swarm", icon: <Network size={12} /> },
};

// Visual grouping for the tab bar: Workspace | Changes | Tools, with Settings pinned last.
// A thin divider is rendered between groups so icons read as organized clusters
// instead of one crowded row. Group membership also drives the canonical default order.
const TAB_GROUPS: { group: string; tabs: Tab[] }[] = [
  { group: "workspace", tabs: ["state", "swarm", "files", "git", "worktrees", "terminal"] },
  { group: "changes", tabs: ["review", "checkpoints"] },
  { group: "tools", tabs: ["browser"] },
];
// Settings is intentionally separated and rendered last (after a flex spacer).
const PINNED_LAST: Tab = "settings";
const groupOf = (t: Tab): string => {
  for (const g of TAB_GROUPS) if (g.tabs.includes(t)) return g.group;
  return "settings";
};
const CANONICAL_ORDER: Tab[] = [
  ...TAB_GROUPS.flatMap(g => g.tabs),
  PINNED_LAST,
];

interface SplitState {
  isSplit: boolean;
  primaryTab: Tab;
  secondaryTab: Tab;
  direction: "horizontal" | "vertical";
  percent: number;
}

export default function RightPane({ artifacts, onOpenWizard }: {
  artifacts: { type: string; headline: string; confidence?: number }[];
  onOpenWizard: () => void;
}) {
  const asideRef = useRef<HTMLDivElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [isResizing, setIsResizing] = useState(false);

  // Tab order state (drag to reorder, persisted in localStorage)
  const [tabOrder, setTabOrder] = useState<Tab[]>(() => {
    let order: Tab[] = CANONICAL_ORDER.slice();
    const saved = localStorage.getItem("pmharness.tabOrder");
    if (saved) {
      try {
        const parsed = JSON.parse(saved) as string[];
        const validTabs: Tab[] = CANONICAL_ORDER.slice();
        // Drop retired "mcp" tab (merged into State).
        const filtered = parsed.filter((t): t is Tab => validTabs.includes(t as Tab));
        const missing = validTabs.filter(t => !filtered.includes(t));
        // Always keep Settings pinned last regardless of saved order.
        const merged = [...filtered, ...missing].filter(t => t !== PINNED_LAST);
        order = [...merged, PINNED_LAST];
      } catch (e) {
        // fallback to canonical
      }
    }
    // One-time migration: promote Swarm to the 2nd tab so the tracker is a
    // first-class default, without discarding a user's other tab reordering.
    if (!localStorage.getItem("pmharness.tabOrder.swarm2nd")) {
      order = order.filter(t => t !== "swarm");
      order.splice(1, 0, "swarm");
      localStorage.setItem("pmharness.tabOrder.swarm2nd", "1");
      localStorage.setItem("pmharness.tabOrder", JSON.stringify(order));
    }
    if (!localStorage.getItem("pmharness.tabOrder.mcpMerged")) {
      order = order.filter(t => (t as string) !== "mcp");
      localStorage.setItem("pmharness.tabOrder.mcpMerged", "1");
      localStorage.setItem("pmharness.tabOrder", JSON.stringify(order));
    }
    return order;
  });

  const saveTabOrder = (newOrder: Tab[]) => {
    setTabOrder(newOrder);
    localStorage.setItem("pmharness.tabOrder", JSON.stringify(newOrder));
  };

  // Drag and drop state for reordering tabs
  const [draggedTab, setDraggedTab] = useState<Tab | null>(null);

  const handleDragStart = (e: React.DragEvent, tabId: Tab) => {
    setDraggedTab(tabId);
    e.dataTransfer.effectAllowed = "move";
  };

  const handleDragOver = (e: React.DragEvent, targetTab: Tab) => {
    e.preventDefault();
    if (!draggedTab || draggedTab === targetTab) return;

    const fromIndex = tabOrder.indexOf(draggedTab);
    const toIndex = tabOrder.indexOf(targetTab);
    if (fromIndex !== -1 && toIndex !== -1) {
      const updated = [...tabOrder];
      updated.splice(fromIndex, 1);
      updated.splice(toIndex, 0, draggedTab);
      saveTabOrder(updated);
    }
  };

  const handleDragEnd = () => {
    setDraggedTab(null);
  };

  // Split state (persisted in localStorage)
  const [splitState, setSplitState] = useState<SplitState>(() => {
    const saved = localStorage.getItem("pmharness.splitState");
    if (saved) {
      try {
        const parsed = JSON.parse(saved);
        const validTabs: Tab[] = ["state", "files", "git", "worktrees", "terminal", "browser", "settings", "checkpoints", "review", "swarm"];
        const remap = (t: string): Tab => (t === "mcp" ? "state" : (validTabs.includes(t as Tab) ? t as Tab : "state"));
        // Settings is a destination you visit, not a home tab: restoring it as
        // the startup tab made every launch open on the settings page whenever
        // the previous session ended there.
        let primaryTab = remap(parsed.primaryTab);
        if (primaryTab === "settings") primaryTab = "state";
        const secondaryTab = remap(parsed.secondaryTab || "terminal");
        return {
          isSplit: !!parsed.isSplit,
          primaryTab,
          secondaryTab,
          direction: parsed.direction === "vertical" ? "vertical" : "horizontal",
          percent: (typeof parsed.percent === "number" && parsed.percent >= 20 && parsed.percent <= 80) ? parsed.percent : 50,
        };
      } catch (e) {
        // fallback
      }
    }
    return {
      isSplit: false,
      primaryTab: "state",
      secondaryTab: "terminal",
      direction: "horizontal",
      percent: 50,
    };
  });

  const [reviews, setReviews] = useState<PendingReview[]>([]);
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
        }
      })
      .catch((err) => console.error("Failed to load reviews:", err));
  };

  const fetchSwarmActivity = () => {
    return api.swarmLive(swarmRepo)
      .then((data) => {
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

  const updateSplitState = (updater: Partial<SplitState> | ((prev: SplitState) => SplitState)) => {
    setSplitState(prev => {
      const next = typeof updater === "function" ? updater(prev) : { ...prev, ...updater };
      localStorage.setItem("pmharness.splitState", JSON.stringify(next));
      return next;
    });
  };

  // Hotkey listener
  useEffect(() => {
    const onFocusTab = (e: any) => {
      if (e?.detail) {
        // MCP merged into State; expand that section when something asks for MCP.
        if (e.detail === "mcp") {
          updateSplitState({ primaryTab: "state" });
          window.dispatchEvent(new Event("harness-expand-mcp"));
          return;
        }
        const targetTab = e.detail as Tab;
        const validTabs: Tab[] = ["state", "files", "git", "worktrees", "terminal", "browser", "settings", "swarm", "checkpoints", "review"];
        if (validTabs.includes(targetTab)) {
          updateSplitState({ primaryTab: targetTab });
        }
      }
    };
    window.addEventListener("harness-focus-tab", onFocusTab as EventListener);
    return () => window.removeEventListener("harness-focus-tab", onFocusTab as EventListener);
  }, []);

  // Draggable divider resize handler
  const handleMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    setIsResizing(true);
  };

  useEffect(() => {
    if (!isResizing) return;

    const handleMouseMove = (e: MouseEvent) => {
      if (!containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      let nextPercent = 50;

      // Per-pane minimum in PIXELS so neither sub-pane can be crushed below
      // a usable size (a fixed percent like 15% becomes ~150px on a small pane
      // and mangles the content). Each pane keeps at least MIN_PANE_PX.
      const MIN_PANE_PX = 260;
      const total = splitState.direction === "horizontal" ? rect.height : rect.width;
      if (splitState.direction === "horizontal") {
        const relativeY = e.clientY - rect.top;
        nextPercent = (relativeY / rect.height) * 100;
      } else {
        const relativeX = e.clientX - rect.left;
        nextPercent = (relativeX / rect.width) * 100;
      }

      // Clamp so BOTH panes keep >= MIN_PANE_PX. If the container is too small
      // to honor both minimums, fall back to a 50/50 split.
      let minPct = (MIN_PANE_PX / total) * 100;
      let maxPct = 100 - minPct;
      if (minPct >= maxPct) {
        nextPercent = 50;
      } else {
        nextPercent = Math.max(minPct, Math.min(maxPct, nextPercent));
      }
      updateSplitState({ percent: nextPercent });
    };

    const handleMouseUp = () => {
      setIsResizing(false);
    };

    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);
    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isResizing, splitState.direction]);

  // Re-clamp the split whenever the container resizes. The drag handler above
  // enforces the per-pane pixel minimum only WHILE dragging, so a restored
  // localStorage percent (or the pane shrinking under a narrow window) could
  // leave a sub-pane crushed below MIN_PANE_PX until the user re-dragged.
  useEffect(() => {
    if (!splitState.isSplit) return;
    const el = containerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const MIN_PANE_PX = 260;
    const reclampSplit = () => {
      const rect = el.getBoundingClientRect();
      const total = splitState.direction === "horizontal" ? rect.height : rect.width;
      if (total <= 0) return;
      const minPct = (MIN_PANE_PX / total) * 100;
      const maxPct = 100 - minPct;
      updateSplitState((prev) => {
        const clamped = minPct >= maxPct ? 50 : Math.max(minPct, Math.min(maxPct, prev.percent));
        return clamped === prev.percent ? prev : { ...prev, percent: clamped };
      });
    };
    const observer = new ResizeObserver(reclampSplit);
    observer.observe(el);
    reclampSplit();
    return () => observer.disconnect();
  }, [splitState.isSplit, splitState.direction]);

  // Compute label visibility based on sub-pane widths


  // StatePane stays mounted in the primary pane (CSS-hidden when inactive) so
  // Codegraph/Wiki SWR caches stay warm across right-rail tab swaps. Secondary
  // split still mounts on demand (rare). Other tabs still unmount.
  const renderPaneBody = (activeTab: Tab, keepStateWarm: boolean) => (
    <div className="relative h-full min-h-0">
      {keepStateWarm ? (
        <div
          className={activeTab === "state" ? "h-full" : "hidden"}
          aria-hidden={activeTab !== "state"}
        >
          <ErrorBoundary label="State" inline>
            <StatePane artifacts={artifacts} embedded />
          </ErrorBoundary>
        </div>
      ) : (
        activeTab === "state" && (
          <ErrorBoundary label="State" inline>
            <StatePane artifacts={artifacts} embedded />
          </ErrorBoundary>
        )
      )}
      {activeTab !== "state" && (
        <ErrorBoundary key={activeTab} label={TAB_CONFIG[activeTab]?.label || activeTab} inline>
          {renderTabInner(activeTab)}
        </ErrorBoundary>
      )}
    </div>
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
        return (
          <SettingsShell
            onOpenWizard={onOpenWizard}
            onClose={() =>
              setSplitState((prev) => {
                // SettingsShell is a full-window (fixed inset-0) overlay, so it
                // renders whenever EITHER pane's tab is "settings". Resetting only
                // primaryTab left it stuck open when Settings was opened in the
                // secondary pane -- close whichever pane(s) show it.
                const next = { ...prev };
                if (next.primaryTab === "settings") next.primaryTab = "state" as Tab;
                if (next.secondaryTab === "settings") next.secondaryTab = "state" as Tab;
                localStorage.setItem("pmharness.splitState", JSON.stringify(next));
                return next;
              })
            }
          />
        );
      case "checkpoints":
        return <CheckpointsPane />;
      case "swarm":
        return <SwarmPane />;
      case "review":
        return <DiffReviewPane reviews={reviews} onRefresh={fetchReviews} />;
      default:
        return null;
    }
  };

  return (
    <aside ref={asideRef} className="bg-panel border-l border-edge flex flex-col h-full overflow-hidden min-w-0">
      <div ref={containerRef} className={`flex-1 flex overflow-hidden min-h-0 ${splitState.isSplit && splitState.direction === "horizontal" ? "flex-col" : "flex-row"}`}>
        {/* Primary Pane */}
        <div
          className="flex flex-col overflow-hidden min-h-0 min-w-0"
          style={splitState.isSplit ? (splitState.direction === "horizontal" ? { height: `${splitState.percent}%` } : { width: `${splitState.percent}%` }) : { flex: 1 }}
        >
          {/* Primary Tab Bar */}
          <div className="flex flex-nowrap border-b border-edge overflow-x-auto scrollbar-none select-none">
            {tabOrder.filter(t => t !== PINNED_LAST).map((tabName, idx, arr) => {
              const config = TAB_CONFIG[tabName];
              const prev = idx > 0 ? arr[idx - 1] : null;
              const newGroup = prev !== null && groupOf(prev) !== groupOf(tabName);
              return (
                <Fragment key={tabName}>
                  {newGroup && <span className="self-center h-4 w-px bg-edge/60 mx-0.5 shrink-0" aria-hidden />}
                  <TabBtn
                    active={splitState.primaryTab === tabName}
                    onClick={() => updateSplitState({ primaryTab: tabName })}
                    icon={config.icon}
                    label={config.label}
                    showLabel={false}
                    draggable
                    onDragStart={(e) => handleDragStart(e, tabName)}
                    onDragOver={(e) => handleDragOver(e, tabName)}
                    onDragEnd={handleDragEnd}
                    className={draggedTab === tabName ? "opacity-30" : ""}
                    badge={tabName === "review" && reviews.length > 0 ? reviews.length : undefined}
                    live={tabName === "swarm" && swarmRunning > 0}
                    liveTitle={swarmRunning > 0 ? `${swarmRunning} swarm job${swarmRunning === 1 ? "" : "s"} running` : undefined}
                  />
                </Fragment>
              );
            })}
            {/* Settings pinned to the far end */}
            <span className="flex-1 min-w-[4px]" aria-hidden />
            <TabBtn
              active={splitState.primaryTab === PINNED_LAST}
              onClick={() => updateSplitState({ primaryTab: PINNED_LAST })}
              icon={TAB_CONFIG[PINNED_LAST].icon}
              label={TAB_CONFIG[PINNED_LAST].label}
              showLabel={false}
              className={`shrink-0 ${draggedTab === PINNED_LAST ? "opacity-30" : ""}`}
            />

            {/* Split controls */}
            <div className="flex items-center px-1 border-l border-edge bg-panel2/35 gap-0.5 shrink-0 select-none">
              {!splitState.isSplit ? (
                <button
                  onClick={() => updateSplitState({ isSplit: true, secondaryTab: splitState.primaryTab })}
                  title="Split Pane"
                  className="p-1.5 text-faint hover:text-txt hover:bg-edge/40 rounded transition-colors"
                >
                  <Split size={12} />
                </button>
              ) : (
                <>
                  <button
                    onClick={() => updateSplitState(prev => ({ ...prev, direction: prev.direction === "horizontal" ? "vertical" : "horizontal" }))}
                    title={splitState.direction === "horizontal" ? "Split Vertically" : "Split Horizontally"}
                    className="p-1.5 text-faint hover:text-txt hover:bg-edge/40 rounded transition-colors"
                  >
                    {splitState.direction === "horizontal" ? <Columns size={12} /> : <Rows size={12} />}
                  </button>
                  <button
                    onClick={() => updateSplitState({ isSplit: false })}
                    title="Close Split"
                    className="p-1.5 text-faint hover:text-risk hover:bg-edge/40 rounded transition-colors"
                  >
                    <X size={12} />
                  </button>
                </>
              )}
            </div>
          </div>

          {/* Primary Pane Content */}
          <div className="flex-1 overflow-hidden min-h-0">
            {renderPaneBody(splitState.primaryTab, true)}
          </div>
        </div>

        {/* Resizable Split Divider */}
        {splitState.isSplit && (
          <div
            onMouseDown={handleMouseDown}
            className={
              splitState.direction === "horizontal"
                ? "h-1 hover:h-1.5 cursor-row-resize bg-edge hover:bg-accent/40 border-t border-b border-edge/35 transition-all select-none shrink-0"
                : "w-1 hover:w-1.5 cursor-col-resize bg-edge hover:bg-accent/40 border-l border-r border-edge/35 transition-all select-none shrink-0"
            }
          />
        )}

        {/* Secondary Pane */}
        {splitState.isSplit && (
          <div
            className="flex flex-col overflow-hidden min-h-0 min-w-0"
            style={splitState.direction === "horizontal" ? { height: `${100 - splitState.percent}%` } : { width: `${100 - splitState.percent}%` }}
          >
            {/* Secondary Tab Bar */}
            <div className="flex flex-nowrap border-b border-edge overflow-x-auto scrollbar-none select-none">
              {tabOrder.filter(t => t !== PINNED_LAST).map((tabName, idx, arr) => {
                const config = TAB_CONFIG[tabName];
                const prev = idx > 0 ? arr[idx - 1] : null;
                const newGroup = prev !== null && groupOf(prev) !== groupOf(tabName);
                return (
                  <Fragment key={tabName}>
                    {newGroup && <span className="self-center h-4 w-px bg-edge/60 mx-0.5 shrink-0" aria-hidden />}
                    <TabBtn
                      active={splitState.secondaryTab === tabName}
                      onClick={() => updateSplitState({ secondaryTab: tabName })}
                      icon={config.icon}
                      label={config.label}
                      showLabel={false}
                      draggable
                      onDragStart={(e) => handleDragStart(e, tabName)}
                      onDragOver={(e) => handleDragOver(e, tabName)}
                      onDragEnd={handleDragEnd}
                      className={draggedTab === tabName ? "opacity-30" : ""}
                      badge={tabName === "review" && reviews.length > 0 ? reviews.length : undefined}
                      live={tabName === "swarm" && swarmRunning > 0}
                      liveTitle={swarmRunning > 0 ? `${swarmRunning} swarm job${swarmRunning === 1 ? "" : "s"} running` : undefined}
                    />
                  </Fragment>
                );
              })}
              <span className="flex-1 min-w-[4px]" aria-hidden />
              <TabBtn
                active={splitState.secondaryTab === PINNED_LAST}
                onClick={() => updateSplitState({ secondaryTab: PINNED_LAST })}
                icon={TAB_CONFIG[PINNED_LAST].icon}
                label={TAB_CONFIG[PINNED_LAST].label}
                showLabel={false}
                className="shrink-0"
              />

              {/* Split controls for Secondary Pane */}
              <div className="flex items-center px-1 border-l border-edge bg-panel2/35 gap-0.5 shrink-0 select-none">
                <button
                  onClick={() => updateSplitState(prev => ({ ...prev, direction: prev.direction === "horizontal" ? "vertical" : "horizontal" }))}
                  title={splitState.direction === "horizontal" ? "Split Vertically" : "Split Horizontally"}
                  className="p-1.5 text-faint hover:text-txt hover:bg-edge/40 rounded transition-colors"
                >
                  {splitState.direction === "horizontal" ? <Columns size={12} /> : <Rows size={12} />}
                </button>
                <button
                  onClick={() => updateSplitState({ isSplit: false })}
                  title="Close Split"
                  className="p-1.5 text-faint hover:text-risk hover:bg-edge/40 rounded transition-colors"
                >
                  <X size={12} />
                </button>
              </div>
            </div>

            {/* Secondary Pane Content */}
            <div className="flex-1 overflow-hidden min-h-0">
              {renderPaneBody(splitState.secondaryTab, false)}
            </div>
          </div>
        )}
      </div>
    </aside>
  );
}

function TabBtn({ active, onClick, icon, label, showLabel, draggable, onDragStart, onDragOver, onDragEnd, className, badge, live, liveTitle }: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  showLabel: boolean;
  draggable?: boolean;
  onDragStart?: (e: React.DragEvent) => void;
  onDragOver?: (e: React.DragEvent) => void;
  onDragEnd?: (e: React.DragEvent) => void;
  className?: string;
  badge?: number;
  /** Pulsing activity light (e.g. Swarm running) -- distinct from a count badge. */
  live?: boolean;
  liveTitle?: string;
}) {
  const btnRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (active && btnRef.current) {
      btnRef.current.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
    }
  }, [active]);

  const tip = live && liveTitle ? `${label} — ${liveTitle}` : label;

  return (
    <button
      ref={btnRef}
      onClick={onClick}
      title={tip}
      aria-label={tip}
      draggable={draggable}
      onDragStart={onDragStart}
      onDragOver={onDragOver}
      onDragEnd={onDragEnd}
      className={`flex-1 min-w-0 overflow-hidden flex items-center justify-center gap-1 py-2 px-1 text-[10px] uppercase tracking-wider font-medium transition whitespace-nowrap
        ${active ? "text-txt border-b-[1.5px] border-accent bg-panel2/10" : "text-faint hover:text-muted hover:bg-panel2/5"} ${className || ""}`}
    >
      <span className="flex-shrink-0 flex items-center justify-center relative">
        {icon}
        {badge !== undefined && (
          <span className="absolute -top-1.5 -right-1.5 bg-accent text-panel text-[8px] font-bold h-3.5 w-3.5 flex items-center justify-center rounded-full border border-panel">
            {badge}
          </span>
        )}
        {live && badge === undefined && (
          <span
            className="absolute -top-0.5 -right-0.5 h-1.5 w-1.5 rounded-full bg-accent animate-pulse"
            aria-hidden
          />
        )}
      </span>
      {showLabel && <span className="text-[10px] tracking-wider select-none">{label}</span>}
    </button>
  );
}
