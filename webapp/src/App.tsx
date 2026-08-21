import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Config } from "./lib/api";
import { subscribeDocumentMotionPolicy } from "./lib/motionPolicy";
import LeftRail from "./components/LeftRail";
import Conversation from "./components/Conversation";
import RightPane from "./components/RightPane";
import RightDock from "./components/RightDock";
import StatusBar from "./components/StatusBar";
import UpdateBanner, { type UpdateAvailability } from "./components/UpdateBanner";
import ProviderKeyBanner from "./components/ProviderKeyBanner";
import Resizer from "./components/Resizer";
import RegistryWizard from "./components/RegistryWizard";
import ErrorBoundary from "./components/ErrorBoundary";
import CommandPalette from "./components/CommandPalette";
import {
  droppedPathIsDirectory,
  resolveDroppedOsPath,
} from "./components/conversation/composerInput";
import { openAgentWorkspace } from "./lib/agentLinks";
import { reclampRailWidths } from "./lib/railLayout";

const LS = {
  left: "pmharness.leftW",
  leftOpen: "pmharness.leftOpen", rightOpen: "pmharness.rightOpen", rightW: "pmharness.rightW",
};
const num = (k: string, d: number) => { const v = Number(localStorage.getItem(k)); return Number.isFinite(v) && v > 0 ? v : d; };
const bool = (k: string, d: boolean) => { const v = localStorage.getItem(k); return v === null ? d : v === "1"; }

function lastRightTab(): string {
  try {
    const raw = localStorage.getItem("pmharness.splitState");
    if (!raw) return "state";
    const parsed = JSON.parse(raw);
    const t = parsed?.primaryTab;
    if (typeof t === "string" && t && t !== "settings" && t !== "mcp") return t;
  } catch { /* ignore */ }
  return "state";
}

function hasStoredRightPaneCards(): boolean {
  try {
    const savedCards = JSON.parse(localStorage.getItem("pmharness.board.openCards") || "null");
    if (Array.isArray(savedCards)) {
      return savedCards.some((tab) => typeof tab === "string" && tab !== "settings");
    }

    const legacySplit = JSON.parse(localStorage.getItem("pmharness.splitState") || "null");
    return typeof legacySplit?.primaryTab === "string"
      && legacySplit.primaryTab !== "settings";
  } catch {
    return false;
  }
}

export default function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [availableUpdate, setAvailableUpdate] = useState<UpdateAvailability | null>(null);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [artifacts, setArtifacts] = useState<{ type: string; headline: string; confidence?: number }[]>([]);
  const [jobsRefresh, setJobsRefresh] = useState(0);

  useEffect(() => {
    setArtifacts([]);
    // StatusBar tok/$ must not keep a prior session's spend painted under a
    // new id (zeros-guard used to freeze stale 83.9k-style totals).
    window.dispatchEvent(
      new CustomEvent("harness-session-changed", {
        detail: { sessionId: activeSessionId },
      }),
    );
  }, [activeSessionId]);

  const [leftW, setLeftW] = useState(() => num(LS.left, 248));
  const [leftOpen, setLeftOpen] = useState(() => bool(LS.leftOpen, true));
  // Default hidden: chat-first layout; RightDock surfaces floating tools.
  const [rightOpen, setRightOpen] = useState(
    () => bool(LS.rightOpen, false) && hasStoredRightPaneCards(),
  );
  const [rightW, setRightW] = useState(() => num(LS.rightW, 520));
  const pendingRightTab = useRef<string | null>(null);
  const leftWRef = useRef(leftW);
  const rightWRef = useRef(rightW);
  leftWRef.current = leftW;
  rightWRef.current = rightW;

  const openRightTo = (tab: string) => {
    const target = tab || "state";
    pendingRightTab.current = target;
    setRightOpen((open) => {
      if (open) {
        // Pane already mounted — the rightOpen effect only runs on transitions,
        // so apply the focus now. Without this, Add key / hotkeys are no-ops
        // when the right rail is already open.
        pendingRightTab.current = null;
        window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: target }));
      }
      return true;
    });
  };
  const closeEmptyRightPane = useCallback(() => setRightOpen(false), []);
  const toggleRight = useCallback(() => {
    setRightOpen((open) => {
      if (open) return false;
      // First open (or re-open after every card was closed) must seed a tab.
      // Bare setRightOpen(true) mounts an empty board that immediately
      // onEmpty-closes — the Ctrl+J flash.
      if (!hasStoredRightPaneCards()) {
        pendingRightTab.current = lastRightTab();
      }
      return true;
    });
  }, []);
  const requestRightMinWidth = useCallback((minPx: number) => {
    const next = reclampRailWidths(
      leftWRef.current,
      Math.max(rightWRef.current, minPx),
      leftOpen,
      true,
      window.innerWidth,
    );
    setLeftW(next.leftW);
    setRightW(next.rightW);
  }, [leftOpen]);

  const [showWizard, setShowWizard] = useState(false);

  const fetchConfig = () => {
    api.config().then(setConfig).catch(() => {});
  };

  // Prevent the Electron window from navigating to a file dropped anywhere
  // outside an explicit drop target (the default would replace the whole app
  // with the file). Composer + message drop zones stopPropagation, so they keep
  // working; this is the safety net for drops that miss those targets.
  // Folders that miss the composer open as the workspace (Hermes-style).
  useEffect(() => {
    const hasFiles = (e: DragEvent) =>
      !!(e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files"));
    const prevent = (e: DragEvent) => {
      if (hasFiles(e)) e.preventDefault();
    };
    const onDrop = async (e: DragEvent) => {
      prevent(e);
      const files = Array.from(e.dataTransfer?.files || []);
      for (const file of files) {
        const osPath = resolveDroppedOsPath(file as { path?: string });
        if (osPath && await droppedPathIsDirectory(osPath)) {
          openAgentWorkspace(osPath);
        }
      }
    };
    window.addEventListener("dragover", prevent);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragover", prevent);
      window.removeEventListener("drop", onDrop);
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

  // PERF: pause nonessential CSS motion while the app is backgrounded. Keep
  // separate idle/hidden classes so visible worker activity can remain truthful
  // when the window is merely unfocused, while hidden windows fully pause.
  // Unsubscribe clears both classes so App unmount cannot leave them on <html>.
  useEffect(() => subscribeDocumentMotionPolicy(), []);

  // persist layout
  useEffect(() => { localStorage.setItem(LS.left, String(leftW)); }, [leftW]);

  // Re-clamp persisted rail widths against the real window width on mount,
  // when either rail opens/closes, and whenever the window shrinks. Resizers
  // clamp only during a drag, so a wide saved layout restored into a small
  // window could otherwise crush the chat column below MIN_CENTER_W.
  useEffect(() => {
    const reclampRails = () => {
      const next = reclampRailWidths(
        leftWRef.current,
        rightWRef.current,
        leftOpen,
        rightOpen,
        window.innerWidth,
      );
      setLeftW(next.leftW);
      setRightW(next.rightW);
    };
    reclampRails();
    window.addEventListener("resize", reclampRails);
    return () => window.removeEventListener("resize", reclampRails);
  }, [leftOpen, rightOpen]);
  useEffect(() => { localStorage.setItem(LS.leftOpen, leftOpen ? "1" : "0"); }, [leftOpen]);
  useEffect(() => { localStorage.setItem(LS.rightOpen, rightOpen ? "1" : "0"); }, [rightOpen]);
  useEffect(() => { localStorage.setItem(LS.rightW, String(rightW)); }, [rightW]);

  // After the floating tools become visible, apply pending focus from the dock / hotkeys.
  useEffect(() => {
    if (!rightOpen || !pendingRightTab.current) return;
    const tab = pendingRightTab.current;
    pendingRightTab.current = null;
    window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: tab }));
  }, [rightOpen]);

  // If something asks for a tab while floating tools are hidden, show them first.
  useEffect(() => {
    const onFocusTab = (e: Event) => {
      const tab = (e as CustomEvent).detail;
      if (!tab || typeof tab !== "string") return;
      setRightOpen((open) => {
        if (!open) pendingRightTab.current = tab;
        return true;
      });
    };
    window.addEventListener("harness-focus-tab", onFocusTab as EventListener);
    return () => window.removeEventListener("harness-focus-tab", onFocusTab as EventListener);
  }, []);

  // hotkeys (Cursor-style, adapted for the harness). Most map to panels/sessions/nav;
  // IDE-only ones (inline edit, autocomplete) do not apply to an orchestration harness.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;
      const k = e.key.toLowerCase();
      // Cmd+` -> focus the Terminal tab (classic terminal toggle)
      if (e.key === "`") { e.preventDefault(); openRightTo("terminal"); return; }
      if (e.shiftKey) {
        // Cmd+Shift+J -> Settings (Cursor: Cursor settings)
        if (k === "j") { e.preventDefault(); openRightTo("settings"); }
        return;
      }
      switch (k) {
        case "b": e.preventDefault(); setLeftOpen((v) => !v); break;        // toggle sessions panel
        case "j": e.preventDefault(); toggleRight(); break;       // toggle right pane
        case "i":                                                          // focus chat input (Cursor: toggle sidepanel)
        // Cmd/Ctrl+L focuses the composer. TerminalPane capture steals this
        // when the live xterm has a selection (Add to chat).
        case "l": e.preventDefault(); window.dispatchEvent(new Event("harness-focus-input")); break;
        case "n":                                                          // new session (Cursor: new chat)
        case "r": e.preventDefault(); window.dispatchEvent(new Event("harness-new-session")); break;
        default: break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggleRight]);

  return (
    <div className="h-full flex flex-col bg-[var(--shell-chrome)]">
      <UpdateBanner onAvailabilityChange={setAvailableUpdate} />
      {/* Keyless nudge: agentic is the shipped default, so instead of a demo run
          we tell the user to plug in a key. Suppressed while the first-run wizard
          is up (it already covers key setup) to avoid stacking two prompts. */}
      {config && (config.workers_ready ?? config.agentic_ready) === false && !showWizard && (
        <ProviderKeyBanner
          variant={
            config.pilot_ready && (config.workers_ready ?? config.agentic_ready) === false
              ? "workers"
              : "keyless"
          }
          onAddKey={() => setShowWizard(true)}
        />
      )}
      {/* Left rail and tool cards sit on the conversation surface. */}
      <div className="flex-1 min-h-0 min-w-0 flex px-px pt-px">
        <div
          className={`relative flex-1 min-w-0 h-full flex overflow-hidden border border-[var(--shell-panel-border)] ${
            leftOpen && rightOpen
              ? "rounded-none"
              : leftOpen
                ? "rounded-r-[var(--shell-panel-radius)]"
                : rightOpen
                  ? "rounded-l-[var(--shell-panel-radius)]"
                  : "rounded-[var(--shell-panel-radius)]"
          } ${leftOpen ? "border-l-0" : ""} ${rightOpen ? "border-r-0" : ""}`}
          style={{
            backgroundColor: "var(--shell-chat, #0f1113)",
            backgroundImage:
              "radial-gradient(120% 80% at 50% -10%, rgba(139,150,196,0.06), rgba(139,150,196,0) 60%)",
          }}
        >
          {leftOpen && (
            <>
              <div style={{ width: leftW }} className="shell-inset-panel shrink-0 h-full">
                <LeftRail jobsRefresh={jobsRefresh} onSessionChange={setActiveSessionId} />
              </div>
              <Resizer
                side="left"
                onResize={(dx) => {
                  const next = reclampRailWidths(
                    leftWRef.current + dx,
                    rightWRef.current,
                    true,
                    rightOpen,
                    window.innerWidth,
                  );
                  setLeftW(next.leftW);
                  setRightW(next.rightW);
                }}
              />
            </>
          )}
          <div className="relative flex-1 min-w-0 min-h-0 flex flex-col">
            <div className="flex-1 min-h-0 min-w-0">
              <ErrorBoundary label="Chat">
                <Conversation
                  config={config}
                  activeSessionId={activeSessionId}
                  onArtifacts={(a) => setArtifacts((prev) => [...a, ...prev])}
                  onJobChange={() => setJobsRefresh((n) => n + 1)}
                />
              </ErrorBoundary>
            </div>
            <RightDock
              panelsOpen={rightOpen}
              onOpenTab={openRightTo}
              onExpand={() => openRightTo(lastRightTab())}
              onCollapse={() => setRightOpen(false)}
            />
          </div>
          {rightOpen && (
            <Resizer
              side="right"
              onResize={(dx) => {
                const next = reclampRailWidths(
                  leftWRef.current,
                  rightWRef.current + dx,
                  leftOpen,
                  true,
                  window.innerWidth,
                );
                setLeftW(next.leftW);
                setRightW(next.rightW);
              }}
            />
          )}
          <div
            className={`shrink-0 h-full min-w-0 overflow-hidden ${rightOpen ? "" : "hidden"}`}
            style={{ width: rightW }}
          >
            <ErrorBoundary label="Tool board">
              <RightPane
                visible={rightOpen}
                artifacts={artifacts}
                onOpenWizard={() => setShowWizard(true)}
                initialTab={pendingRightTab.current}
                onEmpty={closeEmptyRightPane}
                onRequestMinWidth={requestRightMinWidth}
              />
            </ErrorBoundary>
          </div>
        </div>
      </div>
      <div className="shrink-0 px-px py-px">
        <StatusBar config={config} update={availableUpdate}
          leftOpen={leftOpen} rightOpen={rightOpen}
          onToggleLeft={() => setLeftOpen((v) => !v)} onToggleRight={toggleRight}
          onOpenEconomics={() => openRightTo("economics")} />
      </div>

      {showWizard && <RegistryWizard onClose={() => { localStorage.setItem("pmharness.wizardSeen", "1"); setShowWizard(false); }} />}

      <CommandPalette
        onToggleLeft={() => setLeftOpen((v) => !v)}
        onToggleRight={toggleRight}
      />
    </div>
  );
}
