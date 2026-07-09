import { useEffect, useState } from "react";
import { api, type Config } from "./lib/api";
import LeftRail from "./components/LeftRail";
import Conversation from "./components/Conversation";
import RightPane from "./components/RightPane";
import StatusBar from "./components/StatusBar";
import UpdateBanner from "./components/UpdateBanner";
import ProviderKeyBanner from "./components/ProviderKeyBanner";
import Resizer from "./components/Resizer";
import RegistryWizard from "./components/RegistryWizard";
import ErrorBoundary from "./components/ErrorBoundary";

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

  useEffect(() => {
    setArtifacts([]);
  }, [activeSessionId]);

  const [leftW, setLeftW] = useState(() => num(LS.left, 248));
  const [rightW, setRightW] = useState(() => Math.max(340, num(LS.right, 340)));
  const [leftOpen, setLeftOpen] = useState(() => bool(LS.leftOpen, true));
  const [rightOpen, setRightOpen] = useState(() => bool(LS.rightOpen, true));

  const [showWizard, setShowWizard] = useState(false);

  const fetchConfig = () => {
    api.config().then(setConfig).catch(() => {});
  };

  // Prevent the Electron window from navigating to a file dropped anywhere
  // outside an explicit drop target (the default would replace the whole app
  // with the file). Composer + message drop zones stopPropagation, so they keep
  // working; this is the safety net for drops that miss those targets.
  useEffect(() => {
    const prevent = (e: DragEvent) => {
      if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) {
        e.preventDefault();
      }
    };
    window.addEventListener("dragover", prevent);
    window.addEventListener("drop", prevent);
    return () => {
      window.removeEventListener("dragover", prevent);
      window.removeEventListener("drop", prevent);
    };
  }, []);

  useEffect(() => { fetchConfig(); }, []);
  useEffect(() => {
    window.addEventListener("harness-config-changed", fetchConfig);
    return () => {
      window.removeEventListener("harness-config-changed", fetchConfig);
    };
  }, []);

  // First-run behavior checking
  useEffect(() => {
    const checkSetupStatus = async () => {
      const seen = localStorage.getItem("pmharness.wizardSeen");
      if (seen === "1") return;

      try {
        const provs = await api.providers();
        const hasAnyKey = provs.some((p) => p.has_key);
        // Only auto-open the setup wizard when there is genuinely NO provider key
        // configured (real first-run onboarding). Previously `seen === null` also
        // forced it open, so it popped up on EVERY launch even with keys already
        // set, until the user manually dismissed it. If a key already exists,
        // mark the wizard as seen so it never nags again.
        if (!hasAnyKey) {
          setShowWizard(true);
        } else {
          localStorage.setItem("pmharness.wizardSeen", "1");
        }
      } catch (err) {
        console.error("Failed to check provider setup", err);
        // On a status-check failure, do NOT force the wizard open -- an API hiccup
        // shouldn't shove the setup menu in the user's face on every launch.
      }
    };
    checkSetupStatus();
  }, []);

  // PERF: pause CSS animations when the app is backgrounded or the OS window is
  // not focused. Toggles html.app-idle (see index.css) so the shared macOS GPU
  // compositor goes idle instead of driving dozens of spinners/pulses at 60fps
  // while you are in another window -- the cause of alt-tab/window-switch stutter
  // during a long session with live swarms. blur/focus covers alt-tab (the window
  // can stay "visible" but unfocused); visibilitychange covers minimize/hide.
  useEffect(() => {
    const root = document.documentElement;
    const setIdle = () => {
      const idle = document.hidden || !document.hasFocus();
      root.classList.toggle("app-idle", idle);
    };
    setIdle();
    window.addEventListener("blur", setIdle);
    window.addEventListener("focus", setIdle);
    document.addEventListener("visibilitychange", setIdle);
    return () => {
      window.removeEventListener("blur", setIdle);
      window.removeEventListener("focus", setIdle);
      document.removeEventListener("visibilitychange", setIdle);
    };
  }, []);

  // persist layout
  useEffect(() => { localStorage.setItem(LS.left, String(leftW)); }, [leftW]);
  useEffect(() => { localStorage.setItem(LS.right, String(rightW)); }, [rightW]);

  // Re-clamp persisted rail widths against the real window width on mount and
  // whenever the window shrinks. The Resizer clamps only during a drag, so a
  // wide saved layout restored into a small window (or a live shrink) could
  // leave the two rails consuming nearly everything and crush the chat column
  // until the user manually re-dragged both handles.
  useEffect(() => {
    const MIN_CENTER = 360;
    const reclampRails = () => {
      const avail = window.innerWidth - MIN_CENTER;
      setLeftW((w) => clamp(Math.min(w, Math.max(180, avail - rightW)), 180, 420));
      setRightW((w) => clamp(Math.min(w, Math.max(340, avail - leftW)), 340, 640));
    };
    reclampRails();
    window.addEventListener("resize", reclampRails);
    return () => window.removeEventListener("resize", reclampRails);
  }, [leftW, rightW]);
  useEffect(() => { localStorage.setItem(LS.leftOpen, leftOpen ? "1" : "0"); }, [leftOpen]);
  useEffect(() => { localStorage.setItem(LS.rightOpen, rightOpen ? "1" : "0"); }, [rightOpen]);

  // hotkeys (Cursor-style, adapted for the harness). Most map to panels/sessions/nav;
  // IDE-only ones (inline edit, autocomplete) do not apply to an orchestration harness.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;
      const k = e.key.toLowerCase();
      // Cmd+` -> focus the Terminal tab (classic terminal toggle)
      if (e.key === "`") { e.preventDefault(); setRightOpen(true); window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "terminal" })); return; }
      if (e.shiftKey) {
        // Cmd+Shift+J -> Settings (Cursor: Cursor settings)
        if (k === "j") { e.preventDefault(); setRightOpen(true); window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "settings" })); }
        return;
      }
      switch (k) {
        case "b": e.preventDefault(); setLeftOpen((v) => !v); break;        // toggle sessions panel
        case "j": e.preventDefault(); setRightOpen((v) => !v); break;       // toggle right pane
        case "i":                                                          // focus chat input (Cursor: toggle sidepanel)
        case "l": e.preventDefault(); window.dispatchEvent(new Event("harness-focus-input")); break;
        case "n":                                                          // new session (Cursor: new chat)
        case "r": e.preventDefault(); window.dispatchEvent(new Event("harness-new-session")); break;
        default: break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="h-full flex flex-col">
      <UpdateBanner />
      {/* Keyless nudge: agentic is the shipped default, so instead of a demo run
          we tell the user to plug in a key. Suppressed while the first-run wizard
          is up (it already covers key setup) to avoid stacking two prompts. */}
      {config?.agentic_ready === false && !showWizard && (
        <ProviderKeyBanner
          onAddKey={() => {
            setRightOpen(true);
            window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "settings" }));
          }}
        />
      )}
      <div className="flex-1 min-h-0 flex">
        {leftOpen && (
          <>
            <div style={{ width: leftW }} className="shrink-0 h-full overflow-hidden">
              <LeftRail jobsRefresh={jobsRefresh} onSessionChange={setActiveSessionId} />
            </div>
            <Resizer side="left" onResize={(dx) => setLeftW((w) => clamp(w + dx, 180, 420))} />
          </>
        )}
        <div className="flex-1 min-w-0 h-full flex flex-col">
          <div className="flex-1 min-h-0">
            <ErrorBoundary label="Chat">
              <Conversation
                config={config}
                activeSessionId={activeSessionId}
                onArtifacts={(a) => setArtifacts((prev) => [...a, ...prev])}
                onJobChange={() => setJobsRefresh((n) => n + 1)}
              />
            </ErrorBoundary>
          </div>
        </div>
        {rightOpen && (
          <>
            <Resizer side="right" onResize={(dx) => setRightW((w) => clamp(w + dx, 340, 640))} />
            <div style={{ width: rightW }} className="shrink-0 h-full overflow-hidden">
              <ErrorBoundary label="Side panel">
                <RightPane artifacts={artifacts} onOpenWizard={() => setShowWizard(true)} />
              </ErrorBoundary>
            </div>
          </>
        )}
      </div>
      <StatusBar config={config}
        leftOpen={leftOpen} rightOpen={rightOpen}
        onToggleLeft={() => setLeftOpen((v) => !v)} onToggleRight={() => setRightOpen((v) => !v)} />

      {showWizard && <RegistryWizard onClose={() => { localStorage.setItem("pmharness.wizardSeen", "1"); setShowWizard(false); }} />}
    </div>
  );
}
