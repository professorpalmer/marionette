import { useState, useEffect, useRef } from "react";
import { Database, Globe, FolderTree, GitBranch, GitFork, Plug, Settings, SquareTerminal, Columns, Rows, Split, X, History, GitPullRequest } from "lucide-react";
import StatePane from "./StatePane";
import BrowserPane from "./BrowserPane";
import FileTree from "./FileTree";
import SourceControl from "./SourceControl";
import WorktreesPane from "./WorktreesPane";
import McpPane from "./McpPane";
import SettingsPane from "./SettingsPane";
import TerminalPane from "./TerminalPane";
import CheckpointsPane from "./CheckpointsPane";
import DiffReviewPane from "./DiffReviewPane";
import { api, type PendingReview } from "../lib/api";

type Tab = "state" | "files" | "git" | "worktrees" | "terminal" | "browser" | "mcp" | "settings" | "checkpoints" | "review";

const TAB_CONFIG: Record<Tab, { label: string; icon: React.ReactNode }> = {
  state: { label: "State", icon: <Database size={12} /> },
  files: { label: "Files", icon: <FolderTree size={12} /> },
  git: { label: "Git", icon: <GitBranch size={12} /> },
  worktrees: { label: "Worktrees", icon: <GitFork size={12} /> },
  terminal: { label: "Terminal", icon: <SquareTerminal size={12} /> },
  browser: { label: "Browser", icon: <Globe size={12} /> },
  mcp: { label: "MCP", icon: <Plug size={12} /> },
  settings: { label: "Settings", icon: <Settings size={12} /> },
  checkpoints: { label: "History", icon: <History size={12} /> },
  review: { label: "Review", icon: <GitPullRequest size={12} /> },
};

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
    const saved = localStorage.getItem("pmharness.tabOrder");
    if (saved) {
      try {
        const parsed = JSON.parse(saved) as Tab[];
        const validTabs: Tab[] = ["state", "files", "git", "worktrees", "terminal", "browser", "mcp", "settings", "checkpoints", "review"];
        const filtered = parsed.filter(t => validTabs.includes(t));
        const missing = validTabs.filter(t => !filtered.includes(t));
        return [...filtered, ...missing];
      } catch (e) {
        // fallback
      }
    }
    return ["state", "files", "git", "worktrees", "terminal", "browser", "mcp", "settings", "checkpoints", "review"];
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
        const validTabs: Tab[] = ["state", "files", "git", "worktrees", "terminal", "browser", "mcp", "settings", "checkpoints", "review"];
        const primaryTab = validTabs.includes(parsed.primaryTab) ? parsed.primaryTab : "state";
        const secondaryTab = validTabs.includes(parsed.secondaryTab) ? parsed.secondaryTab : "terminal";
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

  const fetchReviews = () => {
    api.getReviews()
      .then((data) => {
        if (Array.isArray(data)) {
          setReviews(data);
        }
      })
      .catch((err) => console.error("Failed to load reviews:", err));
  };

  useEffect(() => {
    fetchReviews();
    const interval = setInterval(fetchReviews, 4000);
    return () => clearInterval(interval);
  }, []);

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
        const targetTab = e.detail as Tab;
        const validTabs: Tab[] = ["state", "files", "git", "worktrees", "terminal", "browser", "mcp", "settings"];
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

  // Compute label visibility based on sub-pane widths


  const renderTabContent = (tabName: Tab) => {
    switch (tabName) {
      case "state":
        return <StatePane artifacts={artifacts} embedded />;
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
      case "mcp":
        return <McpPane />;
      case "settings":
        return <SettingsPane onOpenWizard={onOpenWizard} />;
      case "checkpoints":
        return <CheckpointsPane />;
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
            {tabOrder.map((tabName) => {
              const config = TAB_CONFIG[tabName];
              const show = false; // icon-only always -- labels caused resize/stretch issues; tooltips via title attr
              return (
                <TabBtn
                  key={tabName}
                  active={splitState.primaryTab === tabName}
                  onClick={() => updateSplitState({ primaryTab: tabName })}
                  icon={config.icon}
                  label={config.label}
                  showLabel={show}
                  draggable
                  onDragStart={(e) => handleDragStart(e, tabName)}
                  onDragOver={(e) => handleDragOver(e, tabName)}
                  onDragEnd={handleDragEnd}
                  className={draggedTab === tabName ? "opacity-30" : ""}
                  badge={tabName === "review" && reviews.length > 0 ? reviews.length : undefined}
                />
              );
            })}

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
            {renderTabContent(splitState.primaryTab)}
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
              {tabOrder.map((tabName) => {
                const config = TAB_CONFIG[tabName];
                const show = false; // icon-only always -- labels caused resize/stretch issues; tooltips via title attr
                return (
                  <TabBtn
                    key={tabName}
                    active={splitState.secondaryTab === tabName}
                    onClick={() => updateSplitState({ secondaryTab: tabName })}
                    icon={config.icon}
                    label={config.label}
                    showLabel={show}
                    draggable
                    onDragStart={(e) => handleDragStart(e, tabName)}
                    onDragOver={(e) => handleDragOver(e, tabName)}
                    onDragEnd={handleDragEnd}
                    className={draggedTab === tabName ? "opacity-30" : ""}
                    badge={tabName === "review" && reviews.length > 0 ? reviews.length : undefined}
                  />
                );
              })}

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
              {renderTabContent(splitState.secondaryTab)}
            </div>
          </div>
        )}
      </div>
    </aside>
  );
}

function TabBtn({ active, onClick, icon, label, showLabel, draggable, onDragStart, onDragOver, onDragEnd, className, badge }: {
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
}) {
  const btnRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (active && btnRef.current) {
      btnRef.current.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
    }
  }, [active]);

  return (
    <button
      ref={btnRef}
      onClick={onClick}
      title={label}
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
      </span>
      {showLabel && <span className="text-[10px] tracking-wider select-none">{label}</span>}
    </button>
  );
}
