import { useState } from "react";
import { Database, Globe, FolderTree, GitBranch, Plug, GraduationCap, Settings } from "lucide-react";
import StatePane from "./StatePane";
import BrowserPane from "./BrowserPane";
import FileTree from "./FileTree";
import SourceControl from "./SourceControl";
import McpPane from "./McpPane";
import SkillsPane from "./SkillsPane";
import SettingsPane from "./SettingsPane";

type Tab = "state" | "browser" | "files" | "git" | "mcp" | "skills" | "settings";

export default function RightPane({ artifacts, onOpenWizard }: {
  artifacts: { type: string; headline: string; confidence?: number }[];
  onOpenWizard: () => void;
}) {
  const [tab, setTab] = useState<Tab>("state");
  return (
    <aside className="bg-panel border-l border-edge flex flex-col h-full overflow-hidden">
      <div className="flex border-b border-edge">
        <TabBtn active={tab === "state"} onClick={() => setTab("state")} icon={<Database size={12} />} label="State" />
        <TabBtn active={tab === "browser"} onClick={() => setTab("browser")} icon={<Globe size={12} />} label="Browser" />
        <TabBtn active={tab === "files"} onClick={() => setTab("files")} icon={<FolderTree size={12} />} label="Files" />
        <TabBtn active={tab === "git"} onClick={() => setTab("git")} icon={<GitBranch size={12} />} label="Git" />
        <TabBtn active={tab === "mcp"} onClick={() => setTab("mcp")} icon={<Plug size={12} />} label="MCP" />
        <TabBtn active={tab === "skills"} onClick={() => setTab("skills")} icon={<GraduationCap size={12} />} label="Skills" />
        <TabBtn active={tab === "settings"} onClick={() => setTab("settings")} icon={<Settings size={12} />} label="Settings" />
      </div>
      <div className="flex-1 overflow-hidden">
        {tab === "state" && <StatePane artifacts={artifacts} embedded />}
        {tab === "browser" && <BrowserPane />}
        {tab === "files" && <FileTree />}
        {tab === "git" && <SourceControl />}
        {tab === "mcp" && <McpPane />}
        {tab === "skills" && <SkillsPane />}
        {tab === "settings" && <SettingsPane onOpenWizard={onOpenWizard} />}
      </div>
    </aside>
  );
}

function TabBtn({ active, onClick, icon, label }: any) {
  return (
    <button onClick={onClick}
      className={`flex-1 flex items-center justify-center gap-1.5 py-2.5 text-[10px] uppercase tracking-wider font-medium transition
        ${active ? "text-txt border-b-[1.5px] border-accent" : "text-faint hover:text-muted"}`}>
      {icon}{label}
    </button>
  );
}
