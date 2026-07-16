import { useEffect, useState, type ReactNode } from "react";
import {
  Database,
  GitPullRequest,
  Globe,
  Network,
  PanelRight,
  Settings,
  SquareTerminal,
} from "lucide-react";
import { api } from "../lib/api";

/** Curated destinations when the right pane is collapsed — Cursor-style icon strip.
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
      className="pointer-events-none absolute right-4 top-[3.75rem] bottom-10 z-20 flex flex-col items-center select-none"
      aria-label="Side panel shortcuts"
    >
      {/* Floating pill — transparent so the chat gradient reads through. */}
      <div
        className="pointer-events-auto flex flex-col items-center gap-0.5 rounded-2xl px-1 py-1.5
          bg-panel/35 backdrop-blur-md border border-edge/30 shadow-[0_4px_16px_rgba(0,0,0,0.22)]"
      >
        <button
          type="button"
          onClick={onExpand}
          title="Open side panel (Ctrl/Cmd+J)"
          className="flex h-7 w-7 items-center justify-center rounded-xl text-muted hover:text-txt hover:bg-panel2/50 transition-colors"
        >
          <PanelRight size={15} strokeWidth={1.75} />
        </button>

        <span className="my-0.5 h-px w-4 bg-edge/50" aria-hidden />

        {DOCK_LINKS.map((link) => (
          <button
            key={link.id}
            type="button"
            onClick={() => onOpenTab(link.tab)}
            title={link.title}
            className="relative flex h-7 w-7 items-center justify-center rounded-xl text-muted hover:text-txt hover:bg-panel2/50 transition-colors"
          >
            {link.icon}
            {link.id === "review" && reviewCount > 0 && (
              <span className="absolute -top-0.5 -right-0.5 min-w-[0.875rem] h-3.5 px-0.5 rounded-full bg-accent text-panel text-[8px] font-bold flex items-center justify-center border border-panel">
                {reviewCount > 9 ? "9+" : reviewCount}
              </span>
            )}
          </button>
        ))}

        <span className="my-0.5 h-px w-4 bg-edge/50" aria-hidden />

        <button
          type="button"
          onClick={() => onOpenTab("settings")}
          title="Settings (Ctrl/Cmd+Shift+J)"
          className="flex h-7 w-7 items-center justify-center rounded-xl text-muted hover:text-txt hover:bg-panel2/50 transition-colors"
        >
          <Settings size={15} strokeWidth={1.75} />
        </button>
      </div>
    </aside>
  );
}
