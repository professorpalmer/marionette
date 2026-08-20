import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  Database,
  FolderTree,
  GitBranch,
  GitFork,
  GitPullRequest,
  Globe,
  History,
  Coins,
  Network,
  PanelRight,
  PanelRightClose,
  Plus,
  Settings,
  SquareTerminal,
} from "lucide-react";
import { api } from "../lib/api";
import { lastSelectedProjectRoot } from "../lib/panelTransition";
import { writeSWRCache } from "../lib/useStaleWhileRevalidate";

/** Curated destinations for the floating tool windows — Cursor-style icon strip.
 *  Settings is pinned to the foot of the floating pill. */
const DOCK_LINKS: { id: string; tab: string; icon: ReactNode; title: string }[] = [
  {
    id: "swarm",
    tab: "swarm",
    icon: <Network size={15} strokeWidth={1.75} />,
    title: "Swarm tracker",
  },
  {
    id: "review",
    tab: "review",
    icon: <GitPullRequest size={15} strokeWidth={1.75} />,
    title: "Pending review / apply",
  },
  {
    id: "browser",
    tab: "browser",
    icon: <Globe size={15} strokeWidth={1.75} />,
    title: "In-app browser",
  },
  {
    id: "terminal",
    tab: "terminal",
    icon: <SquareTerminal size={15} strokeWidth={1.75} />,
    title: "Terminal (Ctrl/Cmd+`)",
  },
  {
    id: "state",
    tab: "state",
    icon: <Database size={15} strokeWidth={1.75} />,
    title: "CodeGraph, Wiki, MCP",
  },
];

const PANEL_OPTIONS = [
  { tab: "state", label: "State", icon: <Database size={12} /> },
  { tab: "swarm", label: "Swarm", icon: <Network size={12} /> },
  { tab: "economics", label: "Economics", icon: <Coins size={12} /> },
  { tab: "files", label: "Files", icon: <FolderTree size={12} /> },
  { tab: "git", label: "Git", icon: <GitBranch size={12} /> },
  { tab: "worktrees", label: "Worktrees", icon: <GitFork size={12} /> },
  { tab: "terminal", label: "Terminal", icon: <SquareTerminal size={12} /> },
  { tab: "review", label: "Review", icon: <GitPullRequest size={12} /> },
  { tab: "checkpoints", label: "History", icon: <History size={12} /> },
  { tab: "browser", label: "Browser", icon: <Globe size={12} /> },
];

function readStoredList(key: string): string[] {
  try {
    const value = JSON.parse(localStorage.getItem(key) || "[]");
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

export default function RightDock({
  onOpenTab,
  onExpand,
  onCollapse,
  panelsOpen = true,
}: {
  onOpenTab: (tab: string) => void;
  onExpand: () => void;
  onCollapse: () => void;
  panelsOpen?: boolean;
}) {
  const [reviewCount, setReviewCount] = useState(0);
  // Live swarm activity dot: the collapsed pill must show running jobs just
  // like the expanded tracker tab does, or background swarms go invisible.
  const [swarmRunning, setSwarmRunning] = useState(0);
  const [swarmRepo, setSwarmRepo] = useState<string | undefined>(
    () => lastSelectedProjectRoot() || undefined,
  );
  const [addMenuOpen, setAddMenuOpen] = useState(false);
  const [menuVersion, setMenuVersion] = useState(0);
  const addMenuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!addMenuOpen) return;
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (addMenuRef.current?.contains(event.target as Node)) return;
      setAddMenuOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setAddMenuOpen(false);
    };
    document.addEventListener("mousedown", closeOnOutsideClick);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeOnOutsideClick);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [addMenuOpen]);

  useEffect(() => {
    const onProject = (e: Event) => {
      const path = (e as CustomEvent<string>).detail;
      if (typeof path === "string") setSwarmRepo(path || undefined);
    };
    window.addEventListener("harness-project-selected", onProject);
    return () => window.removeEventListener("harness-project-selected", onProject);
  }, []);

  useEffect(() => {
    const load = () => {
      api.getReviews()
        .then((rows) => setReviewCount(Array.isArray(rows) ? rows.length : 0))
        .catch(() => {});
      api.swarmLive(swarmRepo)
        .then((data) => {
          // Parity with RightPane: seed SwarmPane's SWR cache from the dock poll
          // so expanding into the tracker after a collapsed session is warm too.
          writeSWRCache(`swarm:${swarmRepo || "__default__"}`, data);
          const jobs = Array.isArray(data?.jobs) ? data.jobs : [];
          const n = jobs.filter((j) => {
            const s = (j.status || "").toLowerCase();
            return s.includes("run") || s.includes("progress") || s.includes("active");
          }).length;
          setSwarmRunning(n);
        })
        .catch(() => {
          /* keep last known; dot is best-effort */
        });
    };
    load();
    const t = setInterval(load, 5000);
    window.addEventListener("harness-reviews-refresh", load);
    return () => {
      clearInterval(t);
      window.removeEventListener("harness-reviews-refresh", load);
    };
  }, [swarmRepo]);

  return (
    <aside
      className="pointer-events-none absolute right-4 top-[3.75rem] bottom-10 z-20 flex flex-col items-center select-none"
      aria-label="Floating panel shortcuts"
    >
      {/* Same --shell-panel glass as the left rail: slightly darker keep so
          icons stay readable, no extra backdrop-blur island. */}
      <div
        data-testid="floating-dock-pill"
        className="pointer-events-auto flex flex-col items-center gap-0.5 rounded-2xl px-1 py-1.5 shell-inset-glass"
      >
        <button
          type="button"
          onClick={panelsOpen ? onCollapse : onExpand}
          title={panelsOpen ? "Hide panels (Ctrl/Cmd+J)" : "Show panels (Ctrl/Cmd+J)"}
          aria-label={panelsOpen ? "Hide panels" : "Show panels"}
          {...(panelsOpen ? { "data-testid": "panel-collapse-btn" } : {})}
          className="flex h-7 w-7 items-center justify-center rounded-xl text-faint hover:text-muted hover:bg-panel2/50 transition-colors focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent"
        >
          {panelsOpen ? (
            <PanelRightClose size={15} strokeWidth={1.75} />
          ) : (
            <PanelRight size={15} strokeWidth={1.75} />
          )}
        </button>

        <span className="my-0.5 h-px w-4 bg-edge/50" aria-hidden />

        <div ref={addMenuRef} className="relative">
          <button
            type="button"
            onClick={() => { setAddMenuOpen(open => !open); setMenuVersion(version => version + 1); }}
            aria-expanded={addMenuOpen}
            aria-haspopup="menu"
            aria-label="Add panel"
            title="Add panel"
            className="flex h-7 w-7 items-center justify-center rounded-xl text-faint hover:text-muted hover:bg-panel2/50 transition-colors focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent"
          >
            <Plus size={15} strokeWidth={1.75} />
          </button>
          {addMenuOpen && (
            <div key={menuVersion} role="menu" aria-label="Add panel" className="right-pane-add-menu right-[calc(100%+8px)] left-auto top-0">
              <div className="px-2 py-1 text-[9px] uppercase tracking-wider text-faint">Add panel</div>
              {(() => {
                const stored = readStoredList("pmharness.tabOrder");
                const fallback = PANEL_OPTIONS.map(option => option.tab);
                return stored.length > 0
                  ? [...stored, ...fallback.filter(tab => !stored.includes(tab))]
                  : fallback;
              })().filter(tab => tab !== "settings").map(tab => {
                const option = PANEL_OPTIONS.find(item => item.tab === tab);
                if (!option || readStoredList("pmharness.board.openCards").includes(tab)) return null;
                return (
                  <button
                    role="menuitem"
                    key={tab}
                    type="button"
                    aria-label={option.label}
                    onClick={() => {
                      onOpenTab(tab);
                      setAddMenuOpen(false);
                    }}
                    className="right-pane-add-item"
                  >
                    {option.icon}<span>{option.label}</span>
                  </button>
                );
              })}
              <button
                role="menuitem"
                type="button"
                onClick={() => {
                  onOpenTab("settings");
                  setAddMenuOpen(false);
                }}
                className="right-pane-add-item"
              >
                <Settings size={12} /><span>Settings</span>
              </button>
            </div>
          )}
        </div>

        {DOCK_LINKS.map((link) => (
          <button
            key={link.id}
            type="button"
            onClick={() => onOpenTab(link.tab)}
            title={link.title}
            className="relative flex h-7 w-7 items-center justify-center rounded-xl text-faint hover:text-muted hover:bg-panel2/50 transition-colors focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent"
          >
            {link.icon}
            {link.id === "swarm" && swarmRunning > 0 && (
              <span
                title={`${swarmRunning} swarm job${swarmRunning === 1 ? "" : "s"} running`}
                className="absolute -top-0.5 -right-0.5 h-1.5 w-1.5 rounded-full bg-accent animate-pulse"
              />
            )}
            {link.id === "review" && reviewCount > 0 && (
              <span className="absolute -top-0.5 -right-0.5 min-w-[0.875rem] h-3.5 px-0.5 rounded-full bg-accent text-panel text-[8px] font-bold flex items-center justify-center border border-panel">
                {reviewCount > 9 ? "9+" : reviewCount}
              </span>
            )}
          </button>
        ))}

        <button
          type="button"
          onClick={() => onOpenTab("settings")}
          title="Settings (Ctrl/Cmd+Shift+J)"
          aria-label="Settings"
          className="flex h-7 w-7 items-center justify-center rounded-xl text-faint hover:text-muted hover:bg-panel2/50 transition-colors focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent"
        >
          <Settings size={15} strokeWidth={1.75} />
        </button>
      </div>
    </aside>
  );
}
