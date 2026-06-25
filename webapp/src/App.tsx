import { useEffect, useState } from "react";
import { api, type Config } from "./lib/api";
import LeftRail from "./components/LeftRail";
import Conversation from "./components/Conversation";
import RightPane from "./components/RightPane";
import TaskStack from "./components/TaskStack";
import StatusBar from "./components/StatusBar";
import Resizer from "./components/Resizer";
import RegistryWizard from "./components/RegistryWizard";

const LS = {
  left: "pmharness.leftW", right: "pmharness.rightW",
  leftOpen: "pmharness.leftOpen", rightOpen: "pmharness.rightOpen",
};
const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n));
const num = (k: string, d: number) => { const v = Number(localStorage.getItem(k)); return Number.isFinite(v) && v > 0 ? v : d; };
const bool = (k: string, d: boolean) => { const v = localStorage.getItem(k); return v === null ? d : v === "1"; };

export default function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [artifacts, setArtifacts] = useState<{ type: string; headline: string; confidence?: number }[]>([]);
  const [jobsRefresh, setJobsRefresh] = useState(0);
  const [jobCount, setJobCount] = useState(0);

  const [leftW, setLeftW] = useState(() => num(LS.left, 248));
  const [rightW, setRightW] = useState(() => num(LS.right, 320));
  const [leftOpen, setLeftOpen] = useState(() => bool(LS.leftOpen, true));
  const [rightOpen, setRightOpen] = useState(() => bool(LS.rightOpen, true));

  const [showWizard, setShowWizard] = useState(false);

  useEffect(() => { api.config().then(setConfig).catch(() => {}); }, []);
  useEffect(() => { api.jobs().then((j) => setJobCount(j.length)).catch(() => {}); }, [jobsRefresh]);

  // First-run behavior checking
  useEffect(() => {
    const checkSetupStatus = async () => {
      const seen = localStorage.getItem("pmharness.wizardSeen");
      if (seen === "1") return;

      try {
        const provs = await api.providers();
        const hasAnyKey = provs.some((p) => p.has_key);
        if (!hasAnyKey || seen === null) {
          setShowWizard(true);
        }
      } catch (err) {
        console.error("Failed to check provider setup", err);
        if (seen === null) {
          setShowWizard(true);
        }
      }
    };
    checkSetupStatus();
  }, []);

  // persist layout
  useEffect(() => { localStorage.setItem(LS.left, String(leftW)); }, [leftW]);
  useEffect(() => { localStorage.setItem(LS.right, String(rightW)); }, [rightW]);
  useEffect(() => { localStorage.setItem(LS.leftOpen, leftOpen ? "1" : "0"); }, [leftOpen]);
  useEffect(() => { localStorage.setItem(LS.rightOpen, rightOpen ? "1" : "0"); }, [rightOpen]);

  // hotkeys: Cmd/Ctrl+B toggles left sessions panel; Cmd/Ctrl+J toggles right pane.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;
      if (e.key.toLowerCase() === "b") { e.preventDefault(); setLeftOpen((v) => !v); }
      else if (e.key.toLowerCase() === "j") { e.preventDefault(); setRightOpen((v) => !v); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="h-full flex flex-col">
      <div className="flex-1 min-h-0 flex">
        {leftOpen && (
          <>
            <div style={{ width: leftW }} className="shrink-0 h-full overflow-hidden">
              <LeftRail jobsRefresh={jobsRefresh} onSessionChange={setActiveSessionId} />
            </div>
            <Resizer side="left" onResize={(dx) => setLeftW((w) => clamp(w + dx, 180, 420))} />
          </>
        )}
        <div className="flex-1 min-w-0 h-full">
          <Conversation
            config={config}
            activeSessionId={activeSessionId}
            onArtifacts={(a) => setArtifacts((prev) => [...a, ...prev])}
            onJobChange={() => setJobsRefresh((n) => n + 1)}
          />
        </div>
        {rightOpen && (
          <>
            <Resizer side="right" onResize={(dx) => setRightW((w) => clamp(w + dx, 240, 640))} />
            <div style={{ width: rightW }} className="shrink-0 h-full overflow-hidden">
              <RightPane artifacts={artifacts} onOpenWizard={() => setShowWizard(true)} />
            </div>
          </>
        )}
      </div>
      <TaskStack refresh={jobsRefresh} />
      <StatusBar config={config} jobCount={jobCount}
        leftOpen={leftOpen} rightOpen={rightOpen}
        onToggleLeft={() => setLeftOpen((v) => !v)} onToggleRight={() => setRightOpen((v) => !v)} />

      {showWizard && <RegistryWizard onClose={() => setShowWizard(false)} />}
    </div>
  );
}
