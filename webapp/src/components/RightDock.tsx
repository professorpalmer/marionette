import { useEffect, useState, type ReactNode } from "react";
import {
  Database,
  GitPullRequest,
  Globe,
  Network,
  PanelRight,
  SquareTerminal,
} from "lucide-react";
import { api } from "../lib/api";

/** Curated destinations when the right pane is collapsed — Cursor-style, fixed set. */
const DOCK_LINKS: { id: string; tab: string; label: string; icon: ReactNode; title: string }[] = [
  {
    id: "swarm",
    tab: "swarm",
    label: "Swarm",
    icon: <Network size={14} />,
    title: "Swarm tracker",
  },
  {
    id: "review",
    tab: "review",
    label: "Changes",
    icon: <GitPullRequest size={14} />,
    title: "Pending review / apply",
  },
  {
    id: "browser",
    tab: "browser",
    label: "Browser",
    icon: <Globe size={14} />,
    title: "In-app browser",
  },
  {
    id: "terminal",
    tab: "terminal",
    label: "Terminal",
    icon: <SquareTerminal size={14} />,
    title: "Terminal (Ctrl/Cmd+`)",
  },
  {
    id: "state",
    tab: "state",
    label: "State",
    icon: <Database size={14} />,
    title: "CodeGraph, Wiki, MCP",
  },
];

export default function RightDock({
  onOpenTab,
  onExpand,
}: {
  onOpenTab: (tab: string) => void;
  onExpand: () => void;
}) {
  const [reviewCount, setReviewCount] = useState(0);

  useEffect(() => {
    const load = () => {
      api.getReviews()
        .then((rows) => setReviewCount(Array.isArray(rows) ? rows.length : 0))
        .catch(() => {});
    };
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  return (
    <aside
      className="shrink-0 h-full w-[4.5rem] border-l border-edge bg-panel/80 flex flex-col items-stretch py-2 select-none"
      aria-label="Side panel shortcuts"
    >
      <button
        type="button"
        onClick={onExpand}
        title="Open side panel (Ctrl/Cmd+J)"
        className="mx-1.5 mb-2 flex items-center justify-center gap-1 rounded-md px-1.5 py-1.5 text-[10px] text-muted hover:text-txt hover:bg-panel2/60 transition-colors"
      >
        <PanelRight size={13} />
        <span className="font-medium">Open</span>
      </button>

      <div className="flex-1 min-h-0 overflow-y-auto flex flex-col gap-0.5 px-1">
        {DOCK_LINKS.map((link) => (
          <button
            key={link.id}
            type="button"
            onClick={() => onOpenTab(link.tab)}
            title={link.title}
            className="relative flex flex-col items-center gap-1 rounded-md px-1 py-2.5 text-muted hover:text-txt hover:bg-panel2/50 transition-colors"
          >
            <span className="relative flex items-center justify-center">
              {link.icon}
              {link.id === "review" && reviewCount > 0 && (
                <span className="absolute -top-1.5 -right-2.5 min-w-[0.875rem] h-3.5 px-0.5 rounded-full bg-accent text-panel text-[8px] font-bold flex items-center justify-center border border-panel">
                  {reviewCount > 9 ? "9+" : reviewCount}
                </span>
              )}
            </span>
            <span className="text-[9px] font-medium tracking-wide leading-none">{link.label}</span>
          </button>
        ))}
      </div>
    </aside>
  );
}
