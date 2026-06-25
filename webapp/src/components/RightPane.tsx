import { useState, useEffect, useRef } from "react";
import { Database, Globe, FolderTree, GitBranch, GitFork, Plug, GraduationCap, Settings } from "lucide-react";
import StatePane from "./StatePane";
import BrowserPane from "./BrowserPane";
import FileTree from "./FileTree";
import SourceControl from "./SourceControl";
import WorktreesPane from "./WorktreesPane";
import McpPane from "./McpPane";
import SkillsPane from "./SkillsPane";
import SettingsPane from "./SettingsPane";

type Tab = "state" | "browser" | "files" | "git" | "worktrees" | "mcp" | "skills" | "settings";

export default function RightPane({ artifacts, onOpenWizard }: {
  artifacts: { type: string; headline: string; confidence?: number }[];
  onOpenWizard: () => void;
}) {
  const [tab, setTab] = useState<Tab>("state");
  const asideRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState<number>(600); // Sensible default for wide layout before measure

  useEffect(() => {
    if (!asideRef.current) return;
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setWidth(entry.contentRect.width);
      }
    });
    observer.observe(asideRef.current);
    return () => observer.disconnect();
  }, []);

  const showLabel = (tabName: Tab) => {
    if (width >= 580) return true;
    if (width >= 380) return tab === tabName;
    return false;
  };

  return (
    <aside ref={asideRef} className="bg-panel border-l border-edge flex flex-col h-full overflow-hidden min-w-0">
      <div className="flex flex-nowrap border-b border-edge overflow-x-auto scrollbar-none select-none">
        <TabBtn active={tab === "state"} onClick={() => setTab("state")} icon={<Database size={12} />} label="State" showLabel={showLabel("state")} />
        <TabBtn active={tab === "browser"} onClick={() => setTab("browser")} icon={<Globe size={12} />} label="Browser" showLabel={showLabel("browser")} />
        <TabBtn active={tab === "files"} onClick={() => setTab("files")} icon={<FolderTree size={12} />} label="Files" showLabel={showLabel("files")} />
        <TabBtn active={tab === "git"} onClick={() => setTab("git")} icon={<GitBranch size={12} />} label="Git" showLabel={showLabel("git")} />
        <TabBtn active={tab === "worktrees"} onClick={() => setTab("worktrees")} icon={<GitFork size={12} />} label="Worktrees" showLabel={showLabel("worktrees")} />
        <TabBtn active={tab === "mcp"} onClick={() => setTab("mcp")} icon={<Plug size={12} />} label="MCP" showLabel={showLabel("mcp")} />
        <TabBtn active={tab === "skills"} onClick={() => setTab("skills")} icon={<GraduationCap size={12} />} label="Skills" showLabel={showLabel("skills")} />
        <TabBtn active={tab === "settings"} onClick={() => setTab("settings")} icon={<Settings size={12} />} label="Settings" showLabel={showLabel("settings")} />
      </div>
      <div className="flex-1 overflow-hidden">
        {tab === "state" && <StatePane artifacts={artifacts} embedded />}
        {tab === "browser" && <BrowserPane />}
        {tab === "files" && <FileTree />}
        {tab === "git" && <SourceControl />}
        {tab === "worktrees" && <WorktreesPane />}
        {tab === "mcp" && <McpPane />}
        {tab === "skills" && <SkillsPane />}
        {tab === "settings" && <SettingsPane onOpenWizard={onOpenWizard} />}
      </div>
    </aside>
  );
}

function TabBtn({ active, onClick, icon, label, showLabel }: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  showLabel: boolean;
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
      className={`flex-1 min-w-[36px] flex items-center justify-center gap-1 py-2.5 px-1.5 text-[10px] uppercase tracking-wider font-medium transition whitespace-nowrap flex-shrink-0
        ${active ? "text-txt border-b-[1.5px] border-accent" : "text-faint hover:text-muted"}`}
    >
      <span className="flex-shrink-0 flex items-center justify-center">{icon}</span>
      {showLabel && <span className="text-[10px] tracking-wider select-none">{label}</span>}
    </button>
  );
}
